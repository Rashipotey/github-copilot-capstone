# Implementation Task Logs

The root `logs/` folder contains only append-only implementation-task logs, one per task ID in [the implementation plan](impl-plan.md). Do not create stage-wide logs, logs for other SDLC stages, or documentation inside that folder.

## Task mapping and dependency order

| Task | Log file | Prerequisites |
|---|---|---|
| 1. Package and Test Foundation | `logs/task-1.log` | None |
| 2. CLI, Configuration, Result Contracts, and Resource Defaults | `logs/task-2.log` | Task 1 |
| 3. Safe Repository, Path, and Git-Diff Handling | `logs/task-3.log` | Tasks 1–2 |
| 4. Secret Screening and Fail-Closed Gate | `logs/task-4.log` | Tasks 1–3 |
| 3a. Scope-Correct Content Snapshots and Current Markdown Candidate Inventory | `logs/task-3a.log` | Tasks 1–4 |
| 5. Bounded Evidence Collection and Documentation Target Selection | `logs/task-5.log` | Tasks 1–4 and 3a |
| 6. Deterministic Allowlisted Proposal Rules | `logs/task-6.log` | Tasks 1–5, including 3a |
| 7. Three Independent Validators | `logs/task-7.log` | Tasks 1–6, including 3a |
| 8. Secure Report and Patch Output | `logs/task-8.log` | Tasks 1–7, including 3a |
| 9. CLI Pipeline Integration and End-to-End Behavior | `logs/task-9.log` | Tasks 1–8, including 3a |
| 10. Optional GitHub Actions and Security Checks | `logs/task-10.log` | Tasks 1–9, including 3a |

Use the plan's actual prerequisites, not filename sorting: Task 3a follows Task 4. Task 10 remains optional to local functionality. If approved tasks change, update this mapping and add one matching task log per new task ID.

## Ownership and scope

The orchestrator is the sole log writer. It appends actual implementation-task starts, delegations, results, errors, blockers, task-specific approval decisions, and checks performed as part of implementation. Each event belongs to exactly the task it concerns. Shared evidence may be referenced in multiple task logs with an explanation of its relevance.

Do not log requirements, architecture, design review, planning, standalone code review, standalone verification, PR preparation, or compliance-audit activity here. Keep their concise statuses, approvals, and evidence references in `docs/pipeline-state.md` through `pipeline-state`, with detailed artifacts under `docs/` where appropriate.

## Event format

```text
## <short event title>
- Timestamp: <actual ISO 8601 date-time with UTC offset>
- Task: <plan task ID and title>
- Agent: <identity of the agent that performed the action>
- Event: <task started | delegation requested | result received | blocked | error | check run | other observed implementation-task event>
- Summary: <brief-to-detailed factual account, with relevant artifact/evidence references>
- Outcome: <actual completed, in-progress, blocked, failed, partial, or other observed result>
- Errors: <sanitized actual error details, or None>
```

Append at the end; never truncate, rewrite, reorder, or silently correct existing entries. Append a referenced correction if necessary. Do not invent historical execution, timestamps, outcomes, errors, or approvals. These initial logs are empty and do not retroactively establish evidence for tasks recorded complete in the plan.

Exclude credentials, secret values, and unnecessary personal data. Distinguish delegation requested from work actually performed. Verify each append succeeded; if it fails, report the error and stop implementation progression until logging is restored. The `pipeline-state` agent reads logs as evidence but does not edit them.
