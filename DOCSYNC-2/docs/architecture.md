# DOCSYNC-2 Architecture

**Status:** Revised per the user's requested changes; awaiting Stage 2 review and approval.  
**Scope:** Stage 2 only. This document describes the architecture for the approved requirements; it does not authorize implementation or later-stage work.

## Architecture overview

Add a report-only drift-analysis path invoked as `docsync drift`. It consumes a selected local Git change set and screened, in-scope repository evidence; it identifies candidate Markdown documents using deterministic signals and emits `docsync-drift-report.md` into the independently configured output directory supplied with `--output-dir <directory>`. It refuses to overwrite an existing report and returns an actionable error. It does not create patches, edit source or documentation, execute repository code, or call external services.

### Components and responsibilities

| Component | Responsibility |
|---|---|
| CLI/configuration extension | Expose drift analysis as `docsync drift`; collect the repository, diff scope, exclusions, limits, and output directory through `--output-dir <directory>`. Use the deterministic filename `docsync-drift-report.md`. Reuse the current Python `argparse` CLI and `RunConfig` patterns where practical. |
| Change-set acquisition | Reuse `docsync.git` repository inspection and bounded local Git diff collection. Normalize change metadata; exclude deleted source files as required. |
| Evidence collection and safety | Reuse `docsync.evidence`, `docsync.screening`, and `docsync.paths` patterns for bounded snapshots, current Markdown inventory, exclusions, safe path handling, and evidence screening. Incomplete or unsafe required evidence must prevent a complete-success result. |
| Candidate matcher *(new responsibility)* | Compare each eligible changed source input with current Markdown documents using the approved deterministic signals: filename similarity, shared path/directory, and source-file references in Markdown. Preserve signal evidence for explanations. All matches remain candidates, not confirmed relationships; do not assign confidence scores or reliability labels. Eligible source-file types remain deferred and block implementation. |
| Finding/report model and renderer | Represent changed inputs, candidate documents, signal-based reasons, no-candidate results, ambiguities, skipped inputs, and analysis status; render a safe Markdown report with potential-drift language. Reuse the existing report-safety conventions where suitable. |
| Report-only publisher *(new responsibility)* | Validate the independently configured `--output-dir <directory>` and write only `docsync-drift-report.md`, using safe path and output protections consistent with `docsync.paths` and `docsync.artifacts`. It must not invoke patch creation or modify repository inputs. If the report already exists, refuse to overwrite it and return a clear, actionable error; there is no overwrite option. |

### Interactions and data flow

1. `docsync drift` parses its configuration, including `--output-dir <directory>`, and validates repository/output paths.
2. The change-set component obtains the selected Git diff and changed-path/status metadata within configured file and diff limits.
3. Evidence collection filters out deleted source inputs, applies repository scope and exclusions, and obtains screened snapshots and a bounded inventory of current Markdown. Eligible source-file types are deferred; this decision must be clarified before implementation.
4. The matcher applies the approved deterministic signals to eligible changed sources and Markdown inventory. It records the signals that support each candidate. All matches remain candidates without confidence scores or reliability labels. When multiple candidates exist, the user must select a candidate Markdown file using `--target <repository-relative-path>`. If multiple candidates exist and `--target` is absent, list candidate paths, return a clear error, and do not create a patch. Whether the Markdown report is still written in this error case remains unresolved and blocks implementation.
5. The report model records findings, evidence-based reasons, ambiguity/no-candidate information, skipped inputs, and whether analysis was complete. A failure or incomplete evidence set is reported as such, never as a successful complete analysis.
6. The report-only publisher validates the independently configured output directory and writes `docsync-drift-report.md` only if that file does not already exist. On collision, it refuses to overwrite and returns a clear, actionable error. It does not write a patch or touch source/documentation files.

## Boundaries and technology choices

### Existing technical baseline

- **Python 3.12+** and the existing `docsync` package are the project baseline (`pyproject.toml`).
- The current entry point is `docsync.cli:main`, implemented with the Python standard-library `argparse`.
- Local change collection already uses Git through `docsync.git`; no repository code execution is needed.
- Existing modules provide exclusion matching and resource limits (`config.py`), screened snapshots and Markdown inventory (`evidence.py`, `screening.py`), path validation (`paths.py`), and safe Markdown report conventions (`reporting.py`).
- The architecture uses focused tests for the constrained Markdown renderer; it does not add a separate `markdown-it-py` validation step.
- No new network service, LLM/API, or documentation mutation path is in scope.

These are proposed reuse choices grounded in the current codebase, not approval of a particular implementation. In particular, the existing `run_pipeline` and `write_artifacts` paths generate a report and a patch; they must not be reused unchanged for this report-only feature. The existing `select_targets` path serves deterministic proposal generation, currently considers a narrower set of source formats and evidence anchors, and does not implement the complete approved candidate-signal behavior. It is not a substitute for the drift matcher.

### Scope boundary

**In scope:** current, non-deleted source changes; current Markdown candidate documents; configured repository scope and limits; deterministic, evidence-based candidate reporting; an independently configurable report destination; sanitized failure reporting.

**Out of scope:** unchanged/pre-existing drift; deleted source files; automatic documentation or source edits; patches, commits, or pull requests; non-Markdown documentation; semantic-equivalence guarantees; repository-code execution; external LLM/API judgments.

## Requirement traceability

| Requirements / acceptance | Architectural mapping |
|---|---|
| **R-01 / AC-01** | Change-set acquisition and candidate matcher analyze current changes, exclude deleted source files, and do not scan unchanged documents as independent drift findings. |
| **R-02 / AC-01, AC-05** | Matcher uses the three approved deterministic signals; all findings identify candidates rather than confirmed relationships and have no confidence score or reliability label. Multiple candidates require `--target <repository-relative-path>`; without it, candidate paths are listed and the command returns an error without a patch. |
| **R-03 / AC-02** | Matcher retains signal evidence; report model and renderer express a reason for each candidate based on that evidence. |
| **R-04 / AC-03** | `docsync drift --output-dir <directory>` and the report-only publisher produce `docsync-drift-report.md` in the independently configured output directory. |
| **R-05 / AC-04** | Report model represents changed sources with no candidate as “no relationship established”; candidates are not assigned reliability labels. |
| **R-06 / AC-05** | Renderer consistently labels findings as potential drift and candidate associations, not confirmed inaccuracies or relationships. |
| **R-07 / AC-06** | Evidence collection reuses configured scope, exclusions, per-file limits, aggregate/change limits, and safe path handling. |
| **R-08 / AC-07** | Separate publisher writes only `docsync-drift-report.md` in the designated output directory; it does not call patch creation or modify repository inputs. Path containment is required, and an existing report is refused without replacement. |
| **R-09 / AC-08** | Pipeline status and publisher distinguish complete, incomplete, and failed analysis; incomplete evidence cannot be presented as complete success. |

## Risks and mitigations

- **Uncalibrated candidate matching:** The user selected candidate-only output without reliability labels or scores. Keep signals visible and findings qualified.
- **False positives or missed relations:** Filename/path signals can be weak and source-reference signals may be absent. Report the evidence and ambiguity without claiming correctness.
- **Unsafe or sensitive repository content:** Treat repository data as untrusted; reuse screening and path-validation protections; do not execute files or expose raw diagnostics, secrets, or environment data.
- **Incomplete analysis:** Git, inventory, screening, size-limit, or read errors can omit evidence. Fail closed for complete-success reporting and clearly surface sanitized status.
- **Report collision or path escape:** Use validated destination handling, refuse to replace an existing report with an actionable error, and protect repository inputs.
- **Incompatible reuse:** The current pipeline writes a report and patch, and its target selector is proposal-oriented. A dedicated report-only flow is needed to avoid accidental patch generation or unsupported matching assumptions.

## Feasibility and completeness assessment

Static inspection of the existing package and configuration shows a feasible local implementation path: Python/argparse and the package entry point exist; Git diff collection, changed-file metadata, exclusions, limits, safe repository reads, Markdown inventory, screening, and report-safety patterns exist. The existing artifact writer is specifically unsuitable unchanged because it creates a patch alongside the report. The drift matcher and report-only publishing path are new responsibilities.

Completeness remains conditional on decisions below. Eligible source-file types are deferred. Whether the report is written when multiple candidates lack an explicit target is unspecified. These are blockers before implementation. No tests or runtime checks were run for this architecture-only stage.

## Decisions and blocking unresolved questions

The user directed this revision to use:

- `docsync drift` as the CLI interface.
- `--output-dir <directory>` independently of the files being analyzed.
- `docsync-drift-report.md` as the deterministic report filename.
- No-replace collision handling: if the report exists, refuse to overwrite it and return a clear, actionable error. No overwrite option is provided.
- `--target <repository-relative-path>` for explicit non-interactive selection when multiple candidates exist. If omitted, list candidates, return a clear error, and do not create a patch.
- Candidate-only findings without confidence scores or reliability labels.
- Focused tests for the constrained Markdown renderer, without a separate `markdown-it-py` validation step.

The following decisions remain explicitly deferred and block implementation:

1. **Eligible source-file types:** The user directed that this remain deferred. Do not infer the eligible types from proposal-generation support or other existing behavior; obtain clarification before implementation.
2. **Report behavior on missing target:** When multiple candidates exist and `--target` is omitted, the command must list candidates, return a clear error, and not create a patch. Whether it also writes a Markdown report is not specified; clarify before implementation.

The architecture is revised and awaits the user's review and explicit Stage 2 approval. No Design Review or implementation is authorized by this artifact.
