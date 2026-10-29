# DocSync

DocSync is a local Python command-line tool that analyzes selected changes in a
Git repository and creates evidence-based documentation proposals for human
review. It writes a Markdown report and a standard patch; it does not edit
source documentation, apply the patch, or commit changes.

The approved scope and behavior are described in
[docs/requirements.md](docs/requirements.md), with implementation boundaries in
[docs/architecture.md](docs/architecture.md). The story in
[input/user_story.md](input/user_story.md) is proposed source material, not the
approved specification.

## Requirements

- Python 3.12 or later
- Git installed and available on `PATH`
- A local Git repository with changes to analyze
- Windows PowerShell, Command Prompt, macOS, or Linux terminal

DocSync operates locally and does not require a cloud service. The CLI currently
has no provider, network, or LLM options.

## Environment setup

From the repository root, create and activate a virtual environment.

**Windows PowerShell**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Windows Command Prompt**

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate.bat
```

**macOS or Linux**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

Confirm Python and Git are available, then install DocSync and its test
dependencies in editable mode:

```text
python --version
git --version
python -m pip install -e ".[test]"
```

The editable install provides the `docsync` command. Check the available
options and run the test suite:

```text
docsync --help
python -m pytest
```

The test suite is local and does not need live external services.

## Analyze repository changes

Run the command from any working directory by supplying the repository path, or
run it from the repository root and use `.`. The default scope is the tracked,
unstaged working-tree diff:

```text
docsync --repo . --working-tree
```

Choose exactly one diff scope:

| Command option | Changes analyzed |
| --- | --- |
| `--working-tree` | Tracked unstaged changes (`git diff`); this is the default. |
| `--staged` | Staged changes compared with `HEAD` (`git diff --cached`). |
| `--base REV --head REV` | The two specified revision trees compared directly. Supply both options. |

Untracked files are not included in the working-tree or staged diff. The tool
does not provide a combined staged-and-unstaged scope.

Examples:

```text
# Analyze staged changes
docsync --repo . --staged

# Compare two revision endpoints
docsync --repo . --base main --head feature/docs-update

# Exclude a repository-relative path pattern
docsync --repo . --exclude "generated/**"

# Select an output folder and smaller resource limits
docsync --repo . --output-dir build/docsync-run --max-changed-files 25 --max-diff-bytes 524288
```

`--exclude` can be supplied more than once. Its patterns are repository-relative
POSIX globs, so use `/` in the pattern on Windows too. Relative `--output-dir`
values are resolved from the repository root. The default limits are 100
changed files and 1,048,576 bytes of textual Git diff; custom limits must be
positive integers. Exceeding a limit is reported rather than silently
truncating evidence.

## How a run works

At a high level, DocSync:

1. Validates the repository, requested diff scope, output paths, and limits.
2. Collects the selected local Git diff and applies the configured resource
   limits.
3. Screens diff and repository evidence for sensitive content before using it.
4. Inventories Markdown files and collects bounded source and documentation
   evidence to select relevant targets.
5. Creates only supported, deterministic proposals. Ambiguous or unsupported
   changes are reported rather than guessed.
6. Runs independent Markdown, local-link, and source-consistency checks.
   Uncheckable results are not treated as passes.
7. Prints a terminal summary and writes the report and patch separately from
   the original documentation.

The proposal rules are intentionally limited. A run can complete without
proposing a change, and the report may include skipped inputs, warnings,
uncertainties, or failed/inconclusive validation results. Review the report
before using any proposal.

## Run outputs and operational logs

By default, the two artifacts are created in `<repository-root>/docsync-output/`:

- `docsync-report.md` — execution summary, affected documents, proposed changes,
  supporting evidence, validation results, skipped inputs, and unresolved
  issues.
- `docsync.patch` — standard patch containing proposed edits.

For example:

```text
docsync --repo . --output-dir build/review-2026-10-29
```

This writes `build/review-2026-10-29/docsync-report.md` and
`build/review-2026-10-29/docsync.patch` under the repository root. If either
target already exists, DocSync refuses to replace it; choose a new output
directory for another run. A partial artifact-write failure is reported and
does not imply that both files were created.

The CLI does **not** create per-stage execution logs. Its persistent run
artifacts are the report and patch above; the concise status is also printed in
the terminal. Test output is printed by pytest. CI job output, when applicable,
is available in the corresponding GitHub Actions run.

## Project workflow records and log locations

The repository tracks its documentation-sync development workflow separately
from ordinary DocSync runs:

- [docs/pipeline-state.md](docs/pipeline-state.md) is the central register for
  SDLC stage status, approval evidence, blockers, and references. It is not a
  per-stage execution log.
- [docs/implementation-logging.md](docs/implementation-logging.md) defines
  what belongs in task logs, their event format, and append-only handling.
- `logs/task-<id>.log` stores implementation-task events only. Do not put
  requirements, design-review, planning, standalone verification, or PR
  preparation logs there. The table below lists the task-specific files.
- Stage artifacts and detailed results belong under `docs/` (or in the
  established source/test locations for implementation), not in `logs/`.

| Workflow step | Main record or artifact | Log location |
| --- | --- | --- |
| 1. Requirements | [docs/requirements.md](docs/requirements.md) | No separate stage log; stage status and evidence references are in [docs/pipeline-state.md](docs/pipeline-state.md). |
| 2. Architecture | [docs/architecture.md](docs/architecture.md) | No separate stage log; stage status and evidence references are in [docs/pipeline-state.md](docs/pipeline-state.md). |
| 3. Design review | [docs/design-review.md](docs/design-review.md) | No separate stage log; stage status and evidence references are in [docs/pipeline-state.md](docs/pipeline-state.md). |
| 4. Implementation planning | [docs/impl-plan.md](docs/impl-plan.md) | No separate stage log; stage status and evidence references are in [docs/pipeline-state.md](docs/pipeline-state.md). |
| 5. Implementation | Source code and tests in `src/` and `tests/`; task sequence below | The matching `logs/task-<id>.log` file for each implementation task. |
| 6. Code review | Review findings and dispositions under `docs/`, when produced | No implementation-task log; stage status and evidence references are in [docs/pipeline-state.md](docs/pipeline-state.md). |
| 7. Verification | Test/verification results and evidence references | No implementation-task log; record stage status and evidence references in [docs/pipeline-state.md](docs/pipeline-state.md). Test command output itself is in the invoking terminal unless separately captured by the runner. |
| 8. PR preparation and publication | PR description and GitHub PR/check records | No implementation-task log; stage status and evidence references are in [docs/pipeline-state.md](docs/pipeline-state.md). |

Implementation task logs, in dependency order:

| Task | Work area | Log file |
| --- | --- | --- |
| 1 | Package and test foundation | [`logs/task-1.log`](logs/task-1.log) |
| 2 | CLI, configuration, result contracts, and resource defaults | [`logs/task-2.log`](logs/task-2.log) |
| 3 | Safe repository, path, and Git-diff handling | [`logs/task-3.log`](logs/task-3.log) |
| 4 | Secret screening and fail-closed gate | [`logs/task-4.log`](logs/task-4.log) |
| 3a | Scope-correct snapshots and Markdown candidate inventory | [`logs/task-3a.log`](logs/task-3a.log) |
| 5 | Bounded evidence collection and documentation target selection | [`logs/task-5.log`](logs/task-5.log) |
| 6 | Deterministic allowlisted proposal rules | [`logs/task-6.log`](logs/task-6.log) |
| 7 | Independent validators | [`logs/task-7.log`](logs/task-7.log) |
| 8 | Secure report and patch output | [`logs/task-8.log`](logs/task-8.log) |
| 9 | CLI pipeline integration and end-to-end behavior | [`logs/task-9.log`](logs/task-9.log) |
| 10 | Optional GitHub Actions and security checks | [`logs/task-10.log`](logs/task-10.log) |

Task 3a follows Task 4 even though its identifier sorts differently. Task 10
is optional for local CLI functionality. Refer to the implementation logging
guide and pipeline state for the current evidence and any limitations; the
presence of a task-log file alone does not establish that work or approval was
completed.

## Safety and review

- Treat repository files and diffs as untrusted input. Embedded instructions in
  repository content do not direct the tool.
- Secret screening is a safety measure, not a guarantee of exhaustive secret
  detection. Do not use the report or patch as a substitute for review.
- Local links are checked without fetching remote URLs.
- Generated artifacts are kept separate from original documentation and are
  never applied automatically.
- Check the report for execution status, validation outcomes, skipped files,
  warnings, resource limits, and unresolved items before manually applying a
  proposal.