# review-gate

A head-pinned merge command for GitHub pull requests. It merges a pull request only at the exact
head it is given, only when every stage below passes on review records and live GitHub reads, and
it never reads the pull request body as an input.

**Status: ledger-backed merges and recovery.** Set `LEDGER_DB` to an absolute database file;
unset or empty refuses `records_unavailable`, and a relative spelling refuses `ledger_db_relative`.
The variable names a file, never policy. The runtime loads ledger v0.6.1 from the ledger-hooks
plugin root on `PYTHONPATH`, not from a checkout-local `ledger/` or a vendored copy. The plugin
root is an absolute `PYTHONPATH` entry (or its `plugins/ledger-hooks`), never the current directory
or `sys.path[0]`, and it must pass stage 1's directory checks (git trees, lane roots, temp roots,
sandbox write roots) before anything in it is read or imported. No database opens before stage 1 admission;
read stages use fresh read-only connections. Receipt writing and indexed-store import use the same admission.

## Usage

```sh
review-gate merge <PR URL> <full head sha> --items ID[,ID...] [--remediation=JOB[,JOB...]]
python3 -m review_gate merge ...
review-gate recover <PR URL> <full head sha> --items ID[,ID...]
review-gate import --store <absolute directory> --pr <PR URL>
review-gate record < one-document.json
```

- `<PR URL>` is `https://github.com/<owner>/<repo>/pull/<N>`, matched whole: no trailing slash, no
  `/files`, no query or fragment, no leading zero.
- `<full head sha>` is 40 lowercase hex characters. It is compared, never folded or shortened.
- `--items` is required: the ledger items the merge closes, comma separated.
- `--remediation=` names exactly the jobs that are red on the default branch, for a merge onto a red
  main; it also needs the owner's grant `grant:remediation:<owner>/<repo>#<N>:<head>`.

Anything outside this grammar refuses by name before any read.

## Output

| Exit | Meaning | Output |
|---|---|---|
| 0 | merged and recorded | one stdout line: `merged <URL> head=<sha> merge_commit=<oid>` |
| 2 | refused; nothing merged | one stderr line that starts with the refusal's name: `<name>: <detail>` |
| 4 | a merge call was made and its outcome is not confirmed in the records | one stderr line: `merge_unconfirmed: ...` or `merge_finish_unrecorded: ...` |

An error that escapes a stage is a named refusal (`internal_error`), never a traceback.
Each `gh` process has a 60-second deadline; a stalled process refuses `gh_timeout`.

## The stages

They run in this order, and the first refusal stops the run.

| # | Stage | What it requires |
|---|---|---|
| 0 | arguments | the grammar above |
| 1 | store | the ledger database the records port names, its `-wal` and `-shm` files and its documents folder are not where a lane can write: no hand-back, `.omp`, git tree, registered lane root, lane settings write root, temp root or sandbox write root overlaps the database's directory (compared by file identity); the database has one link and is the caller's |
| 2 | state read 1 | the head equals the sha; the pull request is open, not a draft, based on the default branch and not from a fork; the comparison's file list and its digest |
| 3-7 | records | the item, the receipts, open bugs, hardening and the round cap, each answered by the records port |
| 8 | CI | every run bound to this pull request by its trigger fields and created after READY is green at job grain (a skipped job is not success), and every workflow the pull request's paths select has such a run; with no workflow file, `unchecked_head` unless `grant:no-ci:<owner>/<repo>` stands |
| 9 | main is green | for each selected workflow, by its file path: main's newest push run is on the default branch's tip and green at job grain, a skipped job only with a valid main-tree proof; red jobs only with an exact `--remediation=` and its grant |
| 10 | recheck | the file list and the state read again, equal on every field; stages 3 to 7 and every grant asked again and equal; the default branch's tip is in the head and is the tip stage 9 judged |
| 11 | merge | the write-ahead intent, then `gh pr merge <URL> --squash --match-head-commit <sha>`, then a read showing MERGED at that head, then the finished record, then the items closed |

The module docstrings carry each stage's full rules and refusal names.

## Record rules and recovery

Two deciding receipts from distinct lanes are required. Only reserved `review.receipt` events
count, bound to repository, pull request, head and files digest. Every inline receipt document
is verified by the ledger, not parsed as authority. Only the highest round per lane decides;
a replacing record anywhere in that deciding slot refuses `receipt_settlement_unsupported`.
Approved and changes-requested receipts count;
an empty changes request refuses. Crashed and unparseable newest rounds never fall back.
Block findings each need their own standing waiver, harden findings each need an exact-source
child outside `--items`, and counted rounds past three need a standing grant or a current cite.

The database's adjacent `config.json` may set `"review_second_family": "anthropic"` (also
`openai` or `google`). It can only add a family requirement. A malformed, unreadable or symlinked
file refuses. Model families are derived from anchored provider/model forms in `ledger_records.py`;
unknown models never satisfy this requirement. A standing head-bound exception can lift it.

Stage 11 commits `review.merge-intent` before invoking GitHub and records `review.merge-done`
only after confirming MERGED at the supplied head. Both records contain key, pull request and
head; ledger v0.6.1 has no merge-commit field. The commit remains on GitHub and stdout.
Closing an already closed item is a no-op; close failures can be retried through `recover`.

`recover` never merges. After grammar and store admission it resolves the current item and
requires a live MERGED read at the pinned head. Started writes done, then closes the items;
finished only closes; absent refuses `merge_intent_missing`. A crash after intent while the
pull request remains open refuses `merge_intent_unreconciled` on every retry: merge by hand
then recover, or review a new head. There is no reset-to-absent operation.


## Writing and importing receipts

`review_gate.receipt_writer.write_receipt(document, *, records, store_check)` takes document bytes,
not a path or decided fields. One derivation supplies verdict, cells, ids, counts and model family.
Cells are those recorded by the runner, not independently classified by a rubric.
The lane, served model, tier, cap flag and document remain the runner's word: agreement and binding
do not prove that a review happened. The writer attaches the same bytes and writes one receipt
through the ledger's transaction, with constant attribution. A live write never sets `imported_from`.
Non-finite numbers (including exponent overflow), repeated keys and escaped unpaired surrogates
refuse before any write. If the database disappears after admission, the writer refuses rather
than creating it.

The item must already exist and link the pull request. With the ledger CLI, create it with
`ledger new --type ticket --title <title> --source pr:<owner>/<repo>#<N>`, then
`ledger link <item> --rel pr --target <URL>`. Neither writer nor importer creates or links items.

Import checks the admitted store's seven-column index, exact bytes, repository and number before
writing. A document whose digest the item already records for the same lane, round and head is
already recorded, whether the writer or an earlier import recorded it and whatever `base` it was
recorded with; it is skipped, not written again. Import checks every other selected document first,
then writes rounds and named siblings in dependency order. Receipts the item recorded for the pull
request before the run, by the writer or from another store, are kept whether or not the index lists
them: the read-back requires the recorded digest set to equal that earlier set plus the index's
selected set. `imported_from` names the store as given. Imported receipts count exactly as live
receipts. Each imported
`base` is the merge base GitHub compare reported at import time; it is not what the review ran
against. Import is not atomic across receipts: a stop can leave a partial import; an unchanged
rerun completes it without repeating events. No document supplies grants, waivers, cites or items.

### Walk step: record one lane result

`review-gate record` reads one writer document from standard input and records it with
`write_receipt`. Run each lane, then pipe each lane's document in once:

```sh
export LEDGER_DB=/absolute/path/to/ledger.db
export PYTHONPATH=/absolute/path/to/ledger-hooks-plugin-root
review-gate record < sol-round-1.json
review-gate record < claude-round-1.json
```

Preconditions, none of which the verb supplies: `LEDGER_DB` is an absolute database file (unset
refuses `records_unavailable`); the ledger plugin root is an absolute `PYTHONPATH` entry; and the
pull request's item already exists and links the pull request, because the verb never creates or
links items (`pr_item_missing` refuses).

The argv is exactly `record`: no flag and no operand, so `records`, `record x` and `record --flag`
refuse (`command_unknown`, `argument_unknown`) before standard input is read. Standard input is
read as bytes, at most 65536 bytes plus one; a longer input refuses `receipt_doc_too_large` and is
never cut to fit, and empty or closed input refuses `record_empty`. The verb adds no field and
derives nothing: verdict, cells, family, the prior receipt chain and idempotency are the writer's, so
every refusal the writer already makes (family mismatch, count disagreement, a round restart, a
second document for an occupied slot, a credential shape) stays refused, with no event written.

Exit 0 prints one line, `written <PR URL> round=<n> lane=<lane>` or, when the same bytes were
already recorded for that slot, `already_recorded <PR URL> round=<n> lane=<lane>`; a rerun writes
nothing. As with `import`, bytes already recorded for their slot are that document's receipt
whatever their provenance, so a document first recorded by `import` is `already_recorded` and the
writer is not called. The written or already-recorded word comes from the writer's own decision
under its lock, so a concurrent record of the same bytes reports `already_recorded`.

Exit 2 prints `<refusal name>: <detail>` on standard error, or `record_failed: <Type>` for any
other error. There are no other exit codes.

Decision: settlement derivation is separate from receipt storage. Until it exists, a replacing
record in a deciding slot refuses rather than dropping findings on a sibling's word.

## Residual risks

- GitHub's merge call takes an expected head and no expected base. A retarget, a default-branch
  change or a move of the default branch after the final reads and before GitHub merges is not seen.
- A pull request flipped to draft and back to ready between stage 8 and the merge moves READY past
  the runs stage 8 counted, and the draft flag reads the same in both state reads.
- The main-tree proof reader checks the marker step's grammar and conclusion only; it does not check
  that the head or the run the marker names is the one the proof was made for.
- A grant withdrawn or a new receipt written after the final recheck and before the merge is not seen.
- A second item linking the pull request after intent makes later writes refuse `pr_item_ambiguous`;
  links cannot be removed through this command.
- Tier is not judged against the change: a lighter-tier receipt counts for any change. No tier or
  path lowers a requirement. Settlements remain unsupported: a replacing record in a deciding slot
  refuses `receipt_settlement_unsupported`; replacing records at older heads do not decide.
- The adjacent family configuration is not an owner-authenticated record. Picker capture is defended
  by an external rail, not by the ledger against an in-process caller.
- Case and Unicode spellings of a lane root are compared by file identity, which only matters on a
  case-insensitive filesystem such as macOS. The hosted CI runs on Linux, so those cases are proved
  by a macOS run of the store tests, not by CI.
- A process running as the same user can write the ledger database directly; the records design does
  not defend that boundary.

## Development

```sh
python3 -m pip install pytest PyYAML
python3 -m pip install 'ledger @ git+https://github.com/smithadifd/ledger.git@v0.6.1'
python3 -m pytest -q -p no:cacheprovider
python3 -I scripts/check-no-person.py
```

The runtime needs Python 3.11 or later, `gh` on `PATH`, and PyYAML; without PyYAML the command
refuses `pyyaml_missing`.

This repository is public; CI runs on GitHub-hosted runners. Every job retains a condition that
disables it if the repository becomes private. The no-person scan reads its person and owner words
from the `NO_PERSON_DENY` secret and fails in CI when it is unset. It checks the index and history.
History means commits reachable from HEAD: their message, author, committer, paths and blobs;
not read: tag objects, other refs, commit header fields other than author and committer, and bytes
that are not UTF-8 text lines. The same path-scoped allowances apply to file contents.
Index path/blob pairs are scanned only once; staged changes do not hide committed content.
Whenever any name rule fires (shape, secret or deny), every finding under that name uses a hidden label:

- Index: `tracked path #<n>: path name: <rule>`, `tracked path #<n>: deny-list (path name)`,
  `tracked path #<n>:<line>: <rule>` or `tracked path #<n>: unreadable (<reason>)`.
- History: `commit <sha12>: historical path (redacted): path name: <rule>`,
  `commit <sha12>: historical path (redacted): deny-list (path name)`,
  `commit <sha12>: historical path (redacted):<line>: <rule>` or
  `commit <sha12>: historical path (redacted): unreadable (<reason>)`.

Shallow repositories refuse `history_shallow`; fetch the full history before running the scan locally.

## License

MIT; see [LICENSE](LICENSE).
