---
description: Promote an approved project out of the pipeline — scaffold its PM and subagent team
argument-hint: <slug of an approved project>
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(mv:*), Bash(mkdir:*), Bash(cp:*), Bash(date:*), Bash(git init:*), Bash(chmod:*)
---

Promote project `$ARGUMENTS` from the pipeline into the real world.

**Hard gate — verify before anything else:**
`pipeline/02-potential-projects/$ARGUMENTS/requirements.md` must exist with
`approved: true` in its frontmatter. If it doesn't, refuse, explain, and
point the user at `/review` and `/approve`. No exceptions.

Steps:

1. Read `system/config.yml → promotion_target` and resolve it (relative
   paths resolve from the aria root). Create the directory if needed. If
   `<target>/$ARGUMENTS` already exists, stop and ask rather than overwrite.
2. Move the whole project folder to `<target>/$ARGUMENTS/` and reorganize:
   - `docs/idea.md`, `docs/research.md`, `docs/proposed-plan.md`,
     `docs/requirements.md` (requirements stay the source of truth)
3. Scaffold the Project Manager from `system/templates/pm-claude.md` →
   `<target>/$ARGUMENTS/CLAUDE.md`, filling every `{{PLACEHOLDER}}` from the
   requirements: project name, one-liner, deliverables, milestone summary,
   success metrics.
4. Design the subagent team. From the requirements' "Recommended team"
   section (adapt if the requirements suggest otherwise — a website project
   needs a developer and a market-researcher; a research project needs
   different specialists). Create each as
   `<target>/$ARGUMENTS/.claude/agents/<name>.md` from
   `system/templates/pm-subagent.md`, with a real role description, the
   tools it needs, and what "done" means for its work.
5. Create `<target>/$ARGUMENTS/STATUS.md` from
   `system/templates/project-status.md`: all milestones from requirements as
   unchecked items, phase set to "Not started".
6. Initialize the project as a git repository (`git init`) unless it's being
   promoted into an existing repo (a parent directory already has `.git`).
7. Update `pipeline/LEDGER.md`: `status: promoted`, with the new location.

Finish with the handover message:

- where the project now lives
- the PM team that was created (one line per subagent)
- next step: `cd <target>/$ARGUMENTS && claude`, then tell the PM
  "begin execution"
- reminder that the PM in that directory — not A.R.I.A. — is now the
  interface for this project's whole lifespan.
