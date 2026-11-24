# review-gate

A head-pinned merge command for GitHub pull requests. It merges a pull request only at the exact
head it is given, only when every stage below passes on review records and live GitHub reads, and
it never reads the pull request body as an input.

## Install

review-gate is a Python command, not a Claude plugin, so it has no marketplace entry. Install the
pinned release with pip:

```sh
python3 -m pip install 'review-gate @ git+https://github.com/smithadifd/review-gate.git@v0.3.1'
```

It reads the ledger through the ledger-hooks plugin that the public harness default installs; put
that plugin's root on `PYTHONPATH` as an absolute entry (see Status). The accepted ledger-hooks
versions are exactly `0.6.1`, `0.6.2`, `0.6.3` and `0.7.0`. The manifest's version must be one of
them, and the imported ledger package must report the same version as the manifest; any other
version, a range or a prefix refuses `ledger_version_unsupported`.

**Status: ledger-backed merges and recovery.** Set `LEDGER_DB` to an absolute database file;
unset or empty refuses `records_unavailable`, and a relative spelling refuses `ledger_db_relative`.
The variable names a file, never policy. The runtime loads ledger (one of the accepted ledger-hooks versions, above) from the ledger-hooks
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
review-gate record < sol-round-1.json
review-gate item --pr <PR URL> [--lookup]
```

- `<PR URL>` is `https://github.com/<owner>/<repo>/pull/<N>`, matched whole: no trailing slash, no
  `/files`, no query or fragment, no leading zero.
- `<full head sha>` is 40 lowercase hex characters. It is compared, never folded or shortened.
- `--items` is required: the ledger items the merge closes, comma separated.
- `--remediation=` names exactly the jobs that are red on the default branch, for a merge onto a red
  main; it also needs the owner's grant `grant:remediation:<owner>/<repo>#<N>:<head>`.
- `record` takes a [receipt document](#receipt-document-format), not a summary of a review;
  copy and fill one of the JSON examples below for each lane.

Anything outside this grammar refuses by name before any read.

### Find or create the pull request's item

Before the first review, run `review-gate item --pr <URL>`. It prints the one linking
item's id and exits 0, without changing an existing item. If none links, it creates
one ticket and its `pr` link atomically. The title is the canonical URL alone and
the source is `pr:<canonical URL>`; canonicalization casefolds owner and repository.
Stored full URLs and `owner/repo#N` links compare exactly as ledger's `pr_item` does.
Repeated or concurrent calls return the same item without repeating either event.

`--lookup` is read-only: one item prints its id and exits 0; none exits 3 with
`pr_item_missing`. Either flag order is accepted (`--pr URL --lookup` or
`--lookup --pr URL`); flags are exact, given once, without equals forms.
Malformed argv refuses `argument_unknown`, malformed URLs `pr_url_invalid`.
There is no JSON output convention; stdout is one item id followed by a newline.
Other refusals exit 2 with `<name>: <detail>` on stderr and no stdout:
`pr_item_ambiguous` for multiple linking items, `pr_item_link_conflict` for a
competing link seen by the append precondition, `pr_item_source_conflict` for a
conflicting source or derived id, `pr_item_loop_managed` when the loop owns the
source item, and `pr_item_foreign_link` when that item already has a foreign PR link.
Store and dependency admission use the same checks as receipt writing; unexpected
errors report `item_failed: <Type>`.

Creation and linking share one write transaction, including the append's cross-item
precondition. A refusal or stop before commit rolls back both; after commit, a rerun
returns the linked item. A previously created matching source without a link is
completed on retry. Ledger and loop writers serialize on the database transaction.
Raw `ledger link --rel pr` can still add a second linking item after this commits:
store-level uniqueness remains follow-up `LEDGER-PR-LINK-UNIQUE1`.

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

Stage 8 counts only runs created after the pull request was marked ready, so every workflow the merge needs must list `ready_for_review` in `on.pull_request.types`. A bare `pull_request:` uses GitHub's default types, which leave out `ready_for_review`, so flipping a draft to ready starts no run and stage 8 refuses `ci_no_runs`. A library caller may supply stage 8 through the keyword-only `gate.run(..., ci_stage=...)`; the command line never can.

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
```

The module docstrings carry each stage's full rules and refusal names.

## Record rules and recovery

Two deciding receipts from distinct lanes are required. Only reserved `review.receipt` events
count, bound to repository, pull request, head and files digest. Every inline receipt document
is verified by the ledger. The reserved event remains authority except that stage 6 derives
the harden set from those verified bytes through the writer's derivation. Only the highest
round per lane decides; a replacing record anywhere in that deciding slot refuses `receipt_settlement_unsupported`.
Approved and changes-requested receipts count;
an empty changes request refuses. Crashed and unparseable newest rounds never fall back.
Block findings each need their own standing waiver. Every document harden, including one
collapsed into a block on the same id in the event, needs an exact-source child outside
`--items`; a block waiver does not answer it. The source is `<item>:harden:<origin>`, with
the origin resolved through the event entry's carried root. Event hardens must occur in
the document's harden set; any other document harden must have a block event entry on its id,
else `receipt_document_inconsistent` names the receipt event. Missing, by-path or altered
documents refuse `receipt_doc_missing`, `receipt_doc_not_inline` or `receipt_doc_altered`;
verified documents that cannot derive retain the derivation's refusal name. Stage 6's token
retains the document harden ids for the pre-merge recheck. Counted rounds past three need
a standing grant or a current cite.

The database's adjacent `config.json` may set `"review_second_family": "anthropic"` (also
`openai` or `google`). It can only add a family requirement. A malformed, unreadable or symlinked
file refuses. Model families are derived from anchored provider/model forms in `ledger_records.py`;
unknown models never satisfy this requirement. A standing head-bound exception can lift it.

Stage 11 commits `review.merge-intent` before invoking GitHub and records `review.merge-done`
only after confirming MERGED at the supplied head. Both records contain key, pull request and
head; no accepted ledger version has a merge-commit field. The commit remains on GitHub and stdout.
Closing an already closed item is a no-op; close failures can be retried through `recover`, except on a loop-managed item whose pull request the loop linked (ledger 0.7.0): the ledger refuses that close (`reserved-transition:loop-close`), and `recover` keeps refusing until the loop closes the item.

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

The item must already exist and link the pull request. Resolve it before the first review
with `review-gate item --pr <URL>`. Neither writer nor importer creates or links items.

### Receipt document format

`record` reads one UTF-8 JSON object, at most 65536 bytes. The tables below describe the
fields read by `receipt_document.py` and `receipt_writer.py`; unknown fields are preserved
in the attached bytes but do not supply receipt event fields. Use the exact names:
`pr_repo`, not `repo`; `reviewed_head`, not `head`; `requested_model` and `served_model`, not `model`.

| Field | Required / type | Value |
|---|---|---|
| `pr_repo` | Required string | GitHub `owner/repo` of the PR, not a URL; casefolded for recording. |
| `pr` | Required integer, ≥ 1 | The PR number in that repository; booleans are not integers here. |
| `reviewed_head` | Required string | The reviewed commit: exactly 40 lowercase hex characters, equal to the head supplied to merge. |
| `base` | Required string for a live write | The base commit used for the review comparison: 40 lowercase hex characters. `record` checks its shape, not live GitHub state. Import instead supplies its comparison's merge base. |
| `changed_files_sha256` | Required string | 64 lowercase hex characters: the file-list digest described below, equal to merge's comparison digest. |
| `lane` | Required string | Token matching `[a-z0-9][a-z0-9._-]{0,63}`. Merge counts distinct exact lane strings, e.g. `sol` and `claude`; there is no fixed lane-name enum. Changing only the model does not create a second lane. |
| `tier` | Required string | Same token grammar as `lane`, e.g. `full-live`; merge does not judge the tier against the change. |
| `round` | Required integer, ≥ 1 | This lane's review round; continues across heads of this PR, not restarted at 1. |
| `counts_toward_cap` | Required boolean | Whether this round counts toward the review cap; it does not control whether the lane decides. |
| `requested_model` | Required string, 1–200 characters | The model requested from the review runner. |
| `verdict` | Required string | Exactly `approved`, `changes_requested`, `unparseable` or `crashed`. Only the first two decide a merge; merge refuses a `changes_requested` receipt with no findings. |
| `served_model` | Required for deciding verdicts; otherwise optional string or null | Actual model reported by the runner, 1–200 characters when non-null. |
| `family` | Optional string | If present, must equal the family derived from non-null `served_model`: `anthropic`, `openai`, `google`, or `unknown`. Omit it to let the writer derive it. Unknown models do not satisfy a configured second-family requirement. |
| `severity_rubric` | Required for deciding verdicts; string | Must equal `v2`. |
| `findings` | Required for deciding verdicts; array | This round's findings, shaped as below. For `crashed`/`unparseable`, omit or give an empty array. |
| `hardened_carried` | Required for deciding verdicts; array | Unresolved carried hardens; entries must have `cell: "harden"`. Empty at round 1. |
| `blocks_carried` | Optional array | Carried blocks, each with `cell: "block"`; default `[]`. |
| `nits_carried` | Optional array | Carried nits, each with `cell: "nit"`; default `[]`. |
| `resolved_carried` | Optional array | A located entry with exactly matching `file`, `line`, `description` removes a `hardened_carried` entry. Other entries do not resolve it. |
| `standing_carried`, `unsettled_carried` | Optional arrays | Checked only for array type; their entries do not add event findings or counts. |
| `cells` | Required for deciding verdicts; object | Exactly `block`, `harden`, `nit`, each a nonnegative integer. `block` counts own block findings plus all `blocks_carried`; `harden` and `nit` count own findings only. Carried hardens/nits add no cell count. |
| `open_bugs` | Required for deciding verdicts; nonnegative integer | Must equal `cells.block` and the number of derived block entries. For `crashed`/`unparseable`, omit or give null. |
| `prior_receipt` | Required at round ≥ 2; object | `{"sha256": "<prior-document-sha256>"}`: 64 lowercase hex characters naming the already-recorded document of this lane's previous round on this PR's item. Forbidden at round 1. This is a document digest, not an event id. |
| `settled_from`, `adjudicated_from`, `reparsed_from` | Optional objects | Each has `{"sha256": "<source-document-sha256>"}` naming the newest recorded document in the same lane/round/head slot. At most one, except `settled_from` plus `adjudicated_from`: the latter must name the event the former supersedes. These write replacing siblings, but merge currently refuses a replacing record in a deciding slot. |

For deciding verdicts, entries of `findings`, `hardened_carried`, `blocks_carried` and
`nits_carried` use this shape:

| Entry field | Required / type | Value |
|---|---|---|
| `cell` | Required string | Exactly `block` (BUG), `harden` or `nit`; carried lists require their named cell. |
| `description` | Required string | Finding text; the writer hashes it to derive a stable root when no `residual_root` is supplied. |
| `file` | Optional string or null | Nonempty locator path, without whitespace, `:` or `#`. Omit/null together with `line` for an unlocated finding. A file without a line gets `/unlocated/<file>:1`; neither gets `/unlocated:1`. |
| `line` | Optional integer or null | ≥ 1 when supplied; requires `file`. The derived finding id must fit the ledger grammar and be at most 300 characters. |
| `residual_root` | Optional string or null; required for some carried entries | `<path>:<positive-line>@<16-lowercase-hex>` from the first raise, with no whitespace, `:` or `#` in the path. Reuse the root, not a new hash, for a carried finding. |
| `residual_of` | Optional value | Any non-null value marks a `findings` entry as carried; its contents are not interpreted. Such an entry requires `prior_receipt` and a non-null `residual_root`. |

Every carried entry requires a prior receipt. Located `nits_carried` entries require a
non-null `residual_root`; `blocks_carried`, `hardened_carried` and unlocated `nits_carried`
may instead link through their own derived id's root. That root must exist in the prior
receipt and lead to one origin. Duplicate ids within a list, or two block entries on one
id across lists, refuse. Across other lists one id merges into its most severe cell
(`block` > `harden` > `nit`), carried if any copy is carried; counts must still agree.

Merge reads the derived event's repository, PR, head, file digest, lane, round, verdict,
served model/family, findings/ids/carried links, counts and cap flag, and verifies its
attached document digest. Stage 6 also re-derives harden ids from the verified document.
`base`, `tier` and `requested_model` are recorded metadata, not additional merge gates;
`severity_rubric`, `cells` and the carried lists participate in document derivation.

To obtain the comparison inputs, read the PR's `headRefOid` and `baseRefOid`, then compare
that base to that head. Include both previous and current filenames for renames. The digest
is SHA-256 of sorted, de-duplicated paths encoded as UTF-8 (surrogateescape), each followed
by a NUL byte, not a patch digest or a hash of JSON. For example:

```sh
gh pr view "https://github.com/<owner>/<repo>/pull/<pr-number>" --json headRefOid,baseRefOid
gh api "repos/<owner>/<repo>/compare/<base-sha>...<reviewed-head-sha>" --jq '[.files[] | (.previous_filename // empty), .filename]' > changed-paths.json
python3 -c 'import json; from review_gate.github import files_digest; print(files_digest(json.load(open("changed-paths.json"))))'
```

Use the complete comparison file list (GitHub's compare response is capped at 300 files;
merge refuses a capped list rather than trusting it). `record` itself makes no GitHub read
and does not obtain these values for you.

#### Approving lane, no findings

Copy this into `sol-round-1.json`. Replace every `<...>` value with the actual review
inputs; replace the quoted `"<pr-number>"` with an **unquoted integer**. Model placeholders
mean the real requested/served model ids, not a guessed family. If they differ, put each
actual id in its corresponding field. For a second lane use a distinct `lane`, as in the
next example; set counts and findings to that lane's actual result.

```json
{
  "pr_repo": "<owner/repo>",
  "pr": "<pr-number>",
  "reviewed_head": "<reviewed-head-sha>",
  "base": "<base-sha>",
  "changed_files_sha256": "<changed-files-sha256>",
  "lane": "sol",
  "tier": "full-live",
  "round": 1,
  "counts_toward_cap": true,
  "requested_model": "<sol-model-id>",
  "served_model": "<sol-model-id>",
  "severity_rubric": "v2",
  "verdict": "approved",
  "findings": [],
  "hardened_carried": [],
  "cells": {"block": 0, "harden": 0, "nit": 0},
  "open_bugs": 0
}
```

#### Approving lane, one nit

Copy this into `claude-round-1.json`, replace the placeholders as above, and replace `line: 1`
with the finding's actual line. A nit does not increment `open_bugs`.

```json
{
  "pr_repo": "<owner/repo>",
  "pr": "<pr-number>",
  "reviewed_head": "<reviewed-head-sha>",
  "base": "<base-sha>",
  "changed_files_sha256": "<changed-files-sha256>",
  "lane": "claude",
  "tier": "full-live",
  "round": 1,
  "counts_toward_cap": true,
  "requested_model": "<claude-model-id>",
  "served_model": "<claude-model-id>",
  "severity_rubric": "v2",
  "verdict": "approved",
  "findings": [
    {"file": "<finding-file>", "line": 1, "description": "<nit-description>", "cell": "nit"}
  ],
  "hardened_carried": [],
  "cells": {"block": 0, "harden": 0, "nit": 1},
  "open_bugs": 0
}
```

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
`write_receipt`. Use the [field tables](#receipt-document-format) and the
[no-findings](#approving-lane-no-findings) or [one-nit](#approving-lane-one-nit) JSON example
above. Run each lane, then pipe each lane's document in once:

```sh
export LEDGER_DB=/absolute/path/to/ledger.db
export PYTHONPATH=/absolute/path/to/ledger-hooks-plugin-root
review-gate item --pr https://github.com/OWNER/REPO/pull/1
review-gate record < sol-round-1.json
review-gate record < claude-round-1.json
```

Preconditions, none of which the verb supplies: `LEDGER_DB` is an absolute database file (unset
refuses `records_unavailable`); the ledger plugin root is an absolute `PYTHONPATH` entry; and the
pull request's item already exists and links the pull request (resolve it with
`review-gate item --pr <URL>`), because `record` never creates or links items
(`pr_item_missing` refuses).

The argv is exactly `record`: no flag and no operand, so `records`, `record x` and `record --flag`
refuse (`command_unknown`, `argument_unknown`) before standard input is read. Standard input is
read as bytes, at most 65536 bytes plus one; a longer input refuses `receipt_doc_too_large` and is
never cut to fit, and empty or closed input refuses `record_empty`. The verb adds no field and
derives nothing: verdict, cells, family, the prior receipt chain and idempotency are the writer's, so
every refusal the writer already makes (family mismatch, count disagreement, a round restart, a
second document for an occupied slot, a credential shape) stays refused, with no event written.

Malformed documents refuse before any write: `receipt_doc_not_json` (invalid UTF-8/JSON,
non-object, repeated keys, non-finite numbers or unpaired surrogates), `receipt_doc_too_large`,
`receipt_repository_missing` (absent/null `pr_repo`), `pr_url_invalid` (invalid repository
spelling), `receipt_document_shape` (missing fields, wrong types, formats or chain shape),
`receipt_family_mismatch`, `receipt_cell_unknown`, `receipt_finding_id_invalid`,
`receipt_finding_duplicate`, or `receipt_document_inconsistent` (list cells/counts disagree).
The writer also checks `base` and the derived ledger schema under `receipt_document_shape`
before appending; credential-shaped content refuses `doc_credential_shape`.

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
- A library caller supplying its own stage 8 owns what green CI means for its merges; the public
  command always runs the shipped stage. Review-gate checks only the shape of the selection the
  supplied stage leaves, not how it judged runs: either a non-empty `expected` dict whose keys
  and values are non-empty strings with `no_ci_grant` set to `None`, or a `GrantProof` in
  `no_ci_grant` with `expected` an empty dict. Anything else refuses `ci_stage_selection_invalid`
  before stage 9. Stage 10 rechecks that grant with `_same_grant`, whichever stage set it;
  it does not re-judge CI runs.
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
python3 -m pip install 'ledger @ git+https://github.com/smithadifd/ledger.git@v0.7.0'
python3 -m pytest -q -p no:cacheprovider
python3 -I scripts/check-no-person.py
```

The ledger line installs one of the accepted versions; the suite runs unchanged against any of them, and
CI runs it once per accepted version.

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
