# Changelog

## Unreleased

- **Accepted ledger-hooks versions are an exact set: 0.6.1, 0.6.2, 0.6.3 and 0.7.0.** The manifest's version must be a member by exact string equality, and the imported package's version must equal the manifest's. Anything else, including a neighbour such as 0.7.1, a prefix or a manifest and package that differ, refuses `ledger_version_unsupported`, which now lists the accepted versions. Receipts, credential scanning, the doc and grants verbs, `CliError` and the review and merge-record schemas were checked as unchanged across the four.
- **Known limit with ledger 0.7.0.** On a loop-managed item whose pull request was linked by the loop, the merge succeeds and is recorded, but closing the items is refused (`reserved-transition:loop-close`): the merge prints `items_close_failed` and exits 0, and `recover` keeps refusing until the loop closes the item. Nothing reaches this until the loop links pull requests.
- **CI runs the whole suite once per accepted ledger version** (a `ledger-version` matrix axis next to the Python version).
- **README gains an Install section**: the pinned git install, the ledger-hooks plugin it reads, and the accepted versions.

## 0.1.0 — 2026-11-22

The first release: a head-pinned merge command that reads review records from the ledger and live GitHub state, plus the verbs that record and recover them. It needs ledger v0.6.1, loaded from the ledger-hooks plugin root on `PYTHONPATH`, and an absolute `LEDGER_DB`.

- **`review-gate merge <PR URL> <full head sha> --items ID[,ID...]`** merges a pull request only at the exact head it is given, and only when every stage passes on review records and live GitHub reads. It never reads the pull request body as an input. `--remediation=` names the jobs that are red on the default branch, for a merge onto a red base. (#1, #2)
- **Review records come from the ledger.** The records port reads review receipts and merge records through fresh read-only connections, only after stage 1 admits the database and the ledger plugin root. Without `LEDGER_DB` the command refuses `records_unavailable`; a relative spelling refuses `ledger_db_relative`. (#2)
- **`review-gate recover <PR URL> <full head sha> --items ID[,ID...]`** records a merge that already completed, and never merges. (#2)
- **`review-gate import --store <absolute directory> --pr <PR URL>`** imports a review store's receipts as ledger records. It checks every document before the first write, writes them in dependency order, and reads the item back. A document already recorded for its slot is skipped whatever recorded it, so a rerun completes with zero new events. (#3)
- **`review-gate record`** records one writer document read from standard input, for a person who runs each review lane by hand. It prints `written` or `already_recorded` and exits 0, or exits 2 with the refusal name; the writer's own decision under its lock chooses the word. (#4)

Install with `@v0.1.0`.
