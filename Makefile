# Upstream skill overlay demo. Every target is a thin wrapper around scripts/skill_overlay.py.
PYTHON ?= python3
TOOL := $(PYTHON) scripts/skill_overlay.py

.PHONY: help skills-sync skills-continue skills-refresh skills-generate skills-check skills-status skills-verify-upstream test

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

skills-sync: ## Sync one skill to upstream (SKILL=name [REF=tag-or-commit]); adopts it if not yet mirrored
	$(TOOL) sync $(SKILL) $(if $(REF),--ref $(REF))

skills-continue: ## Resume a sync that stopped on a conflict (SKILL=name)
	$(TOOL) continue $(SKILL)

skills-refresh: ## Record edits to a generated skill as a patch (SKILL=name [MSG="..."] [PATCH=nnnn to fold])
	$(TOOL) refresh $(SKILL) $(if $(PATCH),--patch $(PATCH)) $(if $(MSG),--message "$(MSG)")

skills-generate: ## Rebuild a generated skill from its upstream copy and overlay (SKILL=name)
	$(TOOL) generate $(SKILL)

skills-check: ## Verify every mirrored skill equals upstream copy + overlay, and every copy matches its lock
	$(TOOL) check

skills-status: ## List mirrored skills upstream has moved past, and upstream skills not mirrored
	$(TOOL) status

skills-verify-upstream: ## Compare each upstream copy with upstream at its locked commit ([BASE=ref] skips if nothing upstream changed)
	$(TOOL) verify-upstream $(if $(BASE),--changed-since $(BASE))

test: ## Run the tool's scenario tests
	$(PYTHON) -m unittest discover -s scripts -p 'test_*.py' -v
