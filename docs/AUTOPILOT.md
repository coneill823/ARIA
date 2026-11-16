# Autopilot — autonomous, recursive project execution

Tell A.R.I.A. **`work on <slug>`** (chat), press **▶ Start autopilot** on
the Projects page, or run `aria autopilot <slug> start` — then walk away.

## The loop (plan → build → verify → review → repeat)

1. **Plan.** The planner reads the project's requirements, the live agent
   roster, and the installed skills index, and produces a task plan: each
   task gets an **agent**, optional **skills**, a **model** (tier or a
   direct spec like `deepseek/r1` — autopilot picks the brains it deems
   necessary), a phase, an estimate, dependencies, and an observable
   `done_when`.
2. **Execute.** Tasks run one at a time in dependency order on their
   assigned agent+model. Agents create real project files by emitting
   fenced blocks tagged `file:relative/path` (writes are confined to the
   project folder).
3. **Verify.** A verification judge checks every output against the
   task's `done_when` before it counts as done; one retry with the
   critique attached, otherwise the task is parked for the next replan.
4. **Advance.** The next task starts automatically — no human input.
5. **Iterate.** When the list empties, the planner reviews the changelog
   and emits the next iteration of tasks — recursively, indefinitely —
   until it genuinely judges the goal met, or you stop it.

## Stopping and the paper trail

`stop working on <slug>` (chat), **■ Stop** (Projects page), or
`aria autopilot <slug> stop`: the task in flight finishes, then a
**final report** is written and the project is left as is. Everything is
on disk under `<project>/autopilot/`:

- `plan.json` — the live task plan (feeds the Kanban and Gantt)
- `task-<id>.md` — every task's full output
- `CHANGELOG.md` — every change with files touched, reasoning, references
- `FINAL-REPORT.md` — the end-of-run summary of all changes and why

## Watching it

The **Projects** page shows the Kanban board (To Do / Planning /
In Progress / Reviewing / Done), the **Gantt** chart built from
dependencies and estimates, the changelog, and the report — refreshing
live. Chat knows the state too ("what are you working on?"), and a
`mission-done` webhook (if configured) fires when a run ends.

## Where it runs

Inside the always-on server (`aria.service`), beside the mission worker —
closing every tab changes nothing. A restart recovers stranded tasks and
restarts them. Autopilot never blocks triage, chat, or missions; it
shares only the hardware.

## Guardrails for long unattended runs

Built for "leave it running for days", each tunable in `system/config.yml`:

- **Rolling memory.** At every iteration boundary the loop distils the
  changelog into `autopilot/lessons.md`; the planner and every task read
  it, so settled questions stay settled across iterations.
- **Duplicate guard.** Replanned task titles are checked against
  everything already done or seen; repeats are dropped (and logged).
- **Stall detection.** `autopilot_stall_limit` (default 3) consecutive
  replans that add no new work auto-pause the run as **stalled** and
  fire the `mission-done` webhook. Press Start to resume.
- **Budget caps.** `autopilot_max_tasks` / `autopilot_max_hours`
  (0 = unlimited) pause the run as **budget** at the cap instead of
  burning a week of GPU unnoticed.
- **Human-gate tasks.** The planner can mark a task `needs_human` (pick
  the product name, approve spending). The loop works around it, and when
  it is all that is left, waits and tells you; approve it with one click
  in the task's card on the Projects page.
- **Git per task.** With `autopilot_git: true`, every completed task is
  committed — *only* in projects that are their own git repo (`git init`
  in the project folder, or promoted projects). Pipeline folders and the
  A.R.I.A. repo itself are never auto-committed. This gives diffable
  history and true rollback, and makes the final report verifiable.

## Honest limits

- Agents produce text and files; they cannot execute code or browse. For
  build-and-run execution, pair autopilot with the OpenHands handoff
  (`aria handoff <slug>`) or the generated CrewAI crew.
- Quality tracks the models you assign. Give drafting to fast models and
  judgment to big ones; the verifier is only as sharp as your smart tier.
  The lessons keeper runs on the smart tier too — weak smart model, weak
  memory.
- The stall and budget guards bound busy non-progress, but they can't
  make a weak model produce good work — skim the changelog on long runs.
