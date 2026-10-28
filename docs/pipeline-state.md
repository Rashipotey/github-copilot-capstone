# Documentation-Sync Pipeline State

This is the canonical record for pipeline stage statuses, evidence references, dependencies, blockers, approvals, and state changes. The `pipeline-state` agent maintains the complete record here. Only implementation-task execution events are kept in append-only `logs/task-<id>.log` files by the orchestrator; see [implementation logging](implementation-logging.md). Other stages do not have log files.

## Status Key

- **Not started** — work has not begun.
- **In progress** — authorized work is underway.
- **Blocked** — a prerequisite, approval, or capability prevents progress.
- **Complete** — required work and evidence have been verified.
- **Not assessed** — the available evidence has not yet been reviewed sufficiently to assign a pipeline status.

## Stage Register

| Stage | Status | Dependencies | Artifact(s) | Verification / event-log evidence | Human approval | Blockers / missing evidence |
|---|---|---|---|---|---|---|
| 1. Requirements | Not assessed | Proposed story; explicit user confirmation of clarified decisions | `docs/requirements.md` | Not assessed; no stage log | Not assessed | Pipeline-state agent to assess available evidence |
| 2. Architecture | Not assessed | Approved requirements; approval of material design choices | `docs/architecture.md` | Not assessed; no stage log | Not assessed | Pipeline-state agent to assess available evidence |
| 3. Design review | Not assessed | Requirements and proposed/accepted architecture | `docs/design-review.md` | Not assessed; no stage log | Not assessed | Pipeline-state agent to assess available evidence |
| 4. Planning | Not assessed | Approved requirements, architecture, and design-review findings | `docs/impl-plan.md` | Not assessed; no stage log | Not assessed | Pipeline-state agent to assess available evidence |
| 5. Implementation | Not assessed | Approved plan tasks and accepted architecture; approval for significant changes | Source and test files in their established project locations | `logs/task-1.log` through `logs/task-10.log`, plus `logs/task-3a.log` (not assessed) | Not assessed | Pipeline-state agent to assess available evidence |
| 6. Code review | Not assessed | Implementation diff, plan, requirements, and available tests | Review findings recorded in `docs/` when an artifact is created | Not assessed; no stage log | Not assessed | Pipeline-state agent to assess available evidence |
| 7. Verification | Not assessed | Implementation, tests, requirements, and documentation output | Verification reports in `docs/` when an artifact is created | Not assessed; no stage log | Not assessed | Pipeline-state agent to assess available evidence |
| 8. PR preparation | Not assessed | Reviewed implementation and actual verification evidence; separate explicit approval before publication | PR-preparation artifacts in `docs/` when requested or created | Not assessed; no stage log | Not assessed | Pipeline-state agent to assess available evidence |

## Task Log and State-Change History

No implementation-task log entries or status changes have been assessed in this initial record. The orchestrator appends only actual implementation-task events to the task-specific logs. For each state update, record the date, stage, change or task outcome, supporting artifact/evidence, approval evidence where applicable, and remaining blockers. Do not record unverified claims as evidence.

## Approval Register

No approval decisions have been assessed in this initial record. Record each required human approval with its stage, exact decision, date or conversation reference, and scope. Silence, handoffs, agent recommendations, and planned actions are not approvals.
