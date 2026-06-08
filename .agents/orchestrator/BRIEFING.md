# BRIEFING — 2026-06-08T14:13:00Z

## Mission
Orchestrate the implementation of the Regime-Switching Long/Short Perp Bot (live execution engine) on Binance.

## 🔒 My Identity
- Archetype: teamwork_orchestrator
- Roles: orchestrator, user_liaison, human_reporter, successor
- Working directory: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/orchestrator
- Original parent: top-level
- Original parent conversation ID: 0b7c5f4d-c03f-435b-bf85-9f55cbb4d641

## 🔒 My Workflow
- **Pattern**: Project
- **Scope document**: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/orchestrator/plan.md
1. **Decompose**: Decompose the implementation into structured milestones covering database/models, risk limits, live daemon execution, tests, CLI/UI, and systemd integration.
2. **Dispatch & Execute**:
   - **Direct (iteration loop)**: Spawn Explorer for initial codebase investigation, then Workers to implement components, then Reviewers to verify, and Forensic Auditor to perform integrity checks.
3. **On failure** (in this order):
   - Retry: nudge stuck agent or re-send task
   - Replace: spawn fresh agent with partial progress
   - Skip: proceed without (only if non-critical)
   - Redistribute: split stuck agent's remaining work
   - Redesign: re-partition decomposition
   - Escalate: report to parent (last resort)
4. **Succession**: Self-succeed at 16 spawns, write handoff.md, spawn successor.
- **Work items**:
  1. Codebase Exploration [done]
  2. Implement Risk Guards (perp_guards.py) & Unit Tests [done]
  3. Implement Live Execution Daemon (regime_live.py) & FakeExchange Tests [done]
  4. Extend CLI Subcommands (scripts/tradbot.py) & UI Integration [done]
  5. Package Systemd Service & Integration [done]
  6. E2E Validation & Adversarial Testing [done]
- **Current phase**: 4
- **Current focus**: Complete Project Handoff

## 🔒 Key Constraints
- NEVER write, modify, or create source code files directly.
- NEVER run build/test commands yourself — require workers to do so.
- You MAY use file-editing tools ONLY for metadata/state files (.md) in your .agents/ folder.
- No overrides: Rule 1 (Decoy) and Rule 2 (No overrides) apply strictly.
- Never reuse a subagent after it has delivered its handoff.

## Current Parent
- Conversation ID: 0b7c5f4d-c03f-435b-bf85-9f55cbb4d641
- Updated: not yet

## Key Decisions Made
- Chose Project Pattern with E2E Testing and Implementation dual tracks.

## Team Roster
| Agent | Type | Work Item | Status | Conv ID |
|-------|------|-----------|--------|---------|
| explorer_exploration | teamwork_preview_explorer | Codebase Exploration | completed | c1539d4b-4ba5-4222-a4b2-cd294858f44f |
| worker_perp_guards | teamwork_preview_worker | Implement Risk Guards (perp_guards.py) & Unit Tests | completed | 427f86de-9531-4f84-9756-d7dc67d541e4 |
| worker_regime_live | teamwork_preview_worker | Implement Live Execution Daemon (regime_live.py) & FakeExchange Tests | completed | a37c752f-440f-4940-952e-708718b054f3 |
| worker_cli_subcommands | teamwork_preview_worker | Extend CLI Subcommands (scripts/tradbot.py) & UI Integration | completed | c4a8ef4a-f6aa-4654-93b5-54e092c23d50 |
| worker_deployment | teamwork_preview_worker | Package Systemd Service & Integration | completed | 00b7ad2a-cf8b-4527-a7e7-31f35e3d00a1 |
| auditor | teamwork_preview_auditor | Forensic Audit | completed | 9b528b4c-56f2-4ced-a159-6089dcaa1e96 |

## Succession Status
- Succession required: no
- Spawn count: 6 / 16
- Pending subagents: none
- Predecessor: none
- Successor: not yet spawned

## Active Timers
- Heartbeat cron: stopped
- Safety timer: none
- On succession: kill all timers before spawning successor
- On context truncation: run manage_task(Action="list") — re-create if missing

## Artifact Index
- /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/orchestrator/plan.md — Project plan and milestones
- /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/orchestrator/progress.md — Progress tracker and heartbeat
