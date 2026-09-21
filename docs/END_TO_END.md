# End-to-end walkthrough

A complete run against a real repository, using the commands as they actually
exist. Every step below is one you can copy.

## 1. Go to the repository

```bash
cd /path/to/my-paper-repo
```

## 2. Check the git state

```bash
git status
```

The repository may contain modified files, untracked files, an uncommitted
manuscript, or uncommitted experiment results. **No commit is required before
running Veritas.** Uncommitted and untracked work is part of the artifact, and
`workspace.include_untracked` (on by default) keeps it that way.

Git is optional throughout. Without it you lose patches and commit SHAs in the
run metadata; everything else works unchanged.

## 3. Check the installation

```bash
veritas --help
veritas --version
```

## 4. Initialize the configuration

Only the first time:

```bash
veritas init --profile scientific-paper
```

This writes:

```text
veritas.yaml
.veritas/
```

## 5. Inspect the configuration

```bash
cat veritas.yaml
```

For a paper repository, point the artifact at the manuscript, the code and the
evidence:

```yaml
version: 1

profile: scientific-paper

artifact:
  type: document
  paths:
    - paper           # the manuscript
    - experiments     # the implementation
    - results         # the evidence
    - scripts

models:
  default:
    provider: ${VERITAS_DEFAULT_PROVIDER:-anthropic}
    model: ${VERITAS_DEFAULT_MODEL}
  adversarial:
    provider: openai
    model: ${VERITAS_ADVERSARIAL_MODEL}
  meta:
    provider: google
    model: ${VERITAS_META_MODEL}

workspace:
  mode: worktree            # worktree | snapshot | copy | current
  include_untracked: true
  respect_gitignore: true
  respect_veritasignore: true

loop:
  max_iterations: 5

gate:
  fail_on: [critical]
  max_major: 0

execution:
  allow: []                 # nothing runs unless you list it here
```

Set your credentials from the environment, never in this file:

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY
set -a && . ./.env && set +a
```

## 6. Audit mode: evaluate only

Before letting anything modify files, run the judges on their own:

```bash
veritas evaluate . --profile scientific-paper
```

The flow is: artifact discovery → deterministic checks → independent judges →
findings → claim graph → meta review → quality gate.

```text
Findings:
CRITICAL    0
MAJOR       3
MINOR       5

╭─ Gate ───╮
│ REVISE   │
╰──────────╯
```

This command **never modifies your files.**

## 7. Inspect the findings

```bash
veritas findings
veritas findings --severity major
veritas findings --severity major --json
```

## 8. Inspect the claim graph

```bash
veritas claims
```

```text
Claims: 42

Verified:             36
Partially supported:   3
Unsupported:           2
Unverified:            1

Evidence coverage:    82.0%
```

Evidence coverage is a diagnostic, not a quality score. The findings stand on
their own.

## 9. Inspect the gate

```bash
veritas gate
echo $?
```

```text
REVISE
2
```

Exit codes: `0` pass, `1` pass with warnings, `2` revise, `3` fail, `4` usage or
configuration error. This is what makes the command usable directly in CI.

## 10. Read the report

```bash
veritas report          # rendered
veritas report --raw    # the Markdown
```

The full run is on disk and immutable:

```text
.veritas/runs/2026-09-21T221215Z/
├── manifest.json     models, prompt digests, judge versions, artifact commit
├── results.json
├── gate.json
├── claims.json
├── judges/
├── checks/
├── repair-plan.json
└── report.md
```

## 11. Assist mode: see what would change

```bash
veritas repair-plan .
```

Evaluates and writes `.veritas/repair-plan.json`, showing which actions are
autonomous and which need you:

```text
ID          Priority  Type           Findings          Autonomous
ACTION-001  blocking  artifact_edit  STATS-004         yes
ACTION-002  blocking  experiment     EVIDENCE-013      human

ACTION-002: Resolving this finding requires evidence that does not exist in the
artifact. Veritas will not fabricate measurements, results, citations or
datasets to satisfy its own evaluator.
```

This command **never modifies your files.**

## 12. Configure the repair agent

Autopilot needs a worker. Any CLI coding agent will do:

```yaml
repair:
  mode: autopilot
  agent:
    provider: generic-cli
    command: [codex, exec, "{prompt_file}"]
  permissions:
    documentation: true
    source_code: true
    tests: true
    experiments: false
    datasets: false
    scientific_claims: false
    methodology: false
```

Available placeholders: `{prompt_file}`, `{plan_file}`, `{result_file}`,
`{workspace}`. Swapping the tool is one line:

```yaml
    command: [claude, -p, "{prompt_file}"]
```

## 13. Dry run first

```bash
veritas loop . --dry-run
```

Evaluates, plans, prints the proposed actions, and stops. Nothing is applied,
and no agent is invoked at all.

## 14. Autopilot

```bash
veritas loop . --profile scientific-paper --max-iterations 5
```

```text
Veritas Autopilot

Profile:  scientific-paper
Artifact: .
Budget:   up to 5 iteration(s)

──────────────── Iteration 1 / 5 ────────────────
Evaluating...
  Critical: 0  Major: 4  Minor: 7
  Gate: REVISE

Repair plan:
  4 autonomous action(s)
  1 human-only action(s)

Applying autonomous repairs...
  ✓ ACTION-001
  ✓ ACTION-002
  ✓ ACTION-003
  ✓ ACTION-004
  3 file(s) changed

──────────────── Iteration 2 / 5 ────────────────
Evaluating...
Re-evaluating...
  Resolved:   3
  Improved:   1
  Unchanged:  1
  Regressed:  0
  New:        1
...

╭─ STOP ──────────────────────╮
│ HUMAN_DECISION_REQUIRED     │
╰─────────────────────────────╯

ACTION-005 (EVIDENCE-017): Resolving this finding requires evidence that does
not exist in the artifact.

Autonomous generation of experimental evidence is prohibited.
Do the work by hand, then resume with: veritas loop . --resume
```

Your originals are untouched: repairs happen in a worktree or an isolated copy,
and Veritas tells you where the changes and the patch are.

## 15. Inspect what happened

```bash
veritas loop-report          # the final report
veritas diff 1 2             # compare two iterations, issue by issue
```

```text
Resolved:
  STATS-004
  REPRO-007

Improved:
  EVIDENCE-013

Unchanged:
  NOVELTY-002

Regressed:
  none

New:
  CITATION-021
```

Everything is on disk:

```text
.veritas/loops/<loop-id>/
├── loop-report.md
├── loop-result.json
├── ledger.json
├── final.patch
├── iteration-001/
└── iteration-002/
```

## 16. Do the human part, then resume

When the loop stopped for a human decision — run the experiment, add the
dataset, make the methodology call — do that work, then:

```bash
veritas loop . --resume
```

The previous ledger is carried forward, so an issue you fixed by hand is
recognised as resolved rather than reopened as new.

## 17. Wire it into CI

```bash
veritas evaluate .
```

returns non-zero when the gate does not pass, so the job fails on its own. See
[`.github/workflows/veritas-gate.yml.example`](../.github/workflows/veritas-gate.yml.example).

Run **audit mode** in CI. Autopilot modifies files and belongs on a developer's
machine or in a deliberately configured job, not in a pull-request check.
