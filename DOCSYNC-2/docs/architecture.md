# DOCSYNC-2 Architecture

**Status:** This clarified Stage 2 architecture is explicitly approved by the user; fresh Stage 3 review is in progress. Implementation is not authorized.  
**Scope:** Stage 2 only. `docs/requirements.md` remains unchanged and is the approved source for R-01–R-09 and AC-01–AC-08.

## Overview and boundaries

Add a report-only `docsync drift` command. It reads a defined tracked Git change set and screened, in-scope repository evidence, identifies potential Markdown documentation candidates, and writes the fixed `docsync-drift-report.md` to the separately configured `--output-dir <directory>`. It must not execute repository code, modify source or existing documentation, create/apply patches, commit, or call an external service or LLM.

### Prior approved architecture baseline retained

The following prior architecture choices remain unchanged: the separate drift CLI command; independently configured output directory and fixed report filename; no-overwrite behavior; report-only, non-destructive operation; candidate-only findings; existing Python/`argparse` project baseline; and focused renderer validation without adding a Markdown-validation dependency.

### User-directed revisions

These are explicit architecture clarifications from the user; they do not amend approved requirements:

- **Changed inputs (G-01, G-03):** v1 analyzes tracked added or modified `.py`, `.json`, and `.toml` files only. The change set is tracked staged and unstaged changes compared with `HEAD`; exclude untracked files. Do not offer branch/range selection or interactive change selection. A Git rename maps its new/current path to added and that path is analyzed if eligible; its old path is reported deleted/unanalyzed. Other deleted paths and unsupported inputs are reported as unanalyzed. Sort paths deterministically.
- **Candidate selection (G-02):** One candidate is selected. With multiple candidates and no `--target`, list candidates, return nonzero, and produce neither a Markdown report nor a patch. An explicit invalid target produces an actionable error listing valid candidates. With zero candidates, the report states “no relationship established.” Candidates are never called verified.
- **Evidence (G-04):** Use repository-relative POSIX paths from Git. Match source-path text exactly and case-sensitively. Match source stems, symbols, and configuration keys as exact case-sensitive tokens; ASCII letters, digits, and underscore are token characters. The source-stem match is the v1 filename-similarity signal. A Markdown document shares a directory only when its immediate parent path exactly equals the source's immediate parent path. For each changed source, the candidate set is the union of documents matching any signal; record every matching reason. Use no scores or precedence. One candidate is selected as candidate-only; multiple candidate paths are ambiguous and follow G-02, with a valid `--target` selecting one. Zero matching signals means “no relationship established.” Multiple candidate paths preserve conflict/ambiguity; zero signals are insufficient evidence. No candidate is confirmed or verified.
- **Outcomes (G-05):** Git/change-set acquisition failure, unsafe configuration/output path, or any required decoding/screening safety failure is fatal and produces no report. Deleted, unsupported, out-of-scope, size-limited, unreadable, or parse-failed files are marked unanalyzed and contribute no content. A partial report is safe/useful only if at least one eligible source was successfully decoded, screened, and analyzed, including an explicit no-relationship result. If any inputs are unanalyzed and that condition holds, publish a clearly labeled partial report and return nonzero; otherwise write no report. Skipped items are never counted as analyzed.
- **Output (G-06):** `--output-dir` must already exist. Validate resolved paths and reject unsafe symlink/junction (reparse-point) traversal. Refuse collisions; publish exclusively without replacement and clean temporary files on failure. Write only the report; no patch/apply path or source/documentation modification is permitted.
- **Validation (G-07):** The validation strategy below maps each approved AC verbatim to proposed automated test identifiers. These are plans, not test results.

## Components and responsibilities

| Component | Responsibility and rationale |
|---|---|
| CLI/configuration | Add the separate `docsync drift` command and `--output-dir <directory>`; preserve the fixed report name. Reuse the current Python `argparse` entry point and `RunConfig` patterns where practical. |
| Change-set acquisition | Reuse bounded local Git inspection primitives, but define drift input as tracked staged and unstaged changes against `HEAD`; filter eligible statuses/extensions, retain unsupported/deleted path notices, and sort normalized paths. Do not route through branch/range modes or include untracked files. |
| Evidence collection and safety | Reuse `docsync.evidence`, `docsync.screening`, and `docsync.paths` patterns for screened snapshots, current Markdown inventory, exclusions, file/diff limits, and safe reads. Distinguish available, skipped, failed, and inconclusive evidence. |
| Candidate analysis | Form the candidate set from the G-04 union of exact Markdown source-path references, token-bounded source-stem/symbol/configuration-key matches, and same-immediate-parent matches. Retain every matched reason plus conflict/insufficient-evidence state. Apply G-02 to multiple candidates. Do not claim confirmed relationships or introduce scoring or precedence. |
| Finding model and Markdown renderer | Model candidate findings, no-relationship results, ambiguity, conflicting/insufficient evidence, unsupported/unanalyzed inputs, and complete/partial/fatal outcome. Render safely with potential-drift language and sanitized diagnostics. |
| Report-only publisher | Validate the existing output directory and resolved report path, then create only `docsync-drift-report.md` with exclusive no-replace semantics. Clean up temporary artifacts on failure. Reuse safe path/publication primitives only where they can be separated from patch creation. |

## Technology choices and rationale

- **Python 3.12+ and existing `docsync` package:** established project baseline (`pyproject.toml`).
- **Standard-library `argparse` and current configuration patterns:** the existing entry point and `RunConfig` are available; a separate command isolates drift from the proposal workflow.
- **Local Git CLI through existing `docsync.git`:** current change acquisition is local and bounded; no repository code execution is needed. Drift must apply its own approved `HEAD`/tracked-change contract rather than inherit arbitrary scope modes.
- **Existing screening, path, and report-safety facilities:** reuse `docsync.screening`, `docsync.paths`, and `docsync.reporting` facilities to preserve bounded evidence handling, safe path checks, and constrained Markdown rendering.
- **No added service or dependency:** no external LLM/API or separate Markdown-validation library is introduced. Focused renderer and integration tests must establish valid report output.

The current `run_pipeline`/`write_artifacts` path writes a report and patch and must not be reused unchanged. The drift flow is report-only. Existing output-publication primitives may inform safe exclusive creation, but do not invoke the combined report-and-patch writer.

## Matching boundary and requirement gap

Static inspection of `src/docsync/targets.py` found these current mechanisms:

- Eligible snapshots currently include non-binary `.py`, `.json`, and `.toml` inputs.
- Source evidence includes the changed source path; Python `def`/`class` names; JSON keys; and TOML keys.
- Markdown matching checks source-path text and token-bounded symbol/key text. A filename-stem mention is considered only as an ambiguity candidate when no stronger hit exists. Repeated or multi-document hits can be marked ambiguous or conflicting; incomplete evidence withholds selection.
- This selector is for proposal target selection, not a drift matcher. It does not implement filename-similarity rules or shared-directory matching as stated in approved R-02, and current stem fallback is not a defined similarity policy.

The drift candidate set follows G-04, not the existing proposal selector. Reuse existing primitives only where they implement the stated G-04 matches; do not assume the proposal selector supplies the exact path normalization or immediate-parent rule. Paths are repo-relative POSIX strings; path references are exact and case-sensitive. Token matches are exact and case-sensitive with ASCII letters, digits, and underscore as token characters. The source stem supplies the v1 filename-similarity signal; exact immediate-parent equality supplies the shared-directory signal. Matching reasons form a union, with no ranking. Multiple distinct candidate paths constitute ambiguity/conflicting evidence and follow G-02; zero matches are insufficient evidence and mean “no relationship established.” A valid explicit target resolves the multiple-candidate selection only; the selected document remains a candidate, not a verified relationship. No reliability score or fuzzy matching is introduced.

## Data flow and outcomes

1. Parse `docsync drift`, `--output-dir`, and optional explicit `--target`; validate the existing output directory and its resolved path components before publication.
2. Read tracked staged and unstaged changes against `HEAD`; apply configured repository scope and file/diff limits. Exclude untracked inputs. Classify tracked added/modified `.py`, `.json`, and `.toml` as analyzable. Map a rename's new/current path to added and analyze it if eligible; report its old path as deleted/unanalyzed. Report other deleted and unsupported paths as unanalyzed. Normalize and sort paths deterministically.
3. Collect and screen current source snapshots and the bounded current Markdown inventory. Do not execute repository files. Mark deleted, unsupported, out-of-scope, size-limited, unreadable, or parse-failed files unanalyzed and do not use their content. Any required decoding/screening safety failure is fatal. Skipped items are never counted as analyzed.
4. For each eligible changed source, form the candidate set as the union of exact, case-sensitive repo-relative POSIX source-path references; case-sensitive token matches for source stem, symbols, and configuration keys using ASCII letters/digits/underscore as token characters; and exact immediate-parent matches. Record every reason. Do not score or rank. Multiple distinct candidate paths are ambiguous/conflicting and follow G-02; a valid explicit target selects one, while no target lists candidates and stops nonzero without a report or patch. Reject an invalid target with an actionable error and valid candidates. Zero matching signals means insufficient evidence and “no relationship established.” Candidates are never verified.
5. Classify the run. **Complete:** all required eligible inputs/evidence are analyzed and no inputs are marked unanalyzed; write a complete report and return success, including when analysis has no candidate findings. **Partial:** one or more inputs are unanalyzed; publish a clearly labeled report and return nonzero only if at least one eligible source was successfully decoded, screened, and analyzed (including a no-relationship result); otherwise write no report. **Fatal:** Git/change-set acquisition failure, unsafe configuration/output path, or required decoding/screening safety failure; return a sanitized error and write no report. Skipped items are never successful findings or no-findings.
6. Publish only the fixed Markdown report for complete or partial outcomes, after validating containment within the resolved designated output directory. Refuse an existing destination and use exclusive no-replace creation; remove temporary files on failure. No report is written for fatal errors, a partial run with no safe useful findings, or the no-target multi-candidate outcome.

**Path boundary:** preserve the independently configured output location. Existing `RunConfig` resolves relative output directories against the canonical repository root; do not introduce a broader repository-root containment rule. Resolve and validate the designated path, require its directory to exist, reject symlink/junction traversal, ensure the final report remains within that resolved directory, and never write anywhere else. Do not silently create the directory.

## Requirement traceability

| Requirement | Architectural mapping |
|---|---|
| R-01 | Change-set acquisition analyzes current tracked added/modified eligible inputs, excludes untracked inputs, maps rename new/current paths to added and old paths to deleted/unanalyzed, and reports other deleted/unsupported inputs as unanalyzed. |
| R-02 | Candidate generation follows the G-04 union using exact case-sensitive repo-relative POSIX source-path references, case-sensitive token-bounded source-stem/symbol/config-key matches, and exact immediate-parent equality. Every matched reason is recorded; any match yields a candidate only. Multiple candidate paths are ambiguous and follow G-02; zero signals means no relationship established. |
| R-03 | Finding model retains evidence for explanations; renderer reports only supported reasons and preserves insufficient/conflicting evidence. |
| R-04 | Separate `docsync drift --output-dir <directory>` command writes fixed `docsync-drift-report.md` for complete/partial results. |
| R-05 | Report states “no relationship established” when no defined candidate signal matches. Multiple candidate paths remain ambiguous unless a valid `--target` is provided; unsupported/deleted and other unanalyzed inputs are separately marked unanalyzed, not no-candidate findings. |
| R-06 | Candidate-only, potential-drift wording; no verified claims, unsupported inference, scores, reliability labels, or external LLM. |
| R-07 | Evidence collection enforces configured scope/exclusions and existing file/diff limits; unsupported, skipped, and incomplete inputs are surfaced. |
| R-08 | Report-only publisher writes only beneath the validated designated output directory and never modifies source or existing documentation. |
| R-09 | Complete, partial, and fatal outcomes are distinct; partial is labeled and nonzero, fatal produces no report, and skipped/unanalyzed inputs are never counted as analyzed. A partial run with no safe useful findings produces no report; a complete run can produce an empty-findings report. |

## Architecture validation strategy — proposed AC-to-test map

The exact acceptance-criterion sentences below are copied verbatim from approved `docs/requirements.md`. Test identifiers are proposed and have not been implemented or run.

| AC (verbatim) | Proposed automated test identifiers |
|---|---|
| **AC-01:** For current, non-deleted source changes, identify potentially affected Markdown documentation. Apply the confirmed candidate signals where no explicit relationship exists, and do not include unchanged documentation solely because it may have pre-existing drift. | `test_drift_uses_tracked_added_modified_sources_only`; `test_drift_maps_rename_new_path_to_added_and_old_path_to_deleted`; `test_drift_excludes_untracked_and_reports_deleted_unanalyzed`; `test_drift_reports_unsupported_inputs`; `test_drift_orders_paths_deterministically`; `test_drift_candidate_union_and_exact_match_boundaries` |
| **AC-02:** Each flagged documentation candidate has a reason supported by available repository evidence. | `test_drift_records_each_matched_candidate_reason`; `test_drift_multiple_candidate_paths_preserve_ambiguity`; `test_drift_explicit_target_resolves_selection_only` |
| **AC-03:** Successful analysis produces a valid Markdown report through the separate drift-detection CLI command at a separately configurable destination. | `test_drift_cli_writes_markdown_to_output_directory`; `test_drift_report_uses_fixed_name`; `test_drift_successful_empty_findings_writes_report`; `test_drift_partial_report_is_labeled_and_nonzero` |
| **AC-04:** Relevant changed source files with no reliable documentation candidate are reported as having no relationship established. | `test_drift_no_candidate_reports_no_relationship_established` |
| **AC-05:** Findings remain potential issues, candidates are not represented as confirmed relationships, and ambiguous candidates do not result in documentation changes or patches. Multiple candidates require explicit user target selection. | `test_drift_findings_use_candidate_only_language`; `test_drift_selects_single_candidate`; `test_drift_multiple_candidates_without_target_list_and_write_nothing`; `test_drift_invalid_target_lists_valid_candidates`; `test_drift_never_creates_patch` |
| **AC-06:** Files outside configured scope or size limits are handled according to the existing scope and limit rules. | `test_drift_applies_scope_exclusions_and_size_limits`; `test_drift_marks_limited_or_unsupported_inputs_unanalyzed` |
| **AC-07:** Generating a report leaves source files and existing documentation unchanged and writes only within the designated report output location. | `test_drift_preserves_repository_inputs`; `test_drift_requires_existing_output_directory`; `test_drift_rejects_symlink_and_junction_escape`; `test_drift_refuses_existing_report_without_replacement`; `test_drift_publishes_exclusively_and_cleans_temporary_files` |
| **AC-08:** An analysis failure is communicated clearly and is not represented as successful complete analysis. | `test_drift_complete_partial_and_fatal_outcomes`; `test_drift_partial_returns_nonzero_and_is_labeled`; `test_drift_fatal_acquisition_config_path_or_decode_screen_failure_writes_no_report`; `test_drift_read_or_parse_failure_is_unanalyzed`; `test_drift_partial_includes_successful_no_relationship_result`; `test_drift_partial_without_successfully_analyzed_source_writes_no_report`; `test_drift_errors_are_sanitized`; `test_drift_never_counts_skipped_items_as_analyzed` |

Proposed tests are not implemented or run. The candidate-union and evidence-reason tests cover the user-approved operational definitions of filename similarity, shared directory, source references, and candidate-only outcomes.

## Risks and open issues

- **Incomplete or sensitive evidence:** Screening, limits, and path validation can make evidence unavailable; represent that explicitly and sanitize diagnostics.
- **False positives and misses:** Candidate associations remain potential; preserve ambiguity/conflicts and do not infer unsupported relationships.
- **Failure classification:** A file-level unreadable/parse-failed item is unanalyzed; any required decoding/screening safety failure is fatal. Partial usefulness requires at least one safely analyzed eligible source, including a no-relationship result.
- **Output safety:** A destination collision, path escape, link/reparse point, or failed publication must not replace existing artifacts or leave temporary files.
- **Incompatible reuse:** The current proposal pipeline and artifact writer also generate patches; they are not suitable as the drift command’s execution or publication path.

No tests, Git operations, or code execution were performed for this architecture revision.
