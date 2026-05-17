# Implementation Plan: Test Agent Team Trigger

Status: draft
Execution state: not started

## Goal

Verify that the `team` skill spawns real Claude Code teammates as tmux split panes from a valid `.plans/` file.

## Requirements

- [ ] The `team` skill must accept this plan as valid.
- [ ] The lead must run the tmux runtime preflight before spawning agents.
- [ ] The lead must spawn exactly 2 real Claude Code teammates.
- [ ] Each teammate must appear as its own tmux split pane.
- [ ] Each teammate must make a small, isolated file change inside `.agent-team-trigger-test/`.
- [ ] The lead must run build-only validation after both agents report done.
- [ ] No application code may be modified.

## Assumptions

- [ ] Claude Code is already running inside tmux.
- [ ] `tmux show-options -g focus-events` returns `focus-events on`.
- [ ] The repository allows creating a temporary test directory named `.agent-team-trigger-test/`.
- [ ] Shell commands such as `mkdir`, `test`, and `cat` are available.

## Agent Team Readiness

- [ ] This plan can be executed by an agent team: yes
- [ ] Recommended team size: 2
- [ ] Parallelization strategy: two agents create separate files in the same isolated test directory without touching each other's files
- [ ] Integration contracts are explicit enough for parallel work
- [ ] File ownership boundaries are defined to prevent conflicts
- [ ] Validation can be assigned per agent

## Suggested Agent Structure

### Agent: alpha

- [ ] Owns:
  - `.agent-team-trigger-test/alpha.md` — Alpha agent owns this file exclusively.
- [ ] Does NOT touch:
  - `.agent-team-trigger-test/beta.md` — Owned by beta agent.
  - Application source files — Not part of this trigger test.
- [ ] Responsibilities:
  - Create `.agent-team-trigger-test/alpha.md`.
  - Write a short confirmation that alpha ran successfully.
- [ ] Produces:
  - Alpha trigger output file.
- [ ] Consumes:
  - Directory contract for `.agent-team-trigger-test/`.
- [ ] Validation:
  - `test -f .agent-team-trigger-test/alpha.md` — file exists.
  - `grep -q "alpha agent completed" .agent-team-trigger-test/alpha.md` — expected marker exists.

### Agent: beta

- [ ] Owns:
  - `.agent-team-trigger-test/beta.md` — Beta agent owns this file exclusively.
- [ ] Does NOT touch:
  - `.agent-team-trigger-test/alpha.md` — Owned by alpha agent.
  - Application source files — Not part of this trigger test.
- [ ] Responsibilities:
  - Create `.agent-team-trigger-test/beta.md`.
  - Write a short confirmation that beta ran successfully.
- [ ] Produces:
  - Beta trigger output file.
- [ ] Consumes:
  - Directory contract for `.agent-team-trigger-test/`.
- [ ] Validation:
  - `test -f .agent-team-trigger-test/beta.md` — file exists.
  - `grep -q "beta agent completed" .agent-team-trigger-test/beta.md` — expected marker exists.

## Affected Files

- [ ] `.agent-team-trigger-test/alpha.md` — Created by alpha agent to prove alpha executed.
- [ ] `.agent-team-trigger-test/beta.md` — Created by beta agent to prove beta executed.
- [ ] `.agent-team-trigger-test/` — Temporary directory used only for this trigger test.

## Dependencies

- [ ] `tmux` — Required for visible split-pane teammate mode.
- [ ] Claude Code Agent Teams — Required to spawn real teammates.
- [ ] Shell utilities: `mkdir`, `test`, `grep`, `cat`.

## Integration Contracts

### Contract: alpha → lead

- [ ] Producer: alpha agent
- [ ] Consumer: lead
- [ ] Files involved:
  - `.agent-team-trigger-test/alpha.md`
- [ ] Interface type: file output
- [ ] Contract details:

```text
alpha agent completed
```

- [ ] Success cases:
  - `.agent-team-trigger-test/alpha.md` exists.
  - The file contains `alpha agent completed`.
- [ ] Error cases:
  - File is missing.
  - File exists but does not contain the expected marker.
- [ ] Evidence:
  - Lead validates with `test` and `grep`.

### Contract: beta → lead

- [ ] Producer: beta agent
- [ ] Consumer: lead
- [ ] Files involved:
  - `.agent-team-trigger-test/beta.md`
- [ ] Interface type: file output
- [ ] Contract details:

```text
beta agent completed
```

- [ ] Success cases:
  - `.agent-team-trigger-test/beta.md` exists.
  - The file contains `beta agent completed`.
- [ ] Error cases:
  - File is missing.
  - File exists but does not contain the expected marker.
- [ ] Evidence:
  - Lead validates with `test` and `grep`.

## Cross-Cutting Concerns

- [ ] Concern: file ownership
  - Owner: lead
  - Applies to:
    - alpha agent
    - beta agent
  - Decision: alpha may only edit `.agent-team-trigger-test/alpha.md`; beta may only edit `.agent-team-trigger-test/beta.md`.
  - Evidence: Suggested Agent Structure defines exclusive ownership.

- [ ] Concern: no application code changes
  - Owner: lead
  - Applies to:
    - alpha agent
    - beta agent
  - Decision: agents must not edit files outside `.agent-team-trigger-test/`.
  - Evidence: Affected Files contains only trigger-test files.

- [ ] Concern: tmux split-pane verification
  - Owner: lead
  - Applies to:
    - lead runtime
    - alpha agent
    - beta agent
  - Decision: if teammates do not appear as tmux split panes, stop and report tmux diagnostics instead of continuing in in-process mode.
  - Evidence: team skill runtime preflight requires `$TMUX`, `focus-events`, and tmux pane info.

## Risks

- [ ] **Medium**: Claude Code may fall back to in-process teammate mode instead of tmux split panes.
  - Mitigation: Lead must stop if teammates do not appear as tmux split panes.
  - Owner: lead

- [ ] **Low**: Both agents may try to create the same directory at the same time.
  - Mitigation: `mkdir -p .agent-team-trigger-test` is idempotent.
  - Owner: alpha and beta

- [ ] **Low**: Temporary test files may be left behind.
  - Mitigation: Files are isolated under `.agent-team-trigger-test/` and can be deleted after the test.
  - Owner: lead

## Implementation Phases

### Phase 1: Spawn Agent Team

- [ ] Run tmux runtime preflight
  - Action: Run the tmux checks required by the `team` skill.
  - Why: Confirms Claude Code is running in a tmux-compatible session before spawning teammates.
  - Dependencies: tmux runtime.
  - Risk: Medium.
  - Agent: lead
  - Evidence: `$TMUX` is non-empty, `focus-events` is on, and tmux pane info is available.

- [ ] Spawn alpha and beta as real Claude Code teammates
  - Action: Spawn two teammates in parallel using tmux split panes.
  - Why: This is the behavior being tested.
  - Dependencies: Agent Teams runtime.
  - Risk: Medium.
  - Agent: lead
  - Evidence: Two teammate panes appear in tmux.

### Phase 2: Agent File Outputs

- [ ] Create alpha trigger file (`.agent-team-trigger-test/alpha.md`)
  - Action: Run `mkdir -p .agent-team-trigger-test` and write `alpha agent completed` to `.agent-team-trigger-test/alpha.md`.
  - Why: Proves alpha executed independently.
  - Dependencies: Directory creation.
  - Risk: Low.
  - Agent: alpha
  - Evidence: `.agent-team-trigger-test/alpha.md` exists and contains `alpha agent completed`.

- [ ] Create beta trigger file (`.agent-team-trigger-test/beta.md`)
  - Action: Run `mkdir -p .agent-team-trigger-test` and write `beta agent completed` to `.agent-team-trigger-test/beta.md`.
  - Why: Proves beta executed independently.
  - Dependencies: Directory creation.
  - Risk: Low.
  - Agent: beta
  - Evidence: `.agent-team-trigger-test/beta.md` exists and contains `beta agent completed`.

### Phase 3: Lead Verification

- [ ] Verify alpha output (`.agent-team-trigger-test/alpha.md`)
  - Action: Run `test -f .agent-team-trigger-test/alpha.md && grep -q "alpha agent completed" .agent-team-trigger-test/alpha.md`.
  - Why: Confirms alpha produced the expected output.
  - Dependencies: alpha completion.
  - Risk: Low.
  - Agent: lead
  - Evidence: Command exits successfully.

- [ ] Verify beta output (`.agent-team-trigger-test/beta.md`)
  - Action: Run `test -f .agent-team-trigger-test/beta.md && grep -q "beta agent completed" .agent-team-trigger-test/beta.md`.
  - Why: Confirms beta produced the expected output.
  - Dependencies: beta completion.
  - Risk: Low.
  - Agent: lead
  - Evidence: Command exits successfully.

## Agent Validation

### alpha

- [ ] Run `test -f .agent-team-trigger-test/alpha.md`
  - Expected result: command exits successfully.
  - Evidence: alpha file exists.

- [ ] Run `grep -q "alpha agent completed" .agent-team-trigger-test/alpha.md`
  - Expected result: command exits successfully.
  - Evidence: alpha marker exists.

### beta

- [ ] Run `test -f .agent-team-trigger-test/beta.md`
  - Expected result: command exits successfully.
  - Evidence: beta file exists.

- [ ] Run `grep -q "beta agent completed" .agent-team-trigger-test/beta.md`
  - Expected result: command exits successfully.
  - Evidence: beta marker exists.

## Build Validation

- [ ] Run the project's build command: `test -f .agent-team-trigger-test/alpha.md && test -f .agent-team-trigger-test/beta.md && grep -q "alpha agent completed" .agent-team-trigger-test/alpha.md && grep -q "beta agent completed" .agent-team-trigger-test/beta.md`
- [ ] Confirm the build completes without errors
- [ ] Do not run Playwright, browser automation, agent-browser, Cypress, or E2E validation unless explicitly requested

## Success Criteria

- [ ] The `team` skill accepts `.plans/0004-test-agent-team-trigger.md`.
- [ ] The lead runs tmux runtime preflight.
- [ ] Two real Claude Code teammates are spawned.
- [ ] Each teammate appears as its own tmux split pane.
- [ ] Alpha creates `.agent-team-trigger-test/alpha.md`.
- [ ] Beta creates `.agent-team-trigger-test/beta.md`.
- [ ] Both agent validation checks pass.
- [ ] Lead build validation passes.
- [ ] No application source files are modified.

## Final Verification

- [ ] Every checked implementation item corresponds to an actual repository change.
- [ ] No required implementation step remains unchecked.
- [ ] All file ownership boundaries were respected.
- [ ] All integration contracts were implemented as written or explicitly revised.
- [ ] The build completed without errors.
- [ ] No browser, Playwright, agent-browser, Cypress, or E2E validation was added unless explicitly requested.
- [ ] Teammates appeared as tmux split panes rather than in-process teammates.

## Open Questions

- [ ] None