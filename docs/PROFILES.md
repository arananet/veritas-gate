# Writing a profile

A profile is how Veritas Gate learns a new domain. It is a directory of
configuration and prompts. Adding one requires no change to the engine.

## Layout

```text
profiles/<name>/
├── profile.yaml      # required: judges, checks, gate policy
├── rubric.yaml       # optional: what each severity means here
└── prompts/
    └── <judge>.md    # one versioned prompt per judge
```

## profile.yaml

```yaml
profile: architecture-review
version: "1"
description: Review an architecture document against operational reality.
artifact_type: document
default_paths:
  - .

meta_model_role: meta

judges:
  - name: requirements
    prompt: requirements.md
    model_role: requirements     # which models.<role> entry to use
    version: "1"
    extracts_claims: false       # only claim-extracting judges feed the graph
    description: Whether the stated requirements are complete and testable.

  - name: adversarial
    prompt: adversarial.md
    model_role: adversarial

checks:
  required-sections:
    type: required-sections      # command | required-paths | required-sections
    enabled: true
    severity_on_failure: major
    required_sections: [context, decision, consequences, rollback]

gate:
  fail_on: [critical]
  max_major: 0
  max_minor: null                # null means no limit
  require_checks: []
  min_evidence_coverage: null
  warn_on_minor: true
```

Judges are declarative. There is no Python subclass to write: `LLMJudge` is
instantiated from the prompt file and the model role.

## Check types

| Type | What it asserts | Runs a subprocess |
| --- | --- | --- |
| `required-paths` | Configured paths exist in the artifact | no |
| `required-sections` | A target document contains configured sections | no |
| `content-patterns` | A target's text matches (or avoids) regular expressions | no |
| `command` | An allow-listed command exits zero | yes |

`content-patterns` is the cheap half of archival review — whether a manuscript
names a DOI, cites a commit rather than a branch, or has a data availability
statement at all:

```yaml
checks:
  citability:
    type: content-patterns
    target: paper/manuscript.md
    severity_on_failure: minor
    case_sensitive: false
    required_patterns:
      - pattern: '10\.\d{4,}/'
        description: a DOI for the archived work
    forbidden_patterns:
      - pattern: '(?i)github\.com/\S+/(blob|tree)/(main|master)/'
        description: a link to a mutable branch instead of a pinned commit
```

Invalid regular expressions are rejected when the configuration loads, not
part-way through a paid run. A forbidden match quotes the line it found, so the
finding says where and not only that. A target that cannot be read reports an
error rather than passing: a check that could not run is not a check that passed.

## Prompts

Each prompt is the judge's system instructions. Veritas prepends the security
preamble and the evaluation rules to every one of them, so do not repeat those.
Write what *this* judge should look for, and what each severity means for it.

Prompts are versioned by content: each run records a digest of every prompt in
`manifest.json`, so a change to a prompt is visible in the run history.

## Model roles

A judge names a `model_role`. Configuration maps roles to providers and models:

```yaml
models:
  requirements:
    provider: anthropic
    model: ${VERITAS_REQUIREMENTS_MODEL}
  adversarial:
    provider: openai
    model: ${VERITAS_ADVERSARIAL_MODEL}
  default:              # fallback for any role not named explicitly
    provider: google
    model: ${VERITAS_DEFAULT_MODEL}
```

Routing the adversarial judge to a different vendor than the others is
deliberate: it keeps one model's blind spots from deciding the outcome.

## Claims

Set `extracts_claims: true` on the judge whose prompt asks for claim
extraction. Its claims and evidence build the claim graph, and unsupported
claims become findings the gate can act on. A claim with no evidence attached
is never recorded as `verified`.

## Discovery

Profiles are found, in order, from:

1. `profile_paths:` in `veritas.yaml` (or `--profile-path`)
2. `<project>/profiles/`
3. `$VERITAS_PROFILE_PATH` (os.pathsep-separated)
4. profiles bundled with the installed package
5. `veritas.profiles` entry points from installed plugin packages

A plugin package publishes a profile like this:

```toml
[project.entry-points."veritas.profiles"]
security = "veritas_security:profiles_dir"
```

where `profiles_dir` returns the path to a profile directory, or to a directory
of them.

## Checking your work

```bash
veritas profiles .                        # is it discovered, and are its judges listed?
veritas evaluate <artifact> --profile <name>
```

An invalid profile is reported by name, and a judge whose prompt file is
missing names the file it expected.
