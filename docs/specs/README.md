# Feature Specifications

Implementation specifications for features that are developed with (or handed to)
coding agents. A spec is the single source of truth for a feature: agents implement
*against the spec*, reviewers review *against the spec*.

## Conventions

* **Path & naming:** `docs/specs/NNN-short-kebab-name.md`, `NNN` is a zero-padded
  sequence number. Never reuse a number, never rename after merge (agents and PRs
  link to it). All project documentation lives under `docs/`.
* **Status header:** every spec starts with a table containing Status
  (`Draft` → `Approved` → `Implemented` → `Superseded by NNN`), author, date,
  target version, affected components. Agents must not start implementation
  phases of a `Draft` spec unless explicitly instructed.
* **Required sections:** Summary (with normative invariants if safety-relevant),
  Current State Analysis (with file/line references), Design, Failure Mode
  Analysis, Test Plan, Implementation Plan (phased, with file lists and
  dependencies), Acceptance Criteria, Open Questions.
* **Phased implementation plan:** phases must be independently
  reviewable/mergeable and name their files, so file-disjoint phases can be
  dispatched to parallel git-worktree agents without merge conflicts. When a
  phase is inherently a single file (e.g. the core of a refactor), say so and
  mark it as a serial dependency for the phases that follow.
* **Toolchain gate:** every phase must leave `./lint.sh` green — `ruff check`,
  `ruff format --check`, and `pyright` (strict). CI runs these; a phase that
  cannot pass them is not mergeable. Wrap user-facing strings with the `i18n.py`
  helpers; never translate log messages.
* **Tests:** behaviour-changing specs include a Test Plan. New or refactored
  lifecycle / state-machine / concurrency code ships **with** its unit tests in
  the same phase (a state machine without tests defeats the point), and the CI
  test job is wired up as part of the same spec. Older areas of the tree are not
  retro-actively covered by this rule — it applies to code a spec touches.
* **Agent workflow:** one agent (or agent group) per phase; each PR description
  references the spec (`docs/specs/NNN`) and the phase number, and states which
  spec invariants/criteria it implements. Deviations from the spec discovered
  during implementation are fed back as spec edits in the same PR, not silently
  coded around.
* **After completion:** flip Status to `Implemented`, note the release version.
  Specs are history — do not delete them.

## Index

| Spec | Title | Status |
|---|---|---|
| [001](001-connection-lifecycle-state-machine.md) | Connection lifecycle state machine | Draft |
