#!/usr/bin/env python3
"""Prototype of the upstream skill overlay tool (docs/designs/upstream-skill-overlays.md).

Each mirrored skill has three layers:

  1. third_party/google-skills/<skill>/        exact upstream copy at a pinned commit
  2. agents/platform/skill-overlays/<skill>/    upstream.lock, NNNN-<slug>.patch files, append.md
  3. agents/platform/skills/<skill>/            the generated skill: 1 with 2 applied

Subcommands: sync, continue, refresh, generate, check, status, verify-upstream.
"""

import argparse
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_URL_ENV = "SKILL_OVERLAY_UPSTREAM"
DEFAULT_UPSTREAM_URL = "https://github.com/gke-agentic/skill-overlay-demo-upstream.git"
UPSTREAM_BRANCH = "main"
UPSTREAM_SKILLS_PATH = "skills/cloud"
COPY_ROOT = REPO_ROOT / "third_party" / "google-skills"
OVERLAY_ROOT = REPO_ROOT / "agents" / "platform" / "skill-overlays"
SKILLS_ROOT = REPO_ROOT / "agents" / "platform" / "skills"
SCRATCH_ROOT = REPO_ROOT / ".skill-sync"
UPSTREAM_CACHE = SCRATCH_ROOT / "upstream.git"
LOCK_NAME = "upstream.lock"
APPEND_NAME = "append.md"
SKILL_MD = "SKILL.md"
APPEND_MARKER = "<!-- kube-agents: local addition -->"
PATCH_GLOB = "[0-9][0-9][0-9][0-9]-*.patch"
PATCH_NUMBER_WIDTH = 4
STATE_FILE = "sync-state.json"
EXECUTABLE_MODE = "755"
REGULAR_MODE = "644"
EXIT_FAILURE = 1
EXIT_CONFLICT = 2
PATCH_HEADER_TEMPLATE = (
    "Subject: {subject}\n"
    "\n"
    "Why: TODO, say why this repository needs the change\n"
    "Local-Issue: TODO\n"
    "Upstream-Issue: none\n"
    "Retire-When: TODO\n"
    "\n"
)
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "skill-overlay",
    "GIT_AUTHOR_EMAIL": "skill-overlay@example.invalid",
    "GIT_COMMITTER_NAME": "skill-overlay",
    "GIT_COMMITTER_EMAIL": "skill-overlay@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


class OverlayError(Exception):
    """A user-facing failure; the message says what to do."""


# ---------------------------------------------------------------- helpers


def git(args, cwd, check=True, input_bytes=None, extra_env=None):
    env = dict(os.environ, **GIT_ENV, **(extra_env or {}))
    res = subprocess.run(["git", *args], cwd=cwd, env=env, input=input_bytes, capture_output=True)
    if check and res.returncode != 0:
        raise OverlayError(f"git {' '.join(args)} failed:\n{res.stderr.decode(errors='replace')}")
    return res


def git_out(args, cwd, **kw):
    return git(args, cwd, **kw).stdout.decode().strip()


def git_diff_text(args, cwd):
    """Diff output exactly as git wrote it: trimming would drop trailing blank context lines."""
    return git(args, cwd).stdout.decode()


def upstream_url():
    return os.environ.get(UPSTREAM_URL_ENV, DEFAULT_UPSTREAM_URL)


def copy_dir(skill):
    return COPY_ROOT / skill


def overlay_dir(skill):
    return OVERLAY_ROOT / skill


def generated_dir(skill):
    return SKILLS_ROOT / skill


def is_mirrored(skill):
    return (overlay_dir(skill) / LOCK_NAME).is_file()


def mirrored_skills():
    if not OVERLAY_ROOT.is_dir():
        return []
    return sorted(p.name for p in OVERLAY_ROOT.iterdir() if (p / LOCK_NAME).is_file())


def read_lock(skill):
    values = {}
    for line in (overlay_dir(skill) / LOCK_NAME).read_text().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            values[key.strip()] = value.strip()
    return values


def write_lock(skill, commit, digest):
    overlay_dir(skill).mkdir(parents=True, exist_ok=True)
    (overlay_dir(skill) / LOCK_NAME).write_text(f"commit: {commit}\nsha256: {digest}\n")


def files_of(root):
    root = Path(root)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts)


def file_mode(path):
    return EXECUTABLE_MODE if os.access(path, os.X_OK) else REGULAR_MODE


def tree_sha256(root):
    """sha256 over every file's path, executable bit and bytes, in path order."""
    digest = hashlib.sha256()
    for rel in files_of(root):
        path = Path(root) / rel
        digest.update(rel.encode() + b"\0" + file_mode(path).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def compare_trees(expected, actual):
    """Return a list of 'path: reason' strings where the two trees differ."""
    problems = []
    exp, act = set(files_of(expected)), set(files_of(actual))
    for rel in sorted(exp - act):
        problems.append(f"{rel}: missing")
    for rel in sorted(act - exp):
        problems.append(f"{rel}: not produced by the upstream copy and overlay")
    for rel in sorted(exp & act):
        a, b = Path(expected) / rel, Path(actual) / rel
        if a.read_bytes() != b.read_bytes():
            problems.append(f"{rel}: content differs")
        elif file_mode(a) != file_mode(b):
            problems.append(f"{rel}: executable bit differs")
    return problems


def replace_tree(src, dest):
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))


def clear_worktree(repo):
    for child in Path(repo).iterdir():
        if child.name == ".git":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def fill_worktree(repo, src):
    for rel in files_of(src):
        target = Path(repo) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(src) / rel, target)


def patches(skill):
    return sorted(overlay_dir(skill).glob(PATCH_GLOB))


def patch_header(text):
    idx = text.find("diff --git ")
    return text if idx < 0 else text[:idx]


def patch_why(path):
    for line in Path(path).read_text().splitlines():
        if line.startswith("Why:"):
            return line[len("Why:"):].strip()
    return ""


def separator_for(text):
    return "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")


def strip_index_lines(diff):
    return "".join(line for line in diff.splitlines(keepends=True) if not line.startswith("index "))


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48].rstrip("-") or "change"


# ------------------------------------------------------------- building


def apply_overlay(skill, base, dest):
    """Copy base to dest, apply the skill's patches in filename order, then append.md."""
    replace_tree(base, dest)
    for patch in patches(skill):
        res = git(["apply", "-p1", "--whitespace=nowarn", str(patch)], cwd=dest, check=False)
        if res.returncode != 0:
            raise OverlayError(
                f"{skill}: patch {patch.name} no longer applies. Either an earlier patch it depended on "
                f"was deleted or a hand edit clashes (fold or refresh it, then run "
                f"`make skills-generate SKILL={skill}`), or the upstream copy was changed without "
                f"`make skills-sync SKILL={skill}` (revert the copy and lock, then sync, which "
                f"carries the patches forward).\n{res.stderr.decode(errors='replace')}"
            )
    append = overlay_dir(skill) / APPEND_NAME
    if append.is_file():
        target = Path(dest) / SKILL_MD
        text = target.read_text()
        target.write_text(text + separator_for(text) + append.read_text())


def build(skill, dest):
    apply_overlay(skill, copy_dir(skill), dest)


def verify_copy(skill):
    lock = read_lock(skill)
    actual = tree_sha256(copy_dir(skill))
    if actual != lock.get("sha256"):
        raise OverlayError(
            f"{skill}: {copy_dir(skill).relative_to(REPO_ROOT)} no longer matches the sha256 in "
            f"{LOCK_NAME}. The upstream copy was edited by hand: revert it, and change it only with "
            f"`make skills-sync SKILL={skill}`."
        )


# ------------------------------------------------------------- upstream


def upstream_cache():
    if not (UPSTREAM_CACHE / "HEAD").exists():
        UPSTREAM_CACHE.mkdir(parents=True, exist_ok=True)
        git(["init", "-q", "--bare"], cwd=UPSTREAM_CACHE)
    git(["fetch", "-q", "--tags", "--force", upstream_url(),
         f"+refs/heads/{UPSTREAM_BRANCH}:refs/heads/{UPSTREAM_BRANCH}"], cwd=UPSTREAM_CACHE)
    return UPSTREAM_CACHE


def resolve(cache, ref):
    res = git(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], cwd=cache, check=False)
    if res.returncode != 0:
        git(["fetch", "-q", upstream_url(), ref], cwd=cache, check=False)
        res = git(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], cwd=cache, check=False)
    if res.returncode != 0:
        raise OverlayError(f"upstream has no commit or ref {ref!r}")
    return res.stdout.decode().strip()


def require_on_upstream_branch(cache, commit):
    if git(["merge-base", "--is-ancestor", commit, UPSTREAM_BRANCH], cwd=cache, check=False).returncode != 0:
        raise OverlayError(
            f"commit {commit[:12]} is not on upstream's {UPSTREAM_BRANCH} branch. A commit that exists "
            f"only in a fork is not published upstream and cannot be pinned."
        )


def export_skill(cache, commit, skill, dest):
    """Write upstream's skills/cloud/<skill> at commit to dest. False if upstream has no such skill."""
    path = f"{UPSTREAM_SKILLS_PATH}/{skill}"
    if git(["cat-file", "-e", f"{commit}:{path}"], cwd=cache, check=False).returncode != 0:
        return False
    data = git(["archive", "--format=tar", commit, path], cwd=cache).stdout
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(tmp, filter="tar")
        replace_tree(Path(tmp) / path, dest)
    return True


def upstream_tree_id(cache, commit, skill):
    res = git(["rev-parse", "-q", "--verify", f"{commit}:{UPSTREAM_SKILLS_PATH}/{skill}"], cwd=cache, check=False)
    return res.stdout.decode().strip() if res.returncode == 0 else None


def staleness_notice(skill):
    try:
        cache = upstream_cache()
    except OverlayError:
        return
    commit = read_lock(skill)["commit"]
    if upstream_tree_id(cache, commit, skill) != upstream_tree_id(cache, UPSTREAM_BRANCH, skill):
        newer = git_out(["rev-list", "--count", f"{commit}..{UPSTREAM_BRANCH}", "--",
                         f"{UPSTREAM_SKILLS_PATH}/{skill}"], cwd=cache)
        print(f"note: upstream has {newer} newer commit(s) for {skill}. To take them, run "
              f"`make skills-sync SKILL={skill}` in its own commit or PR.")


# --------------------------------------------------------- series repos


def series_repo(skill, path):
    """A scratch repo: the upstream copy as the root commit, then one commit per patch."""
    if Path(path).exists():
        shutil.rmtree(path)
    Path(path).mkdir(parents=True)
    git(["init", "-q", "-b", "patches"], cwd=path)
    fill_worktree(path, copy_dir(skill))
    git(["add", "-A"], cwd=path)
    git(["commit", "-q", "--allow-empty", "-m", "upstream copy"], cwd=path)
    git(["tag", "base"], cwd=path)
    for patch in patches(skill):
        git(["apply", "-p1", "--whitespace=nowarn", str(patch)], cwd=path)
        git(["add", "-A"], cwd=path)
        git(["commit", "-q", "--allow-empty", "-m", patch.name], cwd=path)
    return Path(path)


def export_series(skill, repo, since, headers):
    """Rewrite the overlay's patch files from the commits since `since`. Returns surviving names."""
    names = []
    for commit in git_out(["rev-list", "--reverse", f"{since}..HEAD"], cwd=repo).split():
        name = git_out(["log", "-1", "--format=%s", commit], cwd=repo)
        diff = git_diff_text(["show", "--format=", "--no-color", "--binary", commit], cwd=repo)
        header = headers.get(name, PATCH_HEADER_TEMPLATE.format(subject=name))
        (overlay_dir(skill) / name).write_text(header + strip_index_lines(diff))
        names.append(name)
    return names


def headers_of(skill):
    return {p.name: patch_header(p.read_text()) for p in patches(skill)}


def lines_changed_share(skill):
    total = changed = 0
    for rel in files_of(generated_dir(skill)):
        new = (generated_dir(skill) / rel).read_text(errors="replace").splitlines()
        old_path = copy_dir(skill) / rel
        old = old_path.read_text(errors="replace").splitlines() if old_path.exists() else []
        total += len(new)
        changed += sum(1 for line in difflib.ndiff(old, new) if line.startswith("+ "))
    return 0 if not total else round(100 * changed / total)


# ------------------------------------------------------------ subcommands


def cmd_generate(skill):
    require_mirrored(skill)
    with tempfile.TemporaryDirectory() as tmp:
        build(skill, Path(tmp) / skill)
        replace_tree(Path(tmp) / skill, generated_dir(skill))
    print(f"generated {generated_dir(skill).relative_to(REPO_ROOT)}")


def cmd_check(skills):
    failures = []
    for skill in skills or mirrored_skills():
        try:
            verify_copy(skill)
            with tempfile.TemporaryDirectory() as tmp:
                build(skill, Path(tmp) / skill)
                problems = compare_trees(Path(tmp) / skill, generated_dir(skill))
            if problems:
                raise OverlayError(
                    f"{skill}: the committed skill differs from upstream copy + overlay:\n  "
                    + "\n  ".join(problems)
                    + f"\nIf you edited the skill, run `make skills-refresh SKILL={skill}`. If you "
                    f"edited a patch or {APPEND_NAME}, run `make skills-generate SKILL={skill}`."
                )
        except OverlayError as e:
            failures.append(str(e))
    for msg in failures:
        print(f"FAIL {msg}\n", file=sys.stderr)
    checked = len(skills or mirrored_skills())
    if failures:
        raise SystemExit(EXIT_FAILURE)
    print(f"ok: {checked} mirrored skill(s) match their upstream copy + overlay")


def split_append(skill, edited_md, base_md):
    """Separate the appended section from an edited SKILL.md. Returns (body, append_text or None)."""
    if not (overlay_dir(skill) / APPEND_NAME).is_file() or APPEND_MARKER not in edited_md:
        return edited_md, None
    idx = edited_md.index(APPEND_MARKER)
    body, appended = edited_md[:idx], edited_md[idx:]
    sep = separator_for(base_md)
    if sep and body.endswith(sep):
        body = body[: -len(sep)]
    return body, appended


def overlap_warnings(repo):
    warnings = set()
    diff = git_out(["diff", "-U0", "--no-color", "HEAD"], cwd=repo)
    current = None
    for line in diff.splitlines():
        if line.startswith("--- a/"):
            current = line[len("--- a/"):]
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? ", line)
        if m and current:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            if count == 0:
                continue
            blame = git_out(["blame", "--porcelain", "-L", f"{start},{start + count - 1}", "HEAD", "--", current],
                            cwd=repo, check=False)
            for commit in {b.split()[0] for b in blame.splitlines() if re.match(r"^[0-9a-f]{40} ", b)}:
                subject = git_out(["log", "-1", "--format=%s", commit], cwd=repo)
                if re.match(r"^\d{4}-.*\.patch$", subject):
                    warnings.add(subject)
    return sorted(warnings)


def cmd_refresh(skill, patch_ref=None, message=None):
    require_mirrored(skill)
    verify_copy(skill)
    repo = series_repo(skill, SCRATCH_ROOT / f"{skill}-refresh")
    try:
        base_md = (repo / SKILL_MD).read_text() if (repo / SKILL_MD).exists() else ""
        with tempfile.TemporaryDirectory() as tmp:
            edited = Path(tmp) / skill
            replace_tree(generated_dir(skill), edited)
            md = edited / SKILL_MD
            if md.exists():
                body, appended = split_append(skill, md.read_text(), base_md)
                md.write_text(body)
                if appended is not None and appended != (overlay_dir(skill) / APPEND_NAME).read_text():
                    (overlay_dir(skill) / APPEND_NAME).write_text(appended)
                    print(f"updated {APPEND_NAME} from the appended section")
            clear_worktree(repo)
            fill_worktree(repo, edited)
        git(["add", "-A"], cwd=repo)
        if not git_out(["status", "--porcelain"], cwd=repo):
            print(f"{skill}: no change outside {APPEND_NAME} to record")
        else:
            for name in overlap_warnings(repo):
                if not patch_ref or not name.startswith(patch_ref):
                    print(f"warning: this edit changes lines that {name} introduced; consider "
                          f"`make skills-refresh SKILL={skill} PATCH={name[:PATCH_NUMBER_WIDTH]}`")
            headers = headers_of(skill)
            if patch_ref:
                target = next((p.name for p in patches(skill) if p.name.startswith(patch_ref)), None)
                if not target:
                    raise OverlayError(f"{skill}: no patch starts with {patch_ref!r}")
                git(["commit", "-q", "-m", f"fixup! {target}"], cwd=repo)
                res = git(["rebase", "-q", "-i", "--autosquash", "base"], cwd=repo, check=False,
                          extra_env={"GIT_SEQUENCE_EDITOR": "true", "GIT_EDITOR": "true"})
                if res.returncode != 0:
                    raise OverlayError(f"{skill}: folding into {target} conflicts with a later patch; "
                                       f"refresh without PATCH= instead.")
                for p in patches(skill):
                    p.unlink()
                export_series(skill, repo, "base", headers)
                print(f"folded the edit into {target}")
            else:
                numbers = [int(p.name[:PATCH_NUMBER_WIDTH]) for p in patches(skill)]
                name = f"{(max(numbers) + 1 if numbers else 1):0{PATCH_NUMBER_WIDTH}d}-{slugify(message or 'change')}.patch"
                git(["commit", "-q", "-m", name], cwd=repo)
                headers[name] = PATCH_HEADER_TEMPLATE.format(subject=message or "TODO describe the change")
                diff = git_diff_text(["show", "--format=", "--no-color", "--binary", "HEAD"], cwd=repo)
                (overlay_dir(skill) / name).write_text(headers[name] + strip_index_lines(diff))
                print(f"wrote {(overlay_dir(skill) / name).relative_to(REPO_ROOT)}; fill in its Why: header")
    finally:
        shutil.rmtree(repo, ignore_errors=True)
    cmd_generate(skill)
    staleness_notice(skill)


def cmd_sync(skill, ref=None):
    cache = upstream_cache()
    commit = resolve(cache, ref or UPSTREAM_BRANCH)
    require_on_upstream_branch(cache, commit)
    scratch = SCRATCH_ROOT / skill
    if scratch.exists():
        raise OverlayError(f"{skill}: a sync is already in progress in {scratch.relative_to(REPO_ROOT)}; "
                           f"run `make skills-continue SKILL={skill}`, or delete that directory to abandon it.")
    with tempfile.TemporaryDirectory() as tmp:
        new_copy = Path(tmp) / skill
        if not export_skill(cache, commit, skill, new_copy):
            raise OverlayError(f"upstream has no {UPSTREAM_SKILLS_PATH}/{skill} at {commit[:12]}. If it was "
                               f"renamed or removed, move or remove the copy, overlay and lock by hand.")
        if not is_mirrored(skill):
            if generated_dir(skill).exists():
                raise OverlayError(f"{skill}: agents/platform/skills/{skill} already exists and is not mirrored "
                                   f"(it has no lock). Rename one of them before adopting upstream's.")
            replace_tree(new_copy, copy_dir(skill))
            write_lock(skill, commit, tree_sha256(copy_dir(skill)))
            cmd_generate(skill)
            print(f"adopted {skill} at upstream {commit[:12]}")
            return
        verify_copy(skill)
        old = read_lock(skill)["commit"]
        if old == commit:
            print(f"{skill}: already at upstream {commit[:12]}")
            return
        repo = series_repo(skill, scratch)
        git(["checkout", "-q", "-b", "upstream-new", "base"], cwd=repo)
        clear_worktree(repo)
        fill_worktree(repo, new_copy)
        git(["add", "-A"], cwd=repo)
        git(["commit", "-q", "--allow-empty", "-m", f"upstream copy @ {commit[:12]}"], cwd=repo)
        git(["checkout", "-q", "patches"], cwd=repo)
        state = {"commit": commit, "old": old, "headers": headers_of(skill),
                 "patches": [p.name for p in patches(skill)],
                 "why": {p.name: patch_why(p) for p in patches(skill)}, "conflicts": 0}
        (repo / ".git" / STATE_FILE).write_text(json.dumps(state))
        snapshot = Path(tmp) / "new-copy-snapshot"
        replace_tree(new_copy, snapshot)
        shutil.copytree(snapshot, repo / ".git" / "new-copy")
    res = git(["rebase", "--empty=drop", "upstream-new"], cwd=repo, check=False)
    continue_or_stop(skill, repo, res)


def continue_or_stop(skill, repo, res):
    state_path = repo / ".git" / STATE_FILE
    while res.returncode != 0:
        state = json.loads(state_path.read_text())
        state["conflicts"] += 1
        state_path.write_text(json.dumps(state))
        stopped = git_out(["log", "-1", "--format=%s", "REBASE_HEAD"], cwd=repo, check=False) or "a patch"
        conflicted = git_out(["diff", "--name-only", "--diff-filter=U"], cwd=repo, check=False)
        print(f"CONFLICT: {stopped} overlaps upstream's change in: {conflicted or '(see git status)'}\n"
              f"Fix the conflict markers in {repo.relative_to(REPO_ROOT)}/, then run "
              f"`make skills-continue SKILL={skill}`.", file=sys.stderr)
        raise SystemExit(EXIT_CONFLICT)
    finish_sync(skill, repo)


def cmd_continue(skill):
    repo = SCRATCH_ROOT / skill
    if not repo.exists():
        raise OverlayError(f"{skill}: no sync in progress")
    git(["add", "-A"], cwd=repo)
    res = git(["rebase", "--continue"], cwd=repo, check=False, extra_env={"GIT_EDITOR": "true"})
    continue_or_stop(skill, repo, res)


def finish_sync(skill, repo):
    state = json.loads((repo / ".git" / STATE_FILE).read_text())
    for p in patches(skill):
        p.unlink()
    surviving = export_series(skill, repo, "upstream-new", state["headers"])
    retired = [name for name in state["patches"] if name not in surviving]
    replace_tree(repo / ".git" / "new-copy", copy_dir(skill))
    write_lock(skill, state["commit"], tree_sha256(copy_dir(skill)))
    shutil.rmtree(repo)
    cmd_generate(skill)
    print(f"\nsynced {skill}: upstream {state['old'][:12]} -> {state['commit'][:12]}")
    for name in retired:
        print(f"  retired {name} (upstream now carries it). Why: {state['why'].get(name) or '-'}")
    append = overlay_dir(skill) / APPEND_NAME
    if append.is_file():
        body = append.read_text().replace(APPEND_MARKER, "").strip()
        upstream_md = copy_dir(skill) / SKILL_MD
        if body and upstream_md.exists() and body in upstream_md.read_text():
            print(f"  {APPEND_NAME}: upstream now contains this text; delete {APPEND_NAME} and regenerate.")
    print(f"  patches: {len(patches(skill))}, share of lines changed: {lines_changed_share(skill)}%, "
          f"conflicts resolved in this sync: {state['conflicts']}")


def cmd_status():
    cache = upstream_cache()
    for skill in mirrored_skills():
        commit = read_lock(skill)["commit"]
        if upstream_tree_id(cache, commit, skill) == upstream_tree_id(cache, UPSTREAM_BRANCH, skill):
            print(f"up to date  {skill}")
        else:
            newer = git_out(["rev-list", "--count", f"{commit}..{UPSTREAM_BRANCH}", "--",
                             f"{UPSTREAM_SKILLS_PATH}/{skill}"], cwd=cache)
            print(f"behind      {skill}: {newer} newer upstream commit(s)")
    listing = git_out(["ls-tree", "--name-only", f"{UPSTREAM_BRANCH}:{UPSTREAM_SKILLS_PATH}"], cwd=cache)
    for skill in listing.split():
        if not is_mirrored(skill):
            clash = " (name clashes with a local skill)" if generated_dir(skill).exists() else ""
            print(f"not mirrored {skill}{clash}")


def cmd_verify_upstream(changed_since=None):
    if changed_since:
        changed = git_out(["diff", "--name-only", f"{changed_since}...HEAD"], cwd=REPO_ROOT).splitlines()
        relevant = [f for f in changed if f.startswith("third_party/google-skills/") or f.endswith(f"/{LOCK_NAME}")]
        if not relevant:
            print("skip: this change touches no upstream copy or lock")
            return
    cache = upstream_cache()
    failures = []
    for skill in mirrored_skills():
        commit = read_lock(skill)["commit"]
        try:
            resolve(cache, commit)
            require_on_upstream_branch(cache, commit)
            with tempfile.TemporaryDirectory() as tmp:
                if not export_skill(cache, commit, skill, Path(tmp) / skill):
                    raise OverlayError(f"upstream has no {skill} at {commit[:12]}")
                problems = compare_trees(Path(tmp) / skill, copy_dir(skill))
            if problems:
                raise OverlayError(f"the copy differs from upstream at {commit[:12]}:\n  " + "\n  ".join(problems))
        except OverlayError as e:
            failures.append(f"{skill}: {e}")
    for msg in failures:
        print(f"FAIL {msg}\n", file=sys.stderr)
    if failures:
        raise SystemExit(EXIT_FAILURE)
    print(f"ok: {len(mirrored_skills())} upstream copies match upstream at their locked commits")


def require_mirrored(skill):
    if not is_mirrored(skill):
        raise OverlayError(f"{skill} is not a mirrored skill (no {LOCK_NAME} in its overlay)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync"); p.add_argument("skill"); p.add_argument("--ref")
    p = sub.add_parser("continue"); p.add_argument("skill")
    p = sub.add_parser("refresh"); p.add_argument("skill"); p.add_argument("--patch"); p.add_argument("--message")
    p = sub.add_parser("generate"); p.add_argument("skill")
    p = sub.add_parser("check"); p.add_argument("skills", nargs="*")
    sub.add_parser("status")
    p = sub.add_parser("verify-upstream"); p.add_argument("--changed-since")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "sync":
            cmd_sync(args.skill, args.ref)
        elif args.cmd == "continue":
            cmd_continue(args.skill)
        elif args.cmd == "refresh":
            cmd_refresh(args.skill, args.patch, args.message)
        elif args.cmd == "generate":
            cmd_generate(args.skill)
        elif args.cmd == "check":
            cmd_check(args.skills)
        elif args.cmd == "status":
            cmd_status()
        elif args.cmd == "verify-upstream":
            cmd_verify_upstream(args.changed_since)
    except OverlayError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAILURE
    return 0


if __name__ == "__main__":
    sys.exit(main())
