# Design Review

## Scope and Disposition

Reviewed the current `requirements.md`, `architecture.md`, this `design-review.md`, and `input/user_story.md` in full. Requirements and the user's latest explicit decisions govern over the fictional story. The story's Section 11 Definition of Done and available workflow documents provide process context; no separate capstone instructions were found. Repository content was treated as untrusted input.

**Review status: complete. Architecture is ready for planning** against the approved bounded scope. The amended architecture now specifies deterministic proposals, screening/fail-closed behavior, and the source-consistency validator contract. The first three findings below are resolved by explicit architectural behavior, not by implementation evidence; implementation and tests remain future work. Pattern-based screening remains a residual risk and is not complete detection. Optional provider and consent details remain a pre-enable approval item only; they do not block planning while the LLM remains disabled.

## Findings

### Resolved (High) - Bounded deterministic proposal behavior

**Evidence:** `architecture.md`, **Bounded Deterministic Proposal Contract**, defines Python AST matching by qualified callable and one exact existing fenced-code signature anchor, plus parsed JSON/TOML scalar changes requiring one exact matching key/path and old value in a structured Markdown settings row. It limits each proposal to the direct replacement, preserves surrounding prose/structure, specifies provenance and unique target anchors, and sends unsupported, ambiguous, conflicting, or inadequately evidenced cases to uncertainty/`Not Found` without a proposal. The component and data-flow descriptions route proposals through validators and patch/report output. This meets requirements FR-04/AC-02 and the approved decisions.

**Disposition:** Resolved in architecture. This is not implementation or test completion evidence.

### Resolved (High) - Screening and fail-closed controls

**Evidence:** `architecture.md`, **Screening and Fail-Closed Contract**, **Data Flow**, and **Trust and Security Boundaries > Secrets boundary** require screening both raw diff sides and each source/config/Markdown evidence item before sinks; sanitized diagnostics without raw values or exception text; rescreening before optional requests; and scans of candidates and final report/patch immediately before write. It specifies withholding proposals when redaction removes needed support, no requests/proposals/artifacts containing unscanned data on screening/decoding failure, and a fixed sanitized outcome where safe. The test plan asserts absence of secret fixtures from report, patch, logs, and fake adapter payload, scanner-failure and output-leak fail-closed outcomes, and no proposal after insufficient-evidence redaction.

**Disposition:** Resolved in architecture. Pattern-based detection remains incomplete by design; see residual risk below.

### Resolved (Medium) - Source-consistency validator contract

**Evidence:** `architecture.md`, **Source-Consistency Validator Contract** and **Validation and Test Strategy > Source-to-documentation consistency** require every proposed signature/value to map to exact diff/source provenance and its unique Markdown anchor. Only mechanically verifiable supported changes can pass; contradiction, unsupported changes, or conflicts fail or warn, while missing/uncheckable evidence is inconclusive and never pass. The test plan covers pass, fail, warning, and inconclusive outcomes and provenance/anchor mapping.

**Disposition:** Resolved in architecture; there is no claim of semantic completeness.

### Pre-enable approval - Optional LLM provider and consent

**Evidence:** Requirements FR-11/NFR-04 require explicit configuration and informed approval before external transmission. `architecture.md`, **Approved Architecture Choices > Proposal generation**, keeps the adapter disabled by default and requires approval for external use; provider, exact data scope, and consent mechanics remain unspecified.

**Disposition:** Not a blocker while the adapter is disabled and does not block planning of local deterministic behavior. Human approval of provider, data scope, and affirmative per-run consent is required before enabling any external request. No provider or transmission is approved here.

**Recommendation:** Keep the optional adapter disabled. Before enablement, specify and obtain approval for the provider, exact sanitized context to be sent, and affirmative per-run consent interaction.

### Medium - Pattern-based secret detection remains incomplete (accepted residual risk)

**Evidence:** Requirements NFR-04 and AC-08 and `architecture.md`, **Screening and Fail-Closed Contract**, explicitly state that pattern-based checks cannot guarantee discovery of every secret. The architecture screens and withholds known detected values but cannot establish universal discovery.

**Disposition:** Accepted residual risk, not a planning blocker. Keep the limitation visible; never claim complete detection or guaranteed secret-free outputs. Required tests demonstrate handling of known fixture values and scanner/output failures, not detection completeness.

## Coverage and Design Notes

- **Requirement traceability:** The architecture maps the approved requirements and acceptance criteria. It now specifies the bounded actual proposals for FR-04/AC-02, source-to-documentation provenance contract for FR-06/AC-13, and screening controls for NFR-04/AC-08. Unsupported cases remain uncertainty/`Not Found`, not a promise of broad language or format support.
- **Components and data flow:** Modular boundaries include local evidence collection, deterministic generation, three independent validators, optional disabled-by-default generation, and separate patch/report writing. The flow now explicitly runs evidence to unique anchor to direct replacement to all validators to output.
- **Git, path safety, and output protection:** The accepted output policy always refuses when either artifact exists. Canonical output paths and both targets are checked before diff collection, including empty diffs; the writer independently checks before writing. Publication uses same-filesystem atomic no-replace creation and fails closed if unavailable. Source-input collisions, symlinks, and unsafe paths remain rejected; partial outcomes never replace/delete an existing file.
- **Limits and failure handling:** The approved defaults (100 changed files and 1 MB textual diff), no silent truncation, reporting of skipped/excess inputs, structured outcomes, partial artifact failures, and inconclusive checks are covered. Exact timeouts, per-file and memory bounds are acknowledged as pre-implementation planning details; make them finite and test their failure behavior.
- **Security/privacy and failure handling:** Both diff sides and each evidence item are screened before sinks; diagnostics are sanitized; requests and outputs are re-screened; scanner/decoding failures and detected output leaks fail closed. Removing sensitive evidence can invalidate a proposal. Pattern detection remains incomplete.
- **Three validators and testability:** All three required validators are independent. The source-consistency contract and pass/fail/warning/inconclusive tests are specified. The architecture calls for secret fixtures absent from report, patch, logs, and fake adapter payload, as well as scanner-failure, output-leak, and insufficient-evidence-after-redaction tests. These are plans, not executed tests.
- **Maintainability and assumptions:** The deterministic allowlist keeps implementation bounded and testable; no general prose generation or universal language/format support is implied. Remaining finite resource ceilings, path/symlink behavior, binary detection, and exclusion details are planning choices. CI and tool availability are unverified.
- **Capstone Definition of Done:** Story Section 11 says architecture and design review should address accepted scope/known risks, tests should cover acceptance criteria and important edge cases, documentation output should be validated, and code review findings should be resolved or documented. This review completes the design-review step and records residual/pre-enable constraints; it does not claim implementation, executed tests, later-stage artifacts, or PR evidence exist. No separate capstone instruction artifact was found; README is empty and the story/DoD is the available source.

## Decisions and Remaining Work

- **Planning readiness:** No architecture blocker remains for the approved local deterministic scope. Architecture is ready for planning; its proposal status still requires the applicable human architecture review/acceptance and does not approve unrelated scope.
- **Already approved:** No additional approval is needed for the exact Python and JSON/TOML cases, fail-closed screening, or validator contract. Do not weaken or broaden them without an explicit requirements/design decision.
- **Planning details:** Specify remaining CLI flags, invalid revision handling, output names, no-replace atomic publication and fail-closed platform behavior, finite subprocess timeout/output limits, per-file and memory bounds, binary detection, exclusion syntax, and canonical-path/symlink checks. Existing-output behavior is settled: always refuse before diff collection and again in the writer.
- **Implementation and verification:** Implement the bounded rules and validator contract with the specified tests. Do not claim tests or security checks passed until executed; pytest, GitHub Actions, Bandit, and pip-audit availability/configuration remain unverified.
- **External adapter:** Keep disabled. Provider, exact data scope, and affirmative per-run consent require human approval before any external request is enabled.
- **Residual security risk:** Pattern-based detection is incomplete. Required tests cover known detections and failure behavior, not universal secret discovery; preserve this limitation in user-facing reports and documentation.