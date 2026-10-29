# DOCSYNC-2 Implementation Logging

**Status:** The plan is approved; only I-01 is authorized. Each remaining task still requires explicit approval before it starts.  
**Scope:** Actual implementation-task events for active story DOCSYNC-2 only. This guide does not authorize implementation, task starts, or log creation.  
**Source of task scope:** Only the tasks and prerequisites in this story's `docs/impl-plan.md` and the shared SDLC workflow skill. Do not use another story's guide or logs.

## Task-to-log mapping and prerequisites

Create a task log only after the user explicitly approves both the plan and that specific task, its listed predecessor tasks are complete, and the task actually starts. No unresolved technical decision gate remains; approval and dependency gates still apply.

| Plan task ID and exact title | Log file | Prerequisites before any event/task start |
|---|---|---|
| **I-01 — Add isolated drift CLI/configuration** | `logs/task-I-01.log` | Explicit user approval of `docs/impl-plan.md` and I-01; no task predecessor. |
| **I-02 — Acquire and normalize the approved change set** | `logs/task-I-02.log` | Explicit plan and I-02 approval; I-01 complete. Implement the accepted G-01/G-03 changed-input and rename mapping. |
| **I-03 — Collect bounded screened evidence** | `logs/task-I-03.log` | Explicit plan and I-03 approval; I-02 complete. Apply accepted scope, screening, safe-read, and G-05 classifications. |
| **I-04 — Match candidates and handle selection** | `logs/task-I-04.log` | Explicit plan and I-04 approval; I-03 complete. Apply accepted G-02/G-04 exact matching and selection rules. |
| **I-05 — Model findings and render analysis status** | `logs/task-I-05.log` | Explicit plan and I-05 approval; I-03 and I-04 complete. Apply accepted complete/partial/fatal and candidate-only rules. |
| **I-06 — Publish report with containment and no-replace** | `logs/task-I-06.log` | Explicit plan and I-06 approval; I-01 and I-05 complete. Apply accepted G-05/G-06 publication and path-safety rules. |
| **I-07 — Complete AC-mapped integration and preservation tests** | `logs/task-I-07.log` | Explicit plan and I-07 approval; I-01–I-06 complete. Run the proposed AC-mapped tests and report observed results accurately. |

G-01–G-08 are resolved planning decisions, not tasks and have no task logs. DR3-01/G-04 and DR3-02/G-05 are resolved by the accepted architecture and approved review; neither is an open blocker. DR4-01 is a low-severity history wording issue corrected/superseded by current pipeline state, not a technical blocker. Plan and task approvals remain pending; all tasks are not authorized. No task logs or events have been created.

## Event schema

Each event records:

- **Timestamp:** Actual event time with UTC offset (for example, ISO 8601 `2026-10-29T12:00:00+00:00`); never estimate, backfill, or copy a planned timestamp.
- **Task ID and title:** Exact approved plan identifier and title.
- **Agent:** Actual responsible agent/actor.
- **Event type:** Actual event, such as `start`, `progress`, `check`, `blocked`, `failure`, `outcome`, or `approval`.
- **Summary/evidence:** Concise factual description and references to actual evidence/artifacts/checks; distinguish observed results from plans.
- **Outcome:** Observed result, including blocked, partial, passed, failed, skipped, or unavailable as applicable.
- **Sanitized errors:** Actual error information with secrets, credentials, environment values, and other sensitive content removed; state `none observed` only when verified.

Suggested entry form:

```text
## <actual timestamp with UTC offset>
- Task: <ID> — <exact title>
- Agent: <actual agent>
- Event type: <actual event type>
- Summary/evidence: <factual summary and evidence references>
- Outcome: <observed outcome>
- Sanitized errors: <sanitized observed errors, or none observed>
```

## Append-only and verification rules

- The orchestrator is the sole writer to `<story_root>/logs/`. Planning, review, verification, and other non-implementation stages do not create or append task events.
- Before delegating/starting an explicitly approved, dependency-ready task, append its actual start event to the mapped log. Verify the append succeeded and retained all prior bytes unchanged before the task starts.
- After the task returns, append the observed outcome, actual checks/results, evidence references, and sanitized errors. Verify the append succeeded and retained the prior content unchanged before any dependent task proceeds.
- Append only at the end. Never truncate, rewrite, reorder, replace, backfill, or invent events. Record corrections as new events referencing the original; preserve the original.
- Record only actual events and evidence. Do not log approvals unless explicitly given and their scope is recorded accurately; plan approval is not automatically approval of every task.
- If an append or preservation check fails, stop task progression, report the failure, and do not claim the log was updated. Do not create logs for blocked, unapproved, or merely planned tasks.
