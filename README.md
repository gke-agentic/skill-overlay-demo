# skill-overlay-demo

A toy repository that demonstrates the
[upstream skill overlays design](https://github.com/gke-labs/kube-agents/pull/2378) for
kube-agents. It mirrors two toy skills from
[`skill-overlay-demo-upstream`](https://github.com/gke-agentic/skill-overlay-demo-upstream),
which plays the part of `google/skills`, and keeps local changes to them as patch files.

`scripts/skill_overlay.py` is a working prototype of the tool the design proposes, with
scenario tests (`make test`).

## Layout

| Layer | Path | Holds |
| --- | --- | --- |
| ① Upstream copy | `third_party/google-skills/<skill>/` | Upstream's `skills/cloud/<skill>/`, byte-identical at the pinned commit |
| ② Overlay | `agents/platform/skill-overlays/<skill>/` | `upstream.lock` (commit + sha256), `NNNN-<slug>.patch` files, optional `append.md` |
| ③ Generated skill | `agents/platform/skills/<skill>/` | ① with ② applied, committed. This is what an agent would read. |

What is in it today:

- `gke-demo-basics`: pinned at upstream `v1`, with two patches (`0001-use-location-flag`,
  `0002-fix-recieve-typo`) and an `append.md` footer.
- `gke-demo-storage`: pinned at `v1`, no local changes (its overlay holds only the lock).
- `gke-local-runbook`: a skill this repository wrote. It has a `gke-` name but no lock, so the
  sync and the check ignore it.

## Commands

| Command | Does |
| --- | --- |
| `make skills-check` | Verifies each copy against its lock, rebuilds every mirrored skill, and compares with ③. Runs in CI. |
| `make skills-verify-upstream` | Compares each copy with upstream at its locked commit. Runs in CI only when a copy or lock changed. |
| `make skills-refresh SKILL=x MSG="..."` | Records your edit to ③ as a new patch (`PATCH=0001` folds it into an existing one). |
| `make skills-generate SKILL=x` | Rebuilds ③ after you edit a patch or `append.md`. |
| `make skills-sync SKILL=x [REF=v3]` | Moves one skill to upstream's latest (or a tag/commit), rebasing its patches. Adopts the skill if it is not mirrored yet. |
| `make skills-continue SKILL=x` | Resumes a sync that stopped on a conflict. |
| `make skills-status` | Lists skills upstream has moved past, and upstream skills not mirrored. |

Requires `git` and Python 3.12 or newer. The tool fetches the upstream repository over HTTPS.

## Demo walkthrough

Run these in a fresh clone. Each step can be undone with `git checkout -- . && git clean -fd`.

### 1. Change a skill: edit, refresh, done

```bash
sed -i.bak 's/1. List the nodes/1. List the nodes with `-o wide`/' agents/platform/skills/gke-demo-basics/SKILL.md && rm agents/platform/skills/gke-demo-basics/SKILL.md.bak
make skills-check          # fails: the edit is not recorded as a patch
make skills-refresh SKILL=gke-demo-basics MSG="Show node IPs in the listing"
make skills-check          # passes: 0003-show-node-ips-in-the-listing.patch now records it
```

### 2. Sync with a nearby upstream edit: merges on its own

Upstream `v2` reworded the line two lines above the command our `0001` patch changes.

```bash
make skills-sync SKILL=gke-demo-basics REF=v2
grep -n -A4 'Cluster Credentials' agents/platform/skills/gke-demo-basics/SKILL.md
# upstream's new wording and our --location both survive
```

### 3. Upstream adopts one of our patches: retired automatically

Upstream `v3` fixed the same typo our `0002` patch fixes.

```bash
make skills-sync SKILL=gke-demo-basics REF=v3
# prints: retired 0002-fix-recieve-typo.patch (upstream now carries it). Why: ...
ls agents/platform/skill-overlays/gke-demo-basics/
```

### 4. Upstream edits our patched line: the sync stops for a person

Upstream `v4` changed the `gcloud` command itself, the line `0001` rewrites.

```bash
make skills-sync SKILL=gke-demo-basics REF=v4
# CONFLICT: 0001-use-location-flag.patch overlaps upstream's change in: SKILL.md
# Edit .skill-sync/gke-demo-basics/SKILL.md so the command reads:
#   gcloud container clusters get-credentials CLUSTER --location=LOCATION --project=PROJECT --quiet
make skills-continue SKILL=gke-demo-basics
make skills-check
```

The patch keeps its file name and `Why:` header; only its content is refreshed.

### 5. See what is behind, and adopt a new upstream skill

```bash
make skills-status
# behind      gke-demo-basics: 3 newer upstream commit(s)
# behind      gke-demo-storage: 1 newer upstream commit(s)
# not mirrored gke-demo-network
make skills-sync SKILL=gke-demo-network
```

### 6. The guardrails

```bash
echo "hand edit" >> third_party/google-skills/gke-demo-storage/SKILL.md
make skills-check            # fails: the copy no longer matches the sha256 in upstream.lock
```

If someone also rewrites the lock's sha256 to match, `make skills-check` passes, but
`make skills-verify-upstream` fails: the copy differs from upstream at the locked commit.

## Demo pull requests

The open pull requests show CI on each case: an unrecorded edit (red), a hand edit to the
upstream copy (red), and a sync that retires a patch (green).
