---
description: Pipeline dashboard — what's where, what's stalled, what needs you
argument-hint: (no arguments)
allowed-tools: Read, Glob, Grep, Bash(date:*), Bash(ls:*)
---

Render the A.R.I.A. pipeline dashboard.

1. Scan every stage directory and read frontmatter:
   - `pipeline/00-inbox/` — split into fresh captures vs `type: unclear`
     items waiting on the user's answers
   - `pipeline/01-processing/` — awaiting `/develop`
   - `pipeline/02-potential-projects/*/` — split `status: proposed`
     (awaiting review) vs `status: approved` (ready to promote)
   - active projects in the `promotion_target` from `system/config.yml` —
     read each project's `STATUS.md` for phase and progress
2. Cross-check against `pipeline/LEDGER.md` and fix any drift you find
   (missing rows, stale states).

Output, in order:

- **Needs you** — unclear items with their questions, and proposed plans
  awaiting review. This is the actionable section; lead with it.
- **In flight** — counts per stage, each item with its age (days since
  `captured`). Flag anything older than 14 days as stalled.
- **Active projects** — one line each: phase, progress, last update.
- **Suggested next command** — the single most useful thing to run now.

Keep it compact — this is a glanceable dashboard, not a report.
