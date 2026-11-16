# Autopilot — she works the project, you sleep

Say **`work on <project>`** (chat or voice), press **▶ Start** on the
Projects page, or run `aria autopilot <slug> start` — then walk away. She
keeps going, batch after batch, until you tell her to stop.

## The one thing that needs you

Autopilot will not touch a plan nobody approved. **Approve** it — the
button on the Projects page, "approve *project*" in chat, or
`aria approve <slug>` — and the proposed plan, exactly as it stands,
becomes `requirements.md`. The project then moves out of the pipeline into
its own workspace (`active-projects/<slug>/`, or wherever
`promotion_target` points) and work begins. Everything after that is hers.

## The loop

```
assess → plan → [ execute → verify → record ]* → reflect → plan → …
```

1. **Assess.** Before planning anything she takes stock and writes
   `ASSESSMENT.md`: what needs to be done, which of the agents, skills,
   models and tools on hand cover which part, what they *cannot* do, and
   the order of attack.
2. **Plan.** A batch of 3–8 tasks. Each names its **agent** (the standing
   team, or a library specialist that fits this project), the **skills**
   it should load, the **model** it needs (a tier, or a specific installed
   model — fast for simple drafting, deep for the hardest reasoning), the
   files it must read, what it depends on, *why* it exists, and an
   observable `done_when`. A task that turns on something only you can
   decide is marked as **yours** — see [When a decision is
   yours](#when-a-decision-is-yours).
3. **Execute.** One task at a time, in dependency order. The agent sees
   the goal, the project memory, the files it asked for and its
   dependencies' output, and answers with whole files in `file:` blocks.
   Tasks marked *research* get live web material with sources attached.
4. **Verify.** First mechanically — does the Python, JSON, shell or
   JavaScript even parse? — then a judge reads the work against the task's
   own `done_when`. A rejected attempt is **rolled back** and retried with
   the critique; after three attempts the task is marked failed and the
   planner routes around it. So every change left in the project passed
   verification.
5. **Record.** A structured entry in `changes.jsonl`, a readable one in
   `CHANGELOG.md`, and — when the project is its own git repository — a
   commit on the `aria/autopilot` branch.
6. **Reflect.** When a batch has nothing left to run, `MEMORY.md` is
   rewritten: the state of the project, decisions made, lessons, what is
   still to do. The next plan starts from that, not from a transcript that
   grows without end.
7. **Repeat.** When the planner says the goal is met, an **audit** checks
   that claim against the requirements; any gaps become the next batch.
   With the goal met she moves on to improvements and keeps going until
   you stop her (set `autopilot_after_goal: stop` to have her write the
   report and stand down instead).

## Watching it

The **Projects** page:

- **Board** — To Do · Planning · In Progress · Reviewing · Done. Each card
  shows its agent, model and skills.
- **Gantt** — the plan as a timeline: estimates, dependencies as arrows,
  one band per iteration.
- **Live** — what she is doing right now, streamed as she thinks, beside
  the activity log.
- **Brief** — her assessment, her memory, your guidance, and the source
  documents.
- **Changes** — every completed task: the files it touched, why it was
  done, how it was verified, and what it drew on.
- **Report** — the final report of the last run.

The sidebar shows what she is working on from every page, and you can ask
from anywhere: "what are you working on", "status of *project*", "what
changed in *project*".

## Steering without stopping

**Steer** (Projects page), "tell *project* to …" in chat, or
`aria autopilot <slug> steer "…"`. Guidance is binding on every plan from
then on; work that has not started is replanned around it, and nothing
that is running is interrupted.

## When a decision is yours

Some things are not hers to decide: spending money, a choice that cannot
be undone, an account only you hold. The planner marks such a task
**needs you**. It stays in To Do — amber, dashed — and so does anything
that builds on it. Everything else carries on, batch after batch. Only
when nothing else is left does she stop and wait, and waiting costs
nothing: no model is called until you answer.

![A task waiting on your decision](ui-gate.png)

Open the task and answer it:

- **Approve** — add your decision if the task needs one ("use the free
  tier", "call it Larder") and the task is told to follow it exactly.
- **Skip it** — it is not attempted, what built on it is dropped, and she
  plans around it.

The same from chat — "approve task t5 in *project*: use the free tier",
"skip task t5 in *project*" — or from a terminal:

```bash
aria autopilot <slug> approve-task t5 "use the free tier"
```

```bash
aria autopilot <slug> skip-task t5 "no budget for this"
```

Your answer is recorded with the change it led to, in the changelog and
in the final report. A voice assistant cannot give it for you. If you
named a `mission-done` webhook she calls it when a task starts waiting
on you and again when she has run out of other work.

## Stopping, and the report

"stop working on *project*", **■ Stop**, or `aria autopilot <slug> stop`:
the step in flight finishes, the final report is written, and the project
is left exactly as it is.

`FINAL-REPORT.md` is built from the structured record, not from a model's
recollection, so it is complete however long the run was. For every
change: the files (created or modified, lines added and removed), why the
task existed, what was done, how it was verified, and its references —
the task's full output, the commit, and any web sources. Failed tasks are
listed with the reason; nothing from them is left in the project. Tasks
still waiting on your decision are listed too, with what waits behind
them. Earlier runs' reports are kept in `autopilot/reports/`.

![The final report](ui-report.png)

Starting again later is a new run that carries on from the same plan and
memory.

## When things go wrong

| What happens | What she does |
|---|---|
| The model server is down or drops the connection | Waits and retries with a growing pause (30 s up to 15 min). The task is not counted as failed and nothing is lost. |
| A model stays silent | Fast and smart tiers are abandoned after `autopilot_idle_minutes` and retried. The deep tier and hand-picked models are never cut off. |
| Work is rejected | Rolled back, retried with the reviewer's critique; failed after three attempts. Tasks that depended on it are skipped and the planner is told why. |
| `autopilot_stall_limit` batches in a row (3 unless you change it) complete nothing, or the planner keeps returning nothing | She pauses herself ("stalled"), writes the report and says why. |
| The planner proposes work that is already done, that you declined, or that has failed twice | It is left out of the plan, and the log says so. |
| The server restarts mid-task | The task is picked up again on the next step. |
| A budget is reached (`autopilot_max_hours`, `autopilot_max_tasks`, `autopilot_max_iterations`) | Finishes the step in flight, writes the report and pauses as "budget reached". Resuming is a new run with a fresh budget. |
| A decision is yours to make | The task waits; the rest carries on. See [above](#when-a-decision-is-yours). |

However a run ends — stopped, complete, stalled, budget reached — she
writes the report and calls the `mission-done` webhook if you named one.

## Git and GitHub

When a project is its own repository — it is when `promotion_target` is
outside the A.R.I.A. repo, or after `aria github init <slug>` — every
verified task is one commit on `aria/autopilot`. Your base branch is never
committed to, and only the files a task wrote are staged: your own
uncommitted edits in the same tree are left alone.

With `github_access: true`, `aria github publish <slug>` creates a private
repository, and at the end of each run she pushes the branch and opens (or
updates) a pull request. See [INTEGRATIONS.md](INTEGRATIONS.md).

## A codebase she did not create

```bash
aria adopt https://github.com/you/your-repo "Add a command line entry point and document it."
```

```bash
aria autopilot your-repo start
```

`adopt` takes a git URL, a GitHub `owner/repo`, or a folder on this
machine. The goal you state is the contract — stating it is the approval.
Her records go in a hidden `.aria/` folder that git is told to ignore, so
the repository only ever receives the verified work.

## What is on disk

Under `<project>/autopilot/` (`.aria/` for an adopted codebase):

| File | What it is |
|---|---|
| `ASSESSMENT.md` | the stock-take written before the first plan |
| `plan.json` | the live task plan — the Board and the Gantt are drawn from it |
| `MEMORY.md` | the working memory, rewritten after every batch (a project first run under the earlier autopilot has `lessons.md`; it is folded in at the first rewrite) |
| `STEERING.md` | your guidance |
| `STOP`, `STEER`, `decision-<id>.json` | your signals — stop, new guidance, an answer on a task that waits for you — picked up before her next step and then removed |
| `changes.jsonl` | the complete, structured change record |
| `CHANGELOG.md` | the same, readable |
| `task-<id>.md` | every attempt at every task, with its verdict |
| `FINAL-REPORT.md`, `reports/` | the latest report, and every earlier one |
| `state.json`, `activity.jsonl` | where she is, and the Live tab's feed |

## Where it runs

Inside the always-on server (`aria.service`), beside the mission worker —
closing every tab changes nothing. Without the server,
`aria autopilot <slug> run` works the project in a terminal. Autopilot
never blocks triage, chat or missions; it shares only the hardware.

## Tuning

- **Models decide the quality.** The planner, the verifier and the audit
  run on the smart tier; a sharper model there helps more than anywhere
  else. Give drafting to fast models and judgment to big ones.
- **`context_tokens`** (default 8192) is requested from Ollama for every
  call and prompts are sized to it. Ollama's own default silently cuts
  long prompts — raise this if your model and VRAM allow.
- **Budgets and the stall limit** (Settings → Autopilot) bound a run you
  leave for days: hours, batches, tasks, and how many batches may
  complete nothing before she pauses herself. All off or lenient by
  default.
- **Skills and specialists** come from `aria install-skills`. For each
  project the planner is offered only the ones that fit its goal.

## Honest limits

- She writes files and checks that code parses. She **cannot run or test
  code** — the report says so, and the tests she writes are yours to run.
  For build-and-run execution, pair autopilot with the OpenHands handoff
  (`aria handoff <slug>`) or the generated CrewAI crew.
- Files are rewritten whole, so very large files are expensive and risky
  to edit; small modules work best.
- The verifier is a model. It is only as sharp as your smart tier, and it
  can reject good work or accept weak work. Skim the Changes tab on long
  runs.
- Web research reads search results and a couple of pages per task. It
  cites what it used; it is not a deep-research system.
- What counts as *yours to decide* is the planner's judgment. A weak
  model may ask too often or not at all — steer it: "decide naming
  yourself", "ask me before anything that costs money".
