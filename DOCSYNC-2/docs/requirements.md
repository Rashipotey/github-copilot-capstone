# DOCSYNC-2 Requirements

**Status:** Consolidated requirements confirmed by the user for Step 1  
**Source:** `input/user_story.md` (proposed input), plus the user's confirmed decisions below

## Scope

Provide a separate CLI command that analyzes current repository changes and produces a Markdown report of potentially affected Markdown documentation, evidence-based reasons for review, and relevant changes for which no reliable documentation candidate is established. This is a report-only analysis: it does not determine or assert that documentation is incorrect.

The analysis covers only current changes, excludes deleted source files, and respects configured repository scope and file-size limits. Unchanged or pre-existing documentation drift is not in scope.

## Functional requirements

| ID | Requirement | Traceability |
|---|---|---|
| R-01 | Analyze available current repository changes and identify Markdown documentation that may be affected. Exclude deleted source files and do not report unchanged documentation merely for possible pre-existing drift. | FR-01; AC-01; user decisions 3 and 5 |
| R-02 | Where no explicit relationship exists, identify documentation candidates using deterministic signals, including filename similarity, shared path or directory, and source-file references in Markdown. A candidate is not a confirmed relationship. If multiple candidates exist, require the user to specify the target explicitly; do not generate documentation changes or patches for ambiguous candidates. If no reliable candidate exists, report that no relationship was established. | FR-01, FR-05; AC-01, AC-05; user decision 4 |
| R-03 | Explain each finding with reasons supported by available repository evidence. | FR-02; AC-02 |
| R-04 | Generate a valid Markdown report containing findings and explanations. Report destination must be separately configurable. Drift detection is invoked as a separate CLI command. | FR-03; AC-03; user decisions 1 and 2 |
| R-05 | Report relevant changed source files for which no reliable documentation candidate is identified. | FR-04; AC-04 |
| R-06 | Present findings as potential drift, not confirmed inaccuracies, unless supported evidence permits that conclusion; do not make unsupported claims. | FR-05; AC-05 |
| R-07 | Analyze only files allowed by configured DOCSYNC scope and file-size limits. | FR-06; AC-06 |
| R-08 | Preserve source files and existing documentation while generating the report; report generation must stay within its designated output location. | FR-07; AC-07 |
| R-09 | Clearly communicate analysis failures and do not present an incomplete report as a successful complete analysis. | FR-08; AC-08 |

## Non-functional requirements

- Drift detection must not execute repository code.
- Handle repository paths safely and respect configured scope and size limits.
- Errors must not expose sensitive environment variables or credentials.
- Findings must be deterministic and reproducible for the same repository state and configuration.
- Output is Markdown only. Do not use an external LLM or AI API to judge documentation accuracy.
- Do not automatically rewrite documentation or modify source code.

## Acceptance criteria

1. **AC-01 — Potentially affected documentation:** For current, non-deleted source changes, identify potentially affected Markdown documentation. Apply the confirmed candidate signals where no explicit relationship exists, and do not include unchanged documentation solely because it may have pre-existing drift. *(FR-01; R-01, R-02)*
2. **AC-02 — Evidence-based explanation:** Each flagged documentation candidate has a reason supported by available repository evidence. *(FR-02; R-03)*
3. **AC-03 — Markdown report:** Successful analysis produces a valid Markdown report through the separate drift-detection CLI command at a separately configurable destination. *(FR-03; R-04)*
4. **AC-04 — Documentation gaps:** Relevant changed source files with no reliable documentation candidate are reported as having no relationship established. *(FR-04; R-05)*
5. **AC-05 — No false certainty:** Findings remain potential issues, candidates are not represented as confirmed relationships, and ambiguous candidates do not result in documentation changes or patches. Multiple candidates require explicit user target selection. *(FR-05; R-02, R-06)*
6. **AC-06 — Scope and file limits:** Files outside configured scope or size limits are handled according to the existing scope and limit rules. *(FR-06; R-07)*
7. **AC-07 — Repository preservation:** Generating a report leaves source files and existing documentation unchanged and writes only within the designated report output location. *(FR-07; R-08)*
8. **AC-08 — Analysis failures:** An analysis failure is communicated clearly and is not represented as successful complete analysis. *(FR-08; R-09)*

## Confirmed decisions

1. Drift detection is a separate CLI command.
2. The report destination is separately configurable.
3. Deleted source files are excluded.
4. Without an explicit relationship, deterministic signals (filename similarity, shared path/directory, and source-file references in Markdown) may identify a **candidate only**. Ambiguous candidates do not receive documentation changes or patches. Multiple candidates require the user to specify a target explicitly; if no reliable candidate exists, report that no relationship was established.
5. Report only documentation potentially affected by current changes, not unchanged or pre-existing drift.

## Constraints and out of scope

- No repository code execution for drift detection; no external LLM/API for judging accuracy.
- No automatic documentation rewriting, source modification, commits, or pull requests.
- No documentation formats other than Markdown.
- No guarantee of semantic equivalence between source code and documentation.
- Do not modify files outside the designated report output location.

## Limitations and remaining clarification items

- The five clarification questions in the proposed story are resolved by the decisions above; no additional requirements-stage clarification is pending.
- No measurable threshold is specified for when candidate matching is “reliable.” This remains an unresolved decision/blocker for a later authorized stage; no threshold is assumed here.
- Exact CLI option names and report-path mechanics are unspecified and deferred to architecture; they are not defined by these requirements. The interaction mechanics for explicit target selection are likewise not specified here.
- Behavior when the configured report destination already contains a prior report is unspecified and remains to be resolved before behavior is designed.
