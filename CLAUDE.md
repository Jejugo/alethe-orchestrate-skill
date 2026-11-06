# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`alethe-orchestrate` is an Agent Skill (`skills/alethe/SKILL.md`) plus a Python runner that lets a lead
agent hand bounded work to other coding CLIs (Claude Code, Codex, Antigravity `agy`, Kimi, or custom
adapters), enforce budgets/quota, and report what each worker did and cost. It ships as a Claude Code
plugin, a Codex plugin, and a plain skill folder.

## Commands

```sh
python -m unittest discover -s tests -v                       # full suite (what CI runs)
python -m unittest tests.test_security -v                     # one module
python -m unittest tests.test_orchestrate.<Class>.<test_name>  # one test
python skills/alethe/scripts/orchestrate.py --version         # smoke check
python skills/alethe/scripts/orchestrate.py --help            # list subcommands
pip install pyyaml && python tools/check_playbooks.py          # validate playbooks.yaml (CI job)
```

There is no build step and no linter config. CI (`.github/workflows/ci.yml`) runs the tests on Ubuntu,
Windows and macOS with Python 3.9 and 3.13, so code must stay 3.9-compatible and cross-platform.

## Hard constraints

- **Runtime code uses the Python standard library only**: no third-party imports, no shell
  (`shell=True`), no network. The one exception is `scripts/jev.py` (optional Jev classifier,
  `urllib` only), loaded lazily and only when `policy.classifier.mode` is not `off`; keep all
  network code there. PyYAML is allowed only in `tools/check_playbooks.py`; the runner has its
  own minimal YAML reader (`_PlaybookYamlReader` / `parse_playbooks_yaml`).
- Python is optional for end users: `references/manual.md` describes the no-Python path, so behavior
  changes to the runner often need a matching manual equivalent there.
- Windows is a first-class target (job objects, `.cmd` shim handling, `%APPDATA%` paths). Files are LF
  (`.gitattributes`).
- Worker safety is a core feature (see CHANGELOG v24): env filtering, read-only verification via git
  snapshots, cwd/taskFile confinement, unsafe-flag detection for custom CLIs, verified PIDs on `stop`,
  internal git calls with hooks/fsmonitor disabled. Exceptions come only from the saved policy's
  `trust` block, never from a plan. Elevated permissions follow the same rule: a worker or phase
  asks via `permissions`, the user's `trust.maxPermissions` caps it (see `resolve_permissions`). Don't weaken these; `tests/test_security.py` covers them.

## Architecture

- `skills/alethe/SKILL.md` — the instructions the lead agent follows (command mapping, playbook
  choice, wave loop). Detailed guidance is split into `references/*.md` (routing, briefs, operations,
  providers, playbooks, onboarding, manual, diagram) and loaded on demand.
- `skills/alethe/scripts/orchestrate.py` — single-file runner (~6.7K lines) with an argparse CLI
  (`doctor`, `policy`, `playbook`, `editor`, `mode`, `scaffold`/`validate`/`dry-run`,
  `new`/`run`/`budget`/`note`/`stop`/`wait`, `status`/`report`/`mark`/`list`/`use`/`clean`, `spend`,
  `usage`, `hook`, …). Key flow: `validate_plan` → `build_invocation` (per-provider argv/flags) →
  `run_worker` (process start/watch, diff capture, read-only tripwire, `classify_result`) under a
  `BudgetTracker` and quota checks (`quota_refusal`, `quota_forecast`) → per-wave `state.json` and
  per-worker `outcome.json`/`result.md`/`diff.patch`.
- `skills/alethe/scripts/hook.py` — Claude Code hook (`hooks/hooks.json`, on `UserPromptSubmit` and
  `PostToolUse`). Must stay fast and never fail: it does cheap gating itself and imports
  `orchestrate.hook_output` only when something may print. Some helpers (e.g. `session_state_filename`,
  `orchestrations_home`) are duplicated from the runner and must be kept in sync.
- `skills/alethe/playbooks.yaml` — bundled playbooks, tiers, profiles, quota and passive settings.
  Users get an editable copy via `playbook --init`; `tools/check_playbooks.py` validates the schema and
  imports the runner to cross-check strategy caps.
- `skills/alethe/prices.json` — list prices used for spend estimates (reported CLI cost always wins).
- `skills/alethe/assets/editor.html`, `report.html` — offline single-page templates the runner fills
  (`write_editor`, `write_report_page`); the editor must also work without Python.
- `skills/alethe/schemas/plan.schema.json` — plan JSON schema (plan version 1).
- User state lives outside the repo: `~/.config/alethe/` (or `%APPDATA%\Alethe\`), overridable with
  `ALETHE_ORCHESTRATE_HOME`, `ALETHE_ORCHESTRATE_POLICY`, `ALETHE_ORCHESTRATE_PLAYBOOKS`;
  `ALETHE_PASSIVE=0` disables the default passive mode.

## Tests

Tests load the runner via `importlib` from its file path and drive real subprocesses against fake
CLIs (`tests/fake_*.py`) instead of real providers. They redirect `CODEX_HOME` and
`ALETHE_ORCHESTRATE_POLICY` to temp dirs; new tests that touch quota, policy or orchestration folders
must do the same so they never read or write the developer's real config.

## Releases and versioning

The version is the integer `metadata.version` in `skills/alethe/SKILL.md`. A release bumps it and
must keep these in sync (enforced by `test_plugin_manifests_and_readme_carry_the_skill_version`):

- `.codex-plugin/plugin.json` and `.agents/plugins/marketplace.json` → `"<N>.0.0"`
- the `skill-v<N>` badge in both `README.md` and `README.pt-BR.md`
- a new `CHANGELOG.md` entry

README changes should be mirrored in `README.pt-BR.md`.
