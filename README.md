# Veritas Gate

![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white) ![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-2088FF?logo=githubactions&logoColor=white) ![OpenSpec](https://img.shields.io/badge/OpenSpec-enforced-blueviolet) ![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

> **Trust, but verify.**

Veritas Gate is an extensible evaluation and quality-gate framework for
AI-generated and human-generated artifacts.

Instead of asking the system that generated an artifact whether the artifact is
good, Veritas Gate sends it through independent judges, deterministic checks,
evidence verification, consensus analysis, and policy-based gates.

```text
Generator
    │
    ▼
Artifact
    │
    ├── Judge
    ├── Judge
    ├── Judge
    ├── Deterministic checks
    │
    ▼
Meta Review
    │
    ▼
Quality Gate
```

---

## Quick start

```bash
pip install veritas-gate

cp .env.example .env        # add the API key for whichever provider you use
set -a && . ./.env && set +a

veritas init
veritas evaluate .
```

From a clone:

```bash
git clone https://github.com/arananet/veritas-gate.git
cd veritas-gate
pip install -e ".[dev]"

veritas evaluate examples/paper --profile scientific-paper
```

`examples/paper` is a deliberately flawed manuscript: its headline number
contradicts its own benchmark file, its code implements a different method than
it describes, and its improvement rests on a single unseeded run. It exists so
you can see the gate do its job on the first run.

---

## What it does

```text
Veritas Gate
Trust, but verify.

Profile:  scientific-paper
Artifact: examples/paper

Running deterministic checks...
  ✓ repository-structure
  ✓ required-sections
Running judges...
  ✗ methodology
  ✗ evidence
  ✗ statistics
  ✗ reproducibility
  ✗ repo-consistency
  ! citations
  ✗ adversarial
Building claim graph...
  ✓ claim graph
Meta review...
  ✓ meta review

3 claims detected
  ✓ 0 verified
  ! 0 partially supported
  ✗ 2 unsupported
  evidence coverage: 0.0%

Findings:
CRITICAL    2
MAJOR       7
MINOR       1
INFO        0

Blocking findings:

  [EVIDENCE-001] Headline latency reduction is contradicted by the results file
    paper/main.md#results
  [REPO-CONSISTENCY-001] Implementation does not match the described method
    experiments/router.py

╭─ Gate ─╮
│ FAIL   │
╰────────╯
```

---

## Design principles

1. **Evaluation and generation are separate.** Nothing in this package writes
   or repairs an artifact. A judge is never allowed to fix what it is judging.
2. **Judges produce structured findings, not prose.** Every finding carries a
   severity, a category, a location, evidence and a confidence.
3. **Evidence outweighs scores.** A finding that cites nothing is kept, but its
   confidence is reduced. A claim with no evidence is never reported as verified.
4. **Critical findings cannot disappear through averaging.** The meta review
   merges duplicates by taking the *worst* severity any judge reported, and
   records every disagreement rather than resolving it silently.
5. **Gate decisions are deterministic.** The pass/fail decision is Python, not
   a model call. The same findings and policy always produce the same exit code.
6. **Artifact content is untrusted.** Everything read from an artifact is
   wrapped in an injection-resistant envelope, and nothing executes unless you
   put it on an allow-list.
7. **Models are interchangeable.** Anthropic, OpenAI, Google and any
   OpenAI-compatible endpoint, selected per judge in configuration.
8. **Profiles are configuration, not code.** See below.
9. **Every evaluation is reproducible.** Each run records models, prompt
   digests, judge versions, profile version, config and artifact commit.
10. **Repair is separate from evaluation.** A run emits `repair-plan.json`;
    applying it is a different, explicitly invoked step.

---

## Profiles are plugins

`scientific-paper` is not the core — it is the first profile. The engine knows
about artifacts, judges, checks, claims and gates, and nothing about papers.
A profile is a directory:

```text
profiles/<name>/
├── profile.yaml      # judges, checks, gate policy
├── rubric.yaml       # what each severity means in this domain (optional)
└── prompts/
    └── <judge>.md    # one versioned prompt per judge
```

Adding a domain means adding a directory. It requires no change to the
orchestration engine — that is a hard requirement, covered by tests.

```bash
veritas evaluate . --profile scientific-paper
veritas evaluate . --profile architecture-review
veritas evaluate . --profile agent
veritas evaluate . --profile repository
veritas evaluate proposal.md --profile technical-proposal
```

Profiles are discovered from `profile_paths:` in your config, `./profiles/`,
the `VERITAS_PROFILE_PATH` environment variable, the profiles bundled with the
package, and `veritas.profiles` entry points published by installed plugin
packages — so `veritas-security` or `veritas-arxiv` can ship profiles of their
own without forking this repository.

Shipped today: `scientific-paper` (methodology, evidence, statistics,
reproducibility, repo-consistency, citations, adversarial) and
`generic-document` (structure, evidence, adversarial).

---

## Configuration

`veritas.yaml` says what to evaluate, with which models, under which policy.
Secrets never appear in it — only the names of environment variables.

```yaml
version: 1

profile: scientific-paper

artifact:
  paths: [paper, experiments, results, scripts]

models:
  default:
    provider: ${VERITAS_DEFAULT_PROVIDER:-anthropic}
    model: ${VERITAS_DEFAULT_MODEL}
  adversarial:                      # a different vendor, on purpose
    provider: openai
    model: ${VERITAS_ADVERSARIAL_MODEL}
  meta:
    provider: google
    model: ${VERITAS_META_MODEL}

gate:
  fail_on: [critical]
  max_major: 0
  max_minor: 20
  require_checks: [tests]

checks:
  tests:
    type: command
    command: [pytest]

execution:
  allow: [pytest]                   # nothing runs unless it is listed here
```

Model ids are never hard-coded. Copy `.env.example`, choose your models, and
point each judge role at whichever provider you want. The adversarial reviewer
defaults to a different vendor than the other judges so that one model's blind
spots do not decide the outcome.

---

## CLI

| Command | Purpose |
| --- | --- |
| `veritas init` | Write `veritas.yaml` and `.veritas/` |
| `veritas evaluate <path>` | Run the evaluation and write an immutable run |
| `veritas evaluate . --profile <name>` | Evaluate under a specific profile |
| `veritas evaluate . --judge evidence --runs 3` | Calibrate one judge and record its stability |
| `veritas report` | Show the latest report |
| `veritas claims` | Show the claim graph and evidence coverage |
| `veritas findings --severity major` | Filter the latest findings |
| `veritas gate` | Print the gate status and exit with its code |
| `veritas repair-plan` | Print the advisory repair plan |
| `veritas profiles` | List every discoverable profile |

Exit codes: `0` pass, `1` pass with warnings, `2` revise, `3` fail, `4` usage
or configuration error.

---

## Every run is reproducible

```text
.veritas/runs/2026-09-21T221215Z/
├── manifest.json      # models, prompt digests, judge versions, artifact commit
├── results.json       # the complete result
├── gate.json          # the gate decision
├── meta.json          # consolidation, consensus, disagreements
├── claims.json        # the claim graph and evidence coverage
├── judges/            # each judge's raw structured result
├── checks/            # each deterministic check's result
├── repair-plan.json   # advisory; Veritas never applies it
└── report.md
```

Runs are never overwritten.

---

## CI

```yaml
name: Veritas Gate

on:
  pull_request:
  push:
    branches: [main]

jobs:
  evaluate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e .
      - run: veritas evaluate .
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

`veritas evaluate` returns a non-zero exit code when the gate does not pass, so
the job fails on its own. A ready-to-copy workflow lives in
[`.github/workflows/veritas-gate.yml.example`](.github/workflows/veritas-gate.yml.example).

---

## Security

Assume an evaluated artifact is hostile. Veritas Gate:

- wraps all artifact content in an `<untrusted_artifact_content>` envelope, and
  instructs every judge that text inside it is data and must never be obeyed;
- assigns finding ids itself, so a model cannot spoof or collide with another
  judge's output;
- executes nothing by default — a command check refuses to run unless its
  executable is listed in `execution.allow`;
- passes no environment to a check beyond `PATH`, `HOME`, `LANG`, `LC_ALL`,
  `TMPDIR` and whatever `execution.env_passthrough` names, so your API keys do
  not leak into a subprocess;
- reads secrets only from environment variables, never from configuration.

Report vulnerabilities through [GitHub private vulnerability reporting](https://github.com/arananet/veritas-gate/security/advisories/new).
See [`SECURITY.md`](SECURITY.md).

---

## Development

```bash
pip install -e ".[dev]"

pytest              # the full suite runs offline, with no API keys
ruff check src tests
mypy
```

Provider adapters are tested against a real local HTTP server rather than a
patched client, so the request payloads and response parsing under test are the
ones that would go over the wire.

This project uses **OpenSpec**: every feature or bugfix starts with a spec under
`.openspec/specs/`. See [`docs/OPENSPEC.md`](docs/OPENSPEC.md) for the workflow
and [`CONTRIBUTING.md`](CONTRIBUTING.md) for the contributor checklist.

---

## Documentation

| Topic | Where |
| --- | --- |
| Writing a profile | [`docs/PROFILES.md`](docs/PROFILES.md) |
| Spec-driven workflow | [`docs/OPENSPEC.md`](docs/OPENSPEC.md) |
| Small-project adoption | [`docs/ADOPTION.md`](docs/ADOPTION.md) |
| Branch protection setup | [`docs/BRANCH_PROTECTION.md`](docs/BRANCH_PROTECTION.md) |
| Architecture decisions | [`docs/adr/`](docs/adr/) |
| Security policy | [`SECURITY.md`](SECURITY.md) |
| Secrets handling | [`SECRETS.md`](SECRETS.md) |
| Release history | [`CHANGELOG.md`](CHANGELOG.md) |

---

## Status

v0.1. Not implemented yet, by design: automatic repair (`veritas repair`) and
any web UI. The CLI comes first.

---

## License

[MIT](LICENSE)

---

## Developer

Eduardo Arana

## Support this with a ko-fi

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/H2H51MPWG)
