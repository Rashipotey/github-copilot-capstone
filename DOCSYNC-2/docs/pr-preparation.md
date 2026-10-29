# DOCSYNC-2 PR Preparation

**Status:** Draft prepared for user confirmation. No commit or PR has been created.  
**Proposed base:** `main`  
**Scope:** DOCSYNC-2 artifacts and shared DocSync implementation/drift tests only. DOCSYNC-1 and unrelated work are excluded.

## Proposed title

Add report-only documentation drift analysis

## Proposed description

### Summary

- Add a local `docsync drift` command that analyzes tracked staged and unstaged `.py`, `.json`, and `.toml` changes against `HEAD`.
- Generate deterministic, candidate-only Markdown findings and publish only `docsync-drift-report.md` to an existing output directory.
- Bound Markdown inventory traversal to 500 repository entries. When truncated, report insufficient evidence and withhold candidate selection and any no-relationship conclusion.

### Validation

- `py -3.14 -m pytest -q tests` — 398 passed.
- `py -3.14 -m pytest -q tests/test_drift_matching.py tests/test_drift_analysis.py tests/test_drift_evidence.py tests/test_drift_end_to_end.py` — 38 passed.
- Stage 7 supplemental explicit-target/truncated-inventory check — passed.
- Local Markdown structure, link, and renderer checks — passed.

### Limitations and accepted risks

- **Accepted documentation deviation:** the shared README does not document `docsync drift`, its required `--output-dir`, or the report filename. The user declined I-08 and accepted this as a PR limitation; this change does not fully meet the README documentation part of the story's Definition of Done.
- **Accepted CR-02 risk:** a temporary-file cleanup failure after report linking may leave a temporary file while the CLI reports that no report was published.
- There is no maintained end-to-end analysis test combining a truncated inventory and explicit target. The matcher test and a one-off Stage 7 check cover that behavior separately.
- No generated report artifact was available for direct inspection; report behavior was exercised by tests.

## Preparation notes

- Independent compliance audit completed; no commit/PR action was taken during the audit.
- Scoped Git inspection found the current feature branch ahead of its upstream by 22 commits. To keep publication limited to the authorized DOCSYNC-2/shared-code scope, the PR must be assembled from `main` using only the inspected and authorized files; do not publish unrelated branch history.
- User explicitly authorized later commit and PR creation within the stated scope. Confirm this prepared content before taking those actions.
