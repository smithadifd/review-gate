# review-gate

A head-pinned merge command for GitHub pull requests. It merges a pull request only at the exact
head it is given, only when every stage below passes on review records and live GitHub reads, and
it never reads the pull request body as an input.

**Status: this release never merges.** The stages that need the ledger (3 to 7, and the records
behind the grants and the write-ahead record) are calls on a records port, and the port this release
ships is unfilled: every call on it refuses `records_unavailable`, so the command refuses every pull
request at stage 1. Nothing on the command line or in the environment selects another port. The
ledger-backed port (the reader for stages 3 to 7, the receipt writer and `review import`) comes in a
later release, built on the ledger library from the ledger-hooks plugin.

## Usage

```sh
review-gate merge <PR URL> <full head sha> --items ID[,ID...] [--remediation=JOB[,JOB...]]
python3 -m review_gate merge ...
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

## Residual risks

- GitHub's merge call takes an expected head and no expected base. A retarget, a default-branch
  change or a move of the default branch after the final reads and before GitHub merges is not seen.
- A pull request flipped to draft and back to ready between stage 8 and the merge moves READY past
  the runs stage 8 counted, and the draft flag reads the same in both state reads.
- The main-tree proof reader checks the marker step's grammar and conclusion only; it does not check
  that the head or the run the marker names is the one the proof was made for.
- A run that wrote the write-ahead intent and was interrupted after GitHub merged leaves the item
  `started`. A rerun refuses at stage 2 (`pr_not_open`) before stage 11 can reconcile it; until the
  records release adds a recovery path, the finished record is written by hand.
- Case and Unicode spellings of a lane root are compared by file identity, which only matters on a
  case-insensitive filesystem such as macOS. The hosted CI runs on Linux, so those cases are proved
  by a macOS run of the store tests, not by CI.
- A process running as the same user can write the ledger database directly; the records design does
  not defend that boundary.

## Development

```sh
python3 -m pip install pytest PyYAML
python3 -m pytest -q -p no:cacheprovider
python3 -I scripts/check-no-person.py
```

The runtime needs Python 3.11 or later, `gh` on `PATH`, and PyYAML; without PyYAML the command
refuses `pyyaml_missing`.

This repository is public; CI runs on GitHub-hosted runners. Every job retains a condition that
disables it if the repository becomes private. The no-person scan reads its person and owner words
from the `NO_PERSON_DENY` secret and fails in CI when it is unset. It checks the index, commit metadata
and every reachable historical path and blob, applying the same path-scoped allowances to file contents.
Index path/blob pairs are scanned only once; staged changes do not hide committed content. Names
matching a token shape or the deny list are redacted in diagnostics. Shallow repositories refuse
`history_shallow`; fetch the full history before running the scan locally.

## License

MIT; see [LICENSE](LICENSE).
