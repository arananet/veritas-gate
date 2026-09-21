# Changelog

All notable changes to `veritas-gate` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Historical dates below are Git author dates, not release dates. Uncommitted
changes remain undated under `Unreleased`.

<!--
Guidelines:
- Add a new entry under `## [Unreleased]` as you work — no batching up for release day.
- Group entries under: Added, Changed, Deprecated, Removed, Fixed, Security.
- Reference the spec slug and PR number:  "Added dark mode (spec: dark-mode, #42)".
- On release, rename `[Unreleased]` to the new version with the release date,
  and open a fresh `[Unreleased]` section at the top.
- The release-drafter workflow auto-populates draft release notes from PRs —
  keep PR titles tidy so they flow straight into here.
-->

## [Unreleased]

### Added

- Bounded evaluation → repair → re-evaluation loop (spec: bounded-repair-loop).
  - `veritas loop` (autopilot), `veritas repair-plan` (assist), `veritas loop-report`,
    `veritas diff`, plus `--dry-run`, `--resume` and `--max-iterations`.
  - `LoopOrchestrator` as an explicit state machine; every run terminates with one of
    twelve `StopReason` values and no configuration can make it unbounded.
  - `RepairPlanner` converts findings into a normalized repair contract; a judge never
    speaks to a repair agent, and the evaluator never sees the agent's reasoning or its
    claim that something was fixed.
  - `RepairAgent` protocol with `MockRepairAgent` and `GenericCLIRepairAgent`, the latter
    driving any CLI coding agent through a command template.
  - `FindingLedger` gives each logical issue a stable identity across iterations;
    `compare_evaluations` measures progress by blocking findings and issue resolution,
    never by an aggregate score.
  - Workspaces (git worktree, snapshot, copy or in place) so originals stay untouched;
    git is used when available and never required.
  - Safety invariant, enforced in code rather than prompt wording: Veritas may improve how
    existing evidence is represented, implemented, documented or validated, and will not
    fabricate missing evidence to satisfy its own evaluator.
  - Every iteration preserved under `.veritas/loops/<loop-id>/`, with patches and a final
    `loop-report.md` and `loop-result.json`.
- Veritas Gate v0.1: the core evaluation framework (spec: veritas-core-evaluation-engine).
  - Domain models: `Artifact`, `Finding`, `JudgeResult`, `CheckResult`, `Claim`, `EvaluationResult`.
  - Profiles as plugins: `scientific-paper` and `generic-document`, discovered from config,
    the project tree, `VERITAS_PROFILE_PATH`, the package, and `veritas.profiles` entry points.
  - Provider adapters for Anthropic, OpenAI, OpenAI-compatible endpoints and Google, with
    structured output, retries, timeouts and token-usage metadata.
  - Blind independent LLM judges, deterministic allow-listed checks, a claim graph with
    evidence coverage, a MetaJudge that preserves critical findings, and a deterministic gate.
  - Immutable runs under `.veritas/runs/`, Markdown and JSON reports, and an advisory
    `repair-plan.json`.
  - The `veritas` CLI (`init`, `evaluate`, `report`, `findings`, `claims`, `gate`,
    `repair-plan`, `profiles`) with CI-meaningful exit codes.
  - Prompt-injection containment: artifact content is wrapped as untrusted data, finding ids
    are assigned by Veritas, and nothing executes unless listed in `execution.allow`.
- Per-model `tls_verify`, for networks that intercept TLS: `truststore` uses the operating
  system trust store (new `[tls]` extra), a path uses an explicit CA bundle, and `false`
  disables verification for a local self-signed endpoint — printing a warning that names
  the endpoint and what it exposes (spec: veritas-core-evaluation-engine).
- Shared local/CI Markdown lint runner with locked dependencies, `make setup-lint`, `make lint-markdown`, and `make verify-template` (spec: lean-agent-workflow).
- Persistent verification evidence, stale-state detection, pause/resume and optional bounded local agent adapters (spec: reliable-verification-and-resumable-execution).

### Fixed

- **The gate could report `PASS` for an artifact it never evaluated.** When every judge
  failed — unreachable provider, missing or invalid credentials — the meta review
  discarded their error results, the gate saw zero findings and approved the artifact
  with exit code 0. Judge errors are now retained as findings, the gate refuses to pass
  while any judge failed to complete (`gate.fail_on_judge_error`, on by default), and the
  console names each failed judge and its reason (spec: veritas-core-evaluation-engine).
- Profile discovery found nothing for an artifact whose config lives in a subdirectory
  (`veritas evaluate examples/paper`) under an editable install, because the packaged
  profile copy only exists in a built wheel. A source-checkout fallback now resolves the
  repository's own `profiles/` (spec: veritas-core-evaluation-engine).
- Artifact and workspace file discovery matched the skip list against absolute path parts,
  so an artifact rooted under a skipped directory name (such as a repair workspace under
  `.veritas/workspaces/`) appeared empty to judges and checks (spec: bounded-repair-loop).

### Changed

- README starts with one minimal adoption path; agent check and auto-fix instructions follow the shared CLI and readiness contract (spec: lean-agent-workflow).
- Markdown lint uses CLI 0.23.2 with a patched TOML parser; table spacing follows its new default rule without changing security policies (spec: lean-agent-workflow).
- CLI, hooks and deterministic CI share a Ruby standard-library YAML engine; Ruby >= 2.6 is now required. `make test name=<slug>` verifies the selected spec.
- Downstream checks reject missing/unconfigured config; template maintenance uses an explicit marker removed during adoption.

### Fixed

- Markdown formatting errors in agent instructions and project documentation, preserving lint rules and policy content (spec: enterprise-hardening).
- Markdown lint CI now passes a checked-in configuration file to the action instead of inline JSON (spec: enterprise-hardening).
- Quoted/commented YAML parsing, ready-spec content checks, staged-spec validation, script/infrastructure coverage and enforcement of declared deterministic policy flags.

## 2026-05-13

Commits: `0a92a2d`, `035a861`, `5adbfbe`.

### Added

- Roles section in spec templates (`implementer`, `reviewer`, `qa`, `product_owner`) for per-spec responsibility assignment
- `roles.default_*` block in `.openspec/config.yaml` and `.openspec/defaults.yaml` for repo-wide default role assignments
- `scripts/openspec scaffold` now reads `roles.default_*` from config and pre-fills new specs
- Onboarding interview (`.openspec/onboarding.yaml`) prompts for default implementer / reviewer / qa / product_owner
- `Makefile` with convenience targets: `check`, `scaffold`, `scaffold-bug`, `test`, `status`, `setup`, `cleanup-template-specs`, `apply-branch-protection`
- `scripts/cleanup-template-specs` removes the template's internal design specs from a fresh fork
- `.vscode/settings.json` and `.vscode/extensions.json` with YAML schemas, markdownlint config, and recommended extensions
- `renovate.json.example` as an opt-in alternative to `dependabot.yml`
- Spec lifecycle state diagram in [`docs/OPENSPEC.md`](docs/OPENSPEC.md)
- **Enterprise hardening:**
  - `.github/workflows/license-scan.yml` + `.licenses/policy.yaml` — ScanCode-based OSS license enforcement
  - `.github/workflows/container-scan.yml` — Hadolint + Trivy scanning, auto-skips when no Dockerfile present
  - `.github/workflows/spec-metrics.yml` — weekly DORA-style report on spec status, role coverage, PR→spec link rate
  - `docs/branch-protection-ruleset.json` + `scripts/apply-branch-protection` — one-command branch protection bootstrap
  - `SECRETS.md` — secrets-management policy with rotation cadences and incident response
  - PR template extended with accessibility, privacy / data-handling, and security checklists
- `CONTRIBUTING.md` documents the `roles` block in the spec workflow
- `CLAUDE.md` Step 5 now instructs Claude to walk users through `roles` during scaffolding
- `CLAUDE.md` Step 6 now instructs Claude to clean up template-internal specs

## 2026-04-15

### Added

- Initial repository commit (`d07225a`). No tagged release date is recorded in the local Git history.

[Unreleased]: https://github.com/arananet/veritas-gate/commits/HEAD
