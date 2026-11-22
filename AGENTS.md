# AGENTS.md — review-gate

A head-pinned merge command that reads review records from the ledger and live GitHub state. See README.md for usage, stages and record rules.

License: MIT

Backup posture: none — a code repository with no live state or secrets; GitHub is the durability layer.

Release class: versioned — cut by hand (a `chore(release): vX.Y.Z` pull request, then an annotated tag and one GitHub Release from the changelog section) until release-please is set up for this repository

## Rules for working in this repo

- The version source is `pyproject.toml`. `CHANGELOG.md` holds one dated section per version, newest first.
- Tests run with one accepted ledger version (0.6.1, 0.6.2, 0.6.3 or 0.7.0) installed; `.github/workflows/ci.yml` runs each.
- The merge command never reads the pull request body as an input. Keep it that way.
