# The bounded repair loop

## Why three systems, not one agent

```text
Judge
  ↓
Finding
  ↓
RepairPlanner
  ↓
RepairPlan
  ↓
LoopOrchestrator
  ↓
RepairAgent
  ↓
Codex / Claude Code / any CLI agent
```

Never `Judge → Codex`.

Each component has exactly one job:

| Component | Question it answers |
| --- | --- |
| `EvaluationEngine` | What is wrong with this artifact? |
| `RepairPlanner` | What concretely has to change? |
| `LoopOrchestrator` | Are we permitted to? Do we continue? |
| `RepairAgent` | Tell the external agent, and report what it did |
| `GateEngine` | Pass or fail, deterministically |
| `FindingLedger` | Is this the same issue we saw last iteration? |

The consequence: the agent that repairs is never the agent that judges the
repair, and neither of them decides whether to keep going. Two agents cannot
negotiate their way to a passing grade, because they never meet.

## A worked example

A judge reports:

```json
{
  "id": "STATS-004",
  "severity": "major",
  "description": "The manuscript reports accuracy but does not state the number of runs or variance.",
  "location": "paper/results.md",
  "evidence": ["results/run_summary.json records N=10, mean=0.873, std=0.004"]
}
```

The planner turns that into something operative:

```json
{
  "id": "ACTION-001",
  "finding_ids": ["STATS-004"],
  "priority": "blocking",
  "action_type": "artifact_edit",
  "instruction": "Problem: ...\n\nRequired action: ...\n\nUse only the evidence already present in the artifact, listed below. If a value is not there, do not invent it:\n  - results/run_summary.json records N=10, mean=0.873, std=0.004",
  "allowed_files": ["paper/results.md"],
  "available_evidence": ["results/run_summary.json records N=10, mean=0.873, std=0.004"],
  "requires_new_evidence": false,
  "requires_human_approval": false
}
```

The orchestrator checks permissions, sees `requires_human_approval: false`, and
calls:

```python
result = await repair_agent.repair(artifact=artifact, plan=plan, workspace=workspace)
```

The agent writes a prompt, runs the configured command, and the external tool
edits `paper/results.md`. It reports back:

```json
{
  "status": "completed",
  "changes": [{"file": "paper/results.md", "description": "Added N=10 and std from results/run_summary.json"}]
}
```

**Veritas does not take its word for it.** The next iteration re-runs the whole
evaluation from scratch. The judge checks independently whether `STATS-004` is
gone. Only then does the ledger record `RESOLVED`.

## The contract the agent receives

The agent is given, and only given:

- the normalized `RepairPlan` (autonomous actions only)
- the files each action may touch
- the evidence that already exists in the artifact
- its permissions
- the versioned repair prompt

It is never given: judge prose, judge reasoning, the rubric, another agent's
reasoning, or a previous iteration's repair prompts.

Symmetrically, the evaluator is never given: the repair prompt, the agent's
reasoning, or the agent's claim that something was fixed. It sees the modified
artifact and nothing else.

## Stop reasons

| Reason | Meaning |
| --- | --- |
| `quality_gate_reached` | The gate passed. |
| `max_iterations` | The iteration budget ran out. |
| `no_progress` | Repairs stopped reducing blocking findings. |
| `repeated_blocker` | The same blocker survived repeated attempts. |
| `regression` | An iteration made the artifact worse. |
| `new_critical_finding` | A repair introduced a new critical finding. |
| `human_decision_required` | A blocker needs a person. |
| `cost_limit` | The configured spend limit was reached. |
| `change_limit` | Too many files changed. |
| `repair_failure` | The agent could not apply the plan. |
| `dry_run` | A plan was produced; nothing was applied. |
| `nothing_to_repair` | The gate failed but no repairable action existed. |

## How progress is measured

By issue, never by score:

```text
Iteration 1:  5 blockers → 2 blockers     progress
Iteration 2:  2 blockers → 2 blockers     no progress
Iteration 3:  2 blockers → 3 blockers     regression
```

`consecutive_non_improving_iterations: 2` stops the loop at iteration 3 in the
first case; a regression or a new critical finding stops it immediately.

A new critical finding outweighs any improvement elsewhere. There is no
aggregate score anywhere in this decision.

## The safety invariant

> Veritas may autonomously improve how existing evidence is represented,
> implemented, documented or validated. It must never autonomously fabricate
> missing evidence in order to satisfy its own evaluator.

Three independent mechanisms enforce it:

1. **The planner** marks an action `requires_new_evidence` when resolving the
   finding would need measurements, data, citations or results the artifact does
   not contain — including the case where a judge asks for "30 seeds" and the
   evidence records 10.
2. **`enforce_permissions`** refuses any such action before it reaches an agent,
   and refuses any action type the permissions do not allow.
3. **The orchestrator** stops the loop if an agent reports
   `new_evidence_created` anyway.

The repair prompt says the same thing in words, but the prompt is the weakest of
the four — it is the one an artifact could try to talk its way around. The other
three are code.

## Permissions

```yaml
repair:
  permissions:
    documentation: true       # how something is written
    source_code: true         # how something is implemented
    tests: true               # how something is validated
    experiments: false        # what the evidence IS
    datasets: false
    scientific_claims: false
    methodology: false
```

The line between `true` and `false` is exactly the line in the invariant.

## Workspaces

```yaml
workspace:
  mode: worktree   # worktree | snapshot | copy | current
```

- `worktree` — a git worktree at the current commit. Your working tree is never
  touched. Degrades to `copy` when git is unavailable.
- `snapshot` / `copy` — an isolated copy of the tree as it stands, uncommitted
  work included.
- `current` — in place. Use it when you have your own backup discipline.

Veritas never pushes, merges, commits to your branches or rewrites history. It
writes `changes.patch` per iteration and `final.patch` for the loop, and tells
you where they are.

## Adding a repair agent

Implement the protocol:

```python
class RepairAgent(Protocol):
    name: str

    async def repair(
        self,
        artifact: Artifact,
        plan: RepairPlan,
        workspace: Workspace,
    ) -> RepairResult: ...
```

Register it in `veritas.repair.build_repair_agent`. The orchestrator is not
coupled to any implementation, which is why `GenericCLIRepairAgent` ships
instead of a `CodexRepairAgent`: a command template covers Codex, Claude Code
and anything else with a CLI, and a vendor-specific agent can be added later
without touching a line of orchestration.
