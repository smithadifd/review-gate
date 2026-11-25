# Changelog

## 0.4.0 — 2026-11-25

- A library caller may supply stage 8 through the keyword-only `gate.run(..., ci_stage=...)`; the command line never can (`--ci-stage=...` refuses `argument_unknown`). The selection is checked before stage 9; invalid shapes refuse `ci_stage_selection_invalid`. (#18)
- Stage 9 judges a selected path-filtered push workflow by its newest run on an ancestor of the default branch's tip when no later commit touches a path admitted by the tip's filter or the workflow's own file. All existing run checks still apply. A touching commit refuses `main_workflow_path_filtered`; unreadable, malformed, capped, non-ancestor or merge-commit reads refuse `main_filter_commits_unreadable`. Unfiltered workflows still refuse stale runs with `main_ci_run_stale`. (#19)
- Add `review-gate check`: run stages 0 to 10 and write nothing, with no write-ahead and no merge. A pass prints `would-merge <URL> head=<sha>` and exits 0; a refusal prints the merge's refusal and exits 2. (#21)
- Add a settlement module that checks settled and adjudicated sibling receipt chains over their verified parsed documents. Invalid chains refuse `receipt_sibling_chain_invalid`; invalid adjudication or settlement content refuses `receipt_adjudication_invalid` or `receipt_settlement_invalid`. Group settles and reparses refuse `receipt_settlement_group_unsupported` and `receipt_reparse_unsupported`. The module ships inert: nothing calls it yet, and the merge still refuses a replacing record in a deciding slot with `receipt_settlement_unsupported`. Wiring is reserved for v0.4.1. (#20)

Install with `@v0.4.0`.

## 0.3.1 — 2026-11-24

- Document the required `ready_for_review` workflow trigger, with a copyable YAML example and the stage 8 refusal when no run starts after a draft is marked ready. (#16)

Install with `@v0.3.1`.

## 0.3.0 — 2026-11-24

- Document the receipt fields, copyable approving and single-nit JSON examples, and shape refusals at the record step. Exercise both README examples through the real record command and ledger writer. (#15)
- Add `review-gate item --pr <URL> [--lookup]`: find the existing pull request item or atomically create one deterministic ticket and PR link. Retries and concurrent calls do not duplicate events. Lookup is read-only and exits 3 with `pr_item_missing` when absent; ambiguity, source conflicts and competing links refuse without writes. (#12)
- Replace manual item creation/linking guidance with the item verb. Raw ledger PR linking still has no cross-item uniqueness guarantee. (#12)
- Merge hardening derives every unresolved harden id from the verified receipt document, including a harden collapsed into a block on the same id. Its exact-source child follow-up remains required even with a standing block waiver. Event/document harden mismatches refuse `receipt_document_inconsistent`; missing, by-path, altered or underivable documents never fall back to event findings. (#13)

Install with `@v0.3.0`.

## 0.2.2 — 2026-11-23

- **A carried finding whose prior receipt holds several entries on its root records and merges when they all reach one origin.** A runner can record two entries on one root at different lines in one round, for example a carried block and a carried harden of one earlier finding. The next round's carried entry on that root used to refuse `receipt_carried_root_missing` in the writer and at the merge port. Now the writer and the merge port's open-bug and hardening reads follow every entry on the root through its `carried_from` link, over the stored receipt events alone, and accept only when every branch ends at one origin finding id. At import, `prepare_receipt` follows the link one hop when the prior holds one entry on the root; the full walk runs at write and at the merge port. That id is the waiver id and the hardening source, and the history is every receipt the walk visited, newest first. Nothing is chosen by cell, line or description. Entries that reach different origins refuse `receipt_carried_root_missing` with the detail `<finding id>: root matches several entries that do not reach one origin`; no entry on the root, a link to no receipt, and a cycle refuse as before. (#10)
- **One waiver covers every block on one root, and one follow-up item every harden on it.** Every entry on a root shares its origin, so two carried blocks on one root need one waiver and two carried hardens on it need one follow-up item. v0.2.1 already behaved this way one round earlier, where each entry reached the origin through a single match. (#10)

Install with `@v0.2.2`.

## 0.2.1 — 2026-11-23

The receipt writer now records the receipt documents the review runner publishes and its merge reader counts.

- **Carried hardens and nits enter no cell.** `cells.block` is the own block findings plus every `blocks_carried` entry, `cells.harden` and `cells.nit` count own findings only, and `open_bugs` equals `cells.block`. A document that disagrees, or whose event would hold a different number of block entries than its `open_bugs`, refuses `receipt_document_inconsistent`. So does an entry of `hardened_carried`, `blocks_carried` or `nits_carried` whose cell is not its list's (for `hardened_carried` this is stricter than the runner's reader). (#8)
- **One event entry per finding id.** The same id in two lists, such as a nit finding that matched a block still carried, is one entry with the most severe cell, carried when any of its entries is. An id twice in one list, and two block entries on one id, refuse `receipt_finding_duplicate`. A harden entry that shares an id with a block is recorded as the block only. (#8)
- **A carried harden is resolved only by a located match**, on exact file, line and description; an unlocated carried harden stays open. (#8)
- **A settled sibling of an adjudication records.** A document may name `settled_from` and `adjudicated_from` together. It supersedes the `settled_from` receipt, and `adjudicated_from` must name the receipt that one supersedes, else `receipt_supersedes_mismatch`. Any other pair of sources, or all three, refuses `receipt_document_shape`. (#8)
- **Some carried entries need no `residual_root`.** A `blocks_carried` entry, a `hardened_carried` entry (located or not), and an unlocated `nits_carried` entry, may omit it or give null; the entry links to the prior receipt through the root of its own id, and a prior receipt without that root still refuses `receipt_carried_root_missing`. A located `nits_carried` entry and a finding with `residual_of` still need one. (#8)
- **A finding with a file and no line records.** Its id is `/unlocated/<file>:1#<root>`, a function of the file and the description only, so a finding and its carried copy share it; the same file and description twice in one list refuses `receipt_finding_duplicate`. An entry with neither keeps the `/unlocated:1#<root>` form. An empty file, whitespace in a file, and a line of 0 or a non-integer still refuse. (#8)

Install with `@v0.2.1`.

## 0.2.0 — 2026-11-22

review-gate now accepts the ledger-hooks versions the public harness default serves, as an exact set, and is installable with a pinned git install.

- **Accepted ledger-hooks versions are an exact set: 0.6.1, 0.6.2, 0.6.3 and 0.7.0.** The manifest's version must be a member by exact string equality, and the imported package's version must equal the manifest's. Anything else, including a neighbour such as 0.7.1, a prefix or a manifest and package that differ, refuses `ledger_version_unsupported`, which now lists the accepted versions. Receipts, credential scanning, the doc and grants verbs, `CliError` and the review and merge-record schemas were checked as unchanged across the four. (#6)
- **Known limit with ledger 0.7.0.** On a loop-managed item whose pull request was linked by the loop, the merge succeeds and is recorded, but closing the items is refused (`reserved-transition:loop-close`): the merge prints `items_close_failed` and exits 0, and `recover` keeps refusing until the loop closes the item. Nothing reaches this until the loop links pull requests. (#6)
- **CI runs the whole suite once per accepted ledger version** (a `ledger-version` matrix axis next to the Python version). (#6)
- **README gains an Install section**: the pinned git install, the ledger-hooks plugin it reads, and the accepted versions. (#6)

Install with `@v0.2.0`.

## 0.1.0 — 2026-11-22

The first release: a head-pinned merge command that reads review records from the ledger and live GitHub state, plus the verbs that record and recover them. It needs ledger v0.6.1, loaded from the ledger-hooks plugin root on `PYTHONPATH`, and an absolute `LEDGER_DB`.

- **`review-gate merge <PR URL> <full head sha> --items ID[,ID...]`** merges a pull request only at the exact head it is given, and only when every stage passes on review records and live GitHub reads. It never reads the pull request body as an input. `--remediation=` names the jobs that are red on the default branch, for a merge onto a red base. (#1, #2)
- **Review records come from the ledger.** The records port reads review receipts and merge records through fresh read-only connections, only after stage 1 admits the database and the ledger plugin root. Without `LEDGER_DB` the command refuses `records_unavailable`; a relative spelling refuses `ledger_db_relative`. (#2)
- **`review-gate recover <PR URL> <full head sha> --items ID[,ID...]`** records a merge that already completed, and never merges. (#2)
- **`review-gate import --store <absolute directory> --pr <PR URL>`** imports a review store's receipts as ledger records. It checks every document before the first write, writes them in dependency order, and reads the item back. A document already recorded for its slot is skipped whatever recorded it, so a rerun completes with zero new events. (#3)
- **`review-gate record`** records one writer document read from standard input, for a person who runs each review lane by hand. It prints `written` or `already_recorded` and exits 0, or exits 2 with the refusal name; the writer's own decision under its lock chooses the word. (#4)

Install with `@v0.1.0`.
