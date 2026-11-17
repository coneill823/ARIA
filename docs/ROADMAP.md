# What helps an agent that runs on its own for days

A loop that plans, works and replans without anyone watching fails in a
few predictable ways: it forgets, it spins, it drifts from the goal, it
breaks things it cannot see, and nobody notices until morning. This is the
list of what guards against that — first what A.R.I.A. already does, then
what would help most next, in rough order of value for effort.

## In place

| Failure it prevents | What she does |
|---|---|
| **Forgetting** — the context fills up and early decisions vanish | Working memory (`MEMORY.md`) rewritten after every batch; prompts budgeted to the context window; the window requested from the model server explicitly rather than left to a silent default |
| **Re-doing finished work** | The planner sees memory, the file list and recent results; a task that repeats one already done, one you declined, or one that has failed twice is dropped — also long after the original has left the board |
| **Spinning on a failing task** | Three attempts, then the task is failed and the planner routes around it |
| **Hammering a dead model server** | Outages are waited out with a growing pause and cost the task nothing |
| **Busy non-progress** | `autopilot_stall_limit` batches in a row that complete nothing, or a planner that keeps returning nothing, and she pauses herself and says why |
| **Leaving broken work behind** | Code is parsed before a judge sees it; the project's own tests are run on every code change, in a sandbox, and a change that breaks them is rejected; rejected attempts are rolled back, so the tree only ever holds verified work |
| **Trusting code a model wrote** | Its tests run with no network, a read-only machine and a scratch copy as the only writable place; a program is only ever started by the owner |
| **Code that works but rots** | When the goal is met — and every few changes after — the whole codebase is reviewed and refined under a "behaviour must not change" rule, checked by the tests |
| **Grading its own homework** | "Done" is audited against the requirements; gaps become the next batch |
| **Working blind** | Tasks are shown the project files they need; the assessment reads the key files of an existing codebase |
| **Recalled "facts"** | Research tasks get live web material with sources, carried into the report |
| **No way back** | A commit per verified task on its own branch |
| **Running forever** | Optional budgets in hours, batches and tasks; at one she writes the report and pauses |
| **Deciding what is yours to decide** | A task the planner marks `needs_human` waits for your answer — approve it with your decision, or skip it — while everything that does not depend on it carries on |
| **Nobody noticing** | A desktop notification and the `mission-done` webhook when a run ends, for whatever reason, and when a task starts waiting on you |
| **The wrong model for the job** | Giants get a role each and are loaded on demand, one at a time; the planner is told what each costs; work is grouped by the model already loaded |
| **Losing the thread of a long run** | The Gantt, the board and the reports keep everything the project ever did; times are measured, and predictions come from them |
| **Unreadable history** | A structured change record, and a final report built from it — complete however long the run |
| **Being unsteerable** | Guidance mid-run replans unstarted work without stopping anything |
| **A hung generation** | Fast and smart tiers are abandoned after a silent spell and retried |

## Next, in order of value for effort

1. **Install what a project needs, in the sandbox.** Tests run today,
   but only with what is already on the machine: a project that needs a
   package nobody installed cannot be tested. A per-project environment
   built inside the sandbox from a pinned, allowlisted index — and smoke
   runs of the program itself, not only its tests — is the next step up
   from "passes its tests" to "starts and works".
2. **A second opinion that is not the author.** Verify with a different
   model family than the one that did the work; for the audit, two judges
   that must agree. Correlated blind spots are the main reason a
   self-reviewing loop approves its own mistakes.
3. **Have the critic attack the plan before it is executed.** One extra
   call per batch that catches missing steps and bad task boundaries while
   they are still cheap to fix.
4. **Edits as patches, not whole files.** Rewriting a 600-line file to
   change four lines wastes the context and invites regressions. A
   search-and-replace block format, applied and verified mechanically,
   makes large files safe to touch.
5. **Let agents look things up.** A small read-only tool loop — list,
   read, search within the project — so an agent can fetch the file it
   realises it needs instead of relying on what the planner packed.
6. **A morning digest, and a nudge on your phone.** A desktop
   notification and the webhook already fire when a run ends or a task
   needs you. Next: a daily summary of what happened overnight, a push
   when she has waited an hour on the model server, and the same through
   the Echo.
7. **Trace every task to a requirement.** Each task names the requirement
   it serves; the audit then reports coverage per requirement instead of
   an overall verdict, and orphan work stands out.
8. **Memory across projects.** Lessons that keep recurring ("this model
   writes better tests when shown one first", "you prefer SQLite")
   promoted to a store every planner reads.
9. **Parallel lanes.** Independent tasks on different backends at once —
   a coder model on one server while a deep model reasons on another. The
   dependency graph already says what can overlap.
10. **A budget she can see.** Time per task is measured and shown now;
    tokens per task, and a running total against the budget on the board,
    would make an expensive dead end visible while it is happening.
11. **A fallback model per task.** A giant that cannot load falls back to
    the smart tier already; the same for any assigned model that is gone
    or keeps timing out — drop a tier and say so, rather than failing.
12. **A regression suite for the loop itself.** A handful of small fixed
    projects run against any new model, prompt or planner change, scored
    on first-attempt passes and audit gaps — so "is this model better at
    being A.R.I.A.?" has a number.
13. **Resumable giant generations.** Persist partial output from very
    large models so a restart continues instead of starting over.
14. **Turn lessons into skills.** When the same lesson shows up in three
    projects, draft a skill from it for you to approve — the loop
    improving its own toolbox.
15. **Mind the machine.** Check VRAM, temperature and disk before starting
    a heavy model; schedule the deep tier for the night.
