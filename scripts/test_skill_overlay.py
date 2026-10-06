"""Scenario tests for scripts/skill_overlay.py.

Each test builds a throwaway upstream repository (tags v1-v5, like the demo upstream) and a
throwaway downstream copy of this repository, then runs the tool as a contributor would.
Run: make test
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "skill_overlay.py"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

BASICS_V1 = """# Basics

## Cluster Credentials

Always specify the cluster's region when fetching credentials:

```bash
gcloud container clusters get-credentials CLUSTER --region=REGION --quiet
```

## Checking Node Health

1. List the nodes.
2. Check that the node can recieve new pods.
"""


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=dict(os.environ, **GIT_ENV),
                          capture_output=True, text=True, check=check)


def build_upstream(root):
    """v1 base; v2 nearby edit; v3 adopts the typo fix; v4 edits the patched line; v5 new skill."""
    skills = root / "skills" / "cloud"
    (skills / "basics").mkdir(parents=True)
    (skills / "storage").mkdir(parents=True)
    (skills / "basics" / "SKILL.md").write_text(BASICS_V1)
    (skills / "storage" / "SKILL.md").write_text("# Storage\n\nCreate a PVC.\n")
    git(root, "init", "-q", "-b", "main")

    def commit(tag):
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", tag)
        git(root, "tag", tag)

    commit("v1")
    md = skills / "basics" / "SKILL.md"
    md.write_text(md.read_text().replace("Always specify the cluster's region",
                                         "Always pass the cluster's region explicitly"))
    commit("v2")
    md.write_text(md.read_text().replace("recieve", "receive"))
    commit("v3")
    md.write_text(md.read_text().replace("--region=REGION --quiet", "--region=REGION --project=PROJECT --quiet"))
    commit("v4")
    (skills / "network").mkdir()
    (skills / "network" / "SKILL.md").write_text("# Network\n")
    commit("v5")


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream = self.tmp / "upstream"
        self.upstream.mkdir()
        build_upstream(self.upstream)
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(TOOL, self.repo / "scripts" / "skill_overlay.py")
        git(self.repo, "init", "-q", "-b", "main")
        self.env = dict(os.environ, SKILL_OVERLAY_UPSTREAM=str(self.upstream), **GIT_ENV)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_tool(self, *args, expect=0):
        res = subprocess.run([sys.executable, "scripts/skill_overlay.py", *args], cwd=self.repo,
                             env=self.env, capture_output=True, text=True)
        self.assertEqual(res.returncode, expect, res.stdout + res.stderr)
        return res.stdout + res.stderr

    def skill(self, name="basics"):
        return self.repo / "agents" / "platform" / "skills" / name / "SKILL.md"

    def overlay(self, name="basics"):
        return self.repo / "agents" / "platform" / "skill-overlays" / name

    def adopt_with_two_patches(self):
        self.run_tool("sync", "basics", "--ref", "v1")
        self.run_tool("sync", "storage", "--ref", "v1")
        md = self.skill()
        md.write_text(md.read_text().replace("--region=REGION --quiet", "--location=LOCATION --quiet"))
        self.run_tool("refresh", "basics", "--message", "use location")
        md.write_text(md.read_text().replace("recieve", "receive"))
        self.run_tool("refresh", "basics", "--message", "fix typo")
        self.assertEqual([p.name for p in sorted(self.overlay().glob("*.patch"))],
                         ["0001-use-location.patch", "0002-fix-typo.patch"])

    def test_check_passes_after_refresh(self):
        self.adopt_with_two_patches()
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_hand_edit_without_patch_fails_check(self):
        self.adopt_with_two_patches()
        self.skill().write_text(self.skill().read_text() + "extra line\n")
        out = self.run_tool("check", expect=1)
        self.assertIn("skills-refresh", out)

    def test_hand_edit_to_upstream_copy_fails_checksum(self):
        self.adopt_with_two_patches()
        copy = self.repo / "third_party" / "google-skills" / "storage" / "SKILL.md"
        copy.write_text(copy.read_text() + "x\n")
        self.assertIn("edited by hand", self.run_tool("check", expect=1))

    def test_forged_lock_fails_upstream_comparison(self):
        self.adopt_with_two_patches()
        copy = self.repo / "third_party" / "google-skills" / "storage" / "SKILL.md"
        copy.write_text(copy.read_text() + "x\n")
        sys.path.insert(0, str(self.repo / "scripts"))
        lock = self.overlay("storage") / "upstream.lock"
        commit = lock.read_text().splitlines()[0].split(": ")[1]
        res = subprocess.run([sys.executable, "-c",
                              "import skill_overlay as m;"
                              f"m.write_lock('storage','{commit}',m.tree_sha256(m.copy_dir('storage')))"],
                             cwd=self.repo / "scripts", env=self.env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.run_tool("generate", "storage")
        self.assertIn("ok: 2", self.run_tool("check"))
        self.assertIn("differs from upstream", self.run_tool("verify-upstream", expect=1))

    def test_nearby_upstream_edit_merges(self):
        self.adopt_with_two_patches()
        self.run_tool("sync", "basics", "--ref", "v2")
        text = self.skill().read_text()
        self.assertIn("Always pass the cluster's region explicitly", text)
        self.assertIn("--location=LOCATION", text)
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_adopted_patch_is_retired(self):
        self.adopt_with_two_patches()
        out = self.run_tool("sync", "basics", "--ref", "v3")
        self.assertIn("retired 0002-fix-typo.patch", out)
        self.assertEqual([p.name for p in self.overlay().glob("*.patch")], ["0001-use-location.patch"])
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_edit_to_patched_line_stops_then_continues(self):
        self.adopt_with_two_patches()
        out = self.run_tool("sync", "basics", "--ref", "v4", expect=2)
        self.assertIn("CONFLICT: 0001-use-location.patch", out)
        scratch = self.repo / ".skill-sync" / "basics" / "SKILL.md"
        lines, keep = [], True
        for line in scratch.read_text().splitlines(keepends=True):
            if line.startswith("<<<<<<<"):
                lines.append("gcloud container clusters get-credentials CLUSTER --location=LOCATION --project=PROJECT --quiet\n")
                keep = False
            elif line.startswith(">>>>>>>"):
                keep = True
            elif keep:
                lines.append(line)
        scratch.write_text("".join(lines))
        self.run_tool("continue", "basics")
        self.assertIn("--location=LOCATION --project=PROJECT", self.skill().read_text())
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_status_and_adopting_a_new_skill(self):
        self.adopt_with_two_patches()
        out = self.run_tool("status")
        self.assertIn("behind      basics", out)
        self.assertIn("not mirrored network", out)
        self.run_tool("sync", "network")
        self.assertIn("ok: 3", self.run_tool("check"))

    def test_local_skill_is_never_touched_and_blocks_adoption_of_its_name(self):
        local = self.skill("network")
        local.parent.mkdir(parents=True)
        local.write_text("# our own network skill\n")
        self.run_tool("sync", "basics", "--ref", "v1")
        self.assertIn("name clashes with a local skill", self.run_tool("status"))
        self.assertIn("not mirrored", self.run_tool("sync", "network", expect=1))
        self.assertEqual(local.read_text(), "# our own network skill\n")
        self.assertIn("ok: 1", self.run_tool("check"))

    def test_fold_into_existing_patch_and_overlap_warning(self):
        self.adopt_with_two_patches()
        md = self.skill()
        md.write_text(md.read_text().replace("--location=LOCATION --quiet", "--location=$LOCATION --quiet"))
        self.assertIn("lines that 0001-use-location.patch introduced", self.run_tool("refresh", "basics"))
        for p in self.overlay().glob("0003-*.patch"):
            p.unlink()
        self.run_tool("generate", "basics")
        md.write_text(md.read_text().replace("--location=LOCATION --quiet", "--location=$LOCATION --quiet"))
        self.run_tool("refresh", "basics", "--patch", "0001")
        self.assertEqual(len(list(self.overlay().glob("*.patch"))), 2)
        self.assertIn("--location=$LOCATION", (self.overlay() / "0001-use-location.patch").read_text())
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_concurrent_unrelated_patches_merge_without_conflict(self):
        self.adopt_with_two_patches()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "checkout", "-q", "-b", "alice")
        md = self.skill()
        md.write_text(md.read_text().replace("# Basics", "# Basics (alice)"))
        self.run_tool("refresh", "basics", "--message", "alice title")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "alice")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "checkout", "-q", "-b", "bob")
        md.write_text(md.read_text().replace("1. List the nodes.", "1. List the nodes (bob)."))
        self.run_tool("refresh", "basics", "--message", "bob step")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "bob")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-edit", "alice")
        merged = git(self.repo, "merge", "-q", "--no-edit", "bob", check=False)
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_append_adopted_upstream_is_reported(self):
        self.run_tool("sync", "storage", "--ref", "v1")
        (self.overlay("storage") / "append.md").write_text(
            "<!-- kube-agents: local addition -->\n\nNew closing note.\n")
        self.run_tool("generate", "storage")
        upstream_md = self.upstream / "skills" / "cloud" / "storage" / "SKILL.md"
        upstream_md.write_text(upstream_md.read_text() + "\nNew closing note.\n")
        git(self.upstream, "commit", "-q", "-am", "adopt note")
        self.assertIn("upstream now contains this text", self.run_tool("sync", "storage"))


if __name__ == "__main__":
    unittest.main()
