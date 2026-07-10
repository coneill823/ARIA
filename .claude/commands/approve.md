---
description: Convert an accepted proposed plan into an approved requirements document
argument-hint: <slug of a project in 02-potential-projects>
allowed-tools: Read, Write, Edit, Glob, Grep
---

The user has approved the proposed plan for project `$ARGUMENTS`. Turn it
into a full requirements document.

Preconditions — stop and explain if any fail:
- `pipeline/02-potential-projects/$ARGUMENTS/proposed-plan.md` exists.
- This command was invoked (or delegated from `/review`) by the user; never
  run it on your own initiative.

Steps:

1. Read `idea.md`, `research.md`, `proposed-plan.md`, and the plan's
   `## Review log` — the review decisions are binding; the requirements doc
   must reflect the plan *as revised*, not as first proposed.
2. Write `requirements.md` in the same folder from
   `system/templates/requirements.md`. It must be complete enough that a PM
   who has read nothing else can execute the project: goals, non-goals,
   deliverables, milestones with acceptance criteria, the recommended
   subagent team, risks, and how success is measured.
3. Frontmatter: `status: approved`, `approved: true`, `approved_on: <date>`.
   Also flip `proposed-plan.md` frontmatter to `status: approved`.
4. Update `pipeline/LEDGER.md`.

Finish by summarizing the requirements in a few lines and telling the user
the project is ready for `/promote-project $ARGUMENTS`.
