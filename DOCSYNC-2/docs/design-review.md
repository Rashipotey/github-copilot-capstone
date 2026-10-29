# DOCSYNC-2 Design Review

**Result:** Review findings recorded; this is not an approval of the design or authorization to proceed to planning.  
**Scope:** Stage 3 design review only. Reviewed `DOCSYNC-2/input/user_story.md`, `DOCSYNC-2/docs/requirements.md`, and `DOCSYNC-2/docs/architecture.md`. The active-story pipeline record was consulted for the approval/status context. No other story was accessed.

## Assessment

The proposed separation of change-set acquisition, evidence collection, candidate matching, report rendering, and report-only publishing is directionally consistent with the report-only requirements. The architecture traces R-01–R-09 and AC-01–AC-08 to components, and it explicitly avoids source/documentation edits, patches, repository-code execution, and external services (`docs/architecture.md:8,16-19,53-61`).

However, several key input, matching, and output behaviors are unresolved. These prevent a sufficiently precise and reproducible implementation contract. In particular, the architecture itself identifies eligible source-file types and missing-target report behavior as implementation blockers (`docs/architecture.md:76,92-93`).

## Findings (severity-ranked)

### DR-01 — High: Eligible changed-source types are undefined

- **Evidence:** Requirements cover current repository/source changes (`docs/requirements.md:10,16`), but leave “reliable” candidate criteria unresolved (`docs/requirements.md:65`). The architecture defers eligible source-file types and says clarification is required before implementation (`docs/architecture.md:17,25,76,92`).
- **Impact / traceability:** The system cannot determine which changed paths enter analysis or consistently satisfy R-01/AC-01 and R-05/AC-04. Different implementations could include materially different files.
- **Blocking:** **Yes, before implementation.** This is an explicit unresolved architecture decision.
- **Recommendation / decision needed:** Obtain human direction on eligible changed-file classes and the treatment of non-source, generated, binary, or otherwise unsupported changes. Then update the architecture and, if scope/behavior changes, obtain the appropriate requirements approval. Do not infer eligibility from existing proposal-generation support.

### DR-02 — High: Report outcome when `--target` is absent is unresolved

- **Evidence:** R-02 requires explicit target selection for multiple candidates (`docs/requirements.md:17,51`). The architecture says to list candidates and return an error when `--target` is absent, but leaves whether to write the Markdown report unanswered (`docs/architecture.md:26,76,86,93`). AC-03 expects a report on successful completion (`docs/requirements.md:39`).
- **Impact / traceability:** CLI success/error behavior, report completeness, and whether the user receives a usable record of ambiguity are unspecified; this affects R-02, R-04, R-09 and AC-03/AC-05/AC-08.
- **Blocking:** **Yes, before implementation.** The artifact explicitly calls this an implementation blocker.
- **Recommendation / decision needed:** Obtain a human decision on whether the ambiguous-candidate result produces a report (and how that report represents unresolved candidates) or fails without one. Specify the command outcome consistently with that choice. A target selection must not be presented as a confirmed relationship.

### DR-03 — High: The analyzed Git change set is not operationally defined

- **Evidence:** The architecture refers to a “selected local Git change set” and bounded Git diff collection, but does not state how that set is selected (`docs/architecture.md:8,24`). The requirements say “current changes” and the story explicitly lists renamed/deleted files as edge cases (`docs/requirements.md:10,16; input/user_story.md:89-97`).
- **Impact / traceability:** Different interpretations of staged, unstaged, untracked, or reference-to-reference changes—and rename detection—can produce different inputs for R-01/AC-01 and undermine reproducibility.
- **Blocking:** **Yes, before implementation**, unless an already-approved interface or behavior is supplied as evidence.
- **Recommendation / decision needed:** Define the change-set source/baseline and treatment of staged, unstaged, untracked, renamed, and deleted paths. Document how scope/exclusion and diff limits apply to that set. Do not assume a Git mode.

### DR-04 — High: Candidate signal rules are not precise enough to reproduce

- **Evidence:** R-02 names filename similarity, shared path/directory, and Markdown source references, while the requirements acknowledge no measurable reliability threshold (`docs/requirements.md:17,51,65`). The architecture names the same signals and preserves their evidence, but specifies no comparison/normalization or candidate-qualification rules (`docs/architecture.md:17,26,54`).
- **Impact / traceability:** The same repository state/configuration may yield inconsistent candidate sets or explanations across implementations, weakening the deterministic/reproducible requirement and R-02/R-03/AC-01/AC-02. Candidate-only language avoids claiming correctness but does not define which signals create a candidate.
- **Blocking:** **Yes, before implementation** for a reproducible contract; numeric scores are not required.
- **Recommendation / decision needed:** Human approval is needed for deterministic operational rules for each signal (including path/reference normalization and what constitutes a match), how signals combine, and when the result is “no relationship established.” Preserve candidate-only wording and do not introduce confidence/reliability labels without approval.

### DR-05 — Medium: Partial-analysis and failure publication behavior is incomplete

- **Evidence:** Requirements require clear failure communication and forbid presenting incomplete analysis as complete (`docs/requirements.md:24,44`). The architecture represents skipped inputs and complete/incomplete/failed status, and says incomplete evidence cannot be reported as complete success (`docs/architecture.md:18,27,61`), but does not define whether partial reports are written, how skipped/oversized/unreadable inputs affect status, or how the CLI signals the outcome. The story lists size-limit and permission-error cases (`input/user_story.md:89-97`).
- **Impact / traceability:** Users and automation cannot distinguish a complete result from a partial one consistently; this affects R-07/R-09 and AC-06/AC-08, and risks confusing “no candidate” with “not analyzed.”
- **Blocking:** **Not a blocker to this review; resolve before implementation.**
- **Recommendation / decision needed:** Specify report contents/status for skipped and failed inputs, whether a partial report is emitted, and the command outcome for complete, incomplete, and failed runs. Keep sanitized diagnostics and never label partial analysis complete.

### DR-06 — Medium: Report-path safety and no-overwrite guarantees need concrete rules

- **Evidence:** Requirements mandate safe paths, preservation, and writes only to the designated output (`docs/requirements.md:23,29,43`). The architecture delegates validation to existing path/artifact protections and refuses an existing report (`docs/architecture.md:19,28,60,85`), without stating the canonicalization/symlink, containment, or race-safe no-clobber behavior.
- **Impact / traceability:** Ambiguous path handling could violate R-08/AC-07 by writing outside the designated location or replacing an existing report under a concurrent collision.
- **Blocking:** **Not a blocker to this review; safety details must be settled before implementation.**
- **Recommendation:** Specify how the destination and final report path are validated (including symlinks and path traversal), how containment is enforced, and how creation guarantees no replacement even under a race. Preserve the report-only boundary.

### DR-07 — Medium: Validation coverage is narrower than the acceptance surface

- **Evidence:** The architecture calls for focused tests of the constrained Markdown renderer, not a separate Markdown validator (`docs/architecture.md:38`), while AC-03/AC-06/AC-07/AC-08 cover valid Markdown, limits, preservation, and failures (`docs/requirements.md:39,42-44`). No acceptance-criteria-to-test scenarios are specified in the architecture.
- **Impact / traceability:** Renderer-only tests would not by themselves demonstrate correct change selection, candidate explanations, no-candidate/ambiguity handling, safe output, preservation, or incomplete/failure reporting.
- **Blocking:** **No for this design review; test design must be made verifiable before implementation approval.**
- **Recommendation:** Add a testability mapping for every AC, including deterministic repeated runs, candidate/no-candidate/multiple-candidate cases, exclusions and limits, rename/deletion behavior, destination collision/path safety, preservation, and sanitized failures. A separate Markdown library is not necessarily required if the approved renderer tests actually establish the required output validity.

### DR-08 — Low: Architecture approval status text conflicts with the canonical state

- **Evidence:** The architecture header says it is “awaiting Stage 2 review and approval” (`docs/architecture.md:3`), and its closing statement says it awaits explicit Stage 2 approval and that no Design Review is authorized (`docs/architecture.md:95`). The active-story pipeline record instead records Architecture Complete, explicit approval, and Stage 3 In progress (`docs/pipeline-state.md:6,17`).
- **Impact:** Reviewers may misread the approved artifact’s status or authority, weakening traceability; it does not negate the approval evidenced in the canonical state.
- **Blocking:** **No.**
- **Recommendation:** In a separately authorized documentation update, reconcile the stale status text with the canonical approval record. This review does not modify the approved architecture.

## Requirement coverage and design observations

| Area | Review result |
|---|---|
| Requirement coverage / traceability | High-level R-01–R-09 and AC-01–AC-08 mappings are present (`docs/architecture.md:52-61`). Coverage remains conditional on DR-01–DR-05. |
| Component boundaries / data flow | Report-only publisher is separated from matching and existing patch generation; the six-step flow is understandable (`docs/architecture.md:10-28`). |
| Security / privacy | Non-execution, screening, safe-path reuse, and sanitized-error intent are present (`docs/architecture.md:8,16,19,67-69`); concrete path guarantees remain under DR-06. |
| Failure handling | Complete/incomplete/failed distinctions are recognized, but user-visible partial-result behavior remains unclear (DR-05). |
| Testability / maintainability | Focused renderer validation and reuse boundaries are named; acceptance-level coverage and exact integration contracts need specification (DR-07). |
| Feasibility | The architecture describes a local Python/argparse/Git reuse path and notes that existing report/patch paths are unsuitable unchanged (`docs/architecture.md:32-40`). This review did not inspect implementation code, so these feasibility claims were not independently verified. |

## Decisions still needed

1. Eligible changed-source file types and unsupported-file handling (DR-01).
2. Whether an ambiguous multi-candidate run without `--target` writes a report and its command outcome (DR-02).
3. Exact Git change-set selection and rename/untracked behavior (DR-03).
4. Deterministic candidate matching and signal-combination rules, including “no relationship established” (DR-04).
5. Partial-report, skipped-input, and failure signaling behavior (DR-05).
6. Concrete safe-path and race-safe no-overwrite rules (DR-06).

These are recommendations and unresolved decisions, not approvals. The design review outcome and any material architecture updates require human approval before planning.

## Validation and unreviewed areas

- Read the active story, requirements, architecture, and active-story pipeline record; checked artifact references and line locations.
- No tests, runtime checks, Git operations, logs, code changes, or other-story access were performed.
- Implementation feasibility claims were not independently verified because code was not inspected. No runtime or test evidence was assessed.
- This report does not approve the architecture, resolve any finding, or authorize Stage 4.
