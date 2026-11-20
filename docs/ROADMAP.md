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
| **Planning one batch at a time** — each plan is sensible and the sum goes nowhere | A roadmap of milestones from where the project is to its goal, each an outcome that can be checked, with what it waits for. Every batch is planned against it, every task belongs to a milestone and is told where it fits, and the road is redrawn when you steer or an audit finds gaps |
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
| **The wrong model for the job** | Giants get a role each and are loaded on demand, one at a time, with Ollama asked to free the memory first; the planner marks the hard tasks and those go to a giant; a task an everyday model had sent back twice gets its last attempt on one; work is grouped by the model already loaded |
| **A big model that was never really usable** | `aria models test <role>` loads a giant, asks it a question and times both; a model with nothing to run it is shown as unable to start, not as ready |
| **Losing the thread of a long run** | The Gantt, the board and the reports keep everything the project ever did; times are measured, and predictions come from them |
| **Estimates for a machine that does not exist** | A planner's "est. hours" is a guess at a person's effort on an ideal system. Her estimates come from what this machine has measured — every task on every model, the planning and checking around each one, how many tasks a milestone really took against the guess, how often work is sent back — and say what they rest on, with an ordinary bad day beside the typical one |
| **"Start the model server first"** | She starts Ollama herself, the way it is run on this machine, when she starts and whenever a call finds it away; loads the quick model so the first reply does not wait; and keeps the everyday models in memory between uses |
| **A plan with no business behind it** | Every plan comes with a business plan (figures sourced, or marked as estimates with the arithmetic), a list of what a complete proposal still needs, and the remaining work split into her part and yours |
| **The work only you can do, forgotten** | Your part has a list of its own — in the app, in `aria mylist`, by voice — that she surfaces and prepares for, and never ticks |
| **Not being able to see what she did** | Her last forty outputs are kept in full and can be paged through; every verified change keeps the file as it was, so any change can be read as before and after |
| **A question that waits behind the work** | Progress, what needs you and status are answered from her records without a model; anything else gets a reply in bounded time — if the model is busy she says so, and where things stand |
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
   needs you, and an Echo answers when you ask it how things stand.
   Next: a daily summary of what happened overnight, a push when she has
   waited an hour on the model server, and the Echo speaking first
   (Amazon's proactive events).
7. **Trace every milestone to a requirement.** Each task names its
   milestone today. Next, each milestone names the requirements it
   covers; the audit then reports coverage per requirement instead of an
   overall verdict, and orphan work stands out.
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

## For making projects — and businesses

The list above is about the loop staying on the rails. This one is about
what she makes with it: a plan that is worth building, and a business
that is worth starting. The questions behind it, with their sources, are
in [PROJECT-GROUNDWORK.md](PROJECT-GROUNDWORK.md); what is there today is
the business plan beside every plan, the proposal checklist, and the
split into her part and yours. What would add most from here, in rough
order of value for effort.

### Business making

1. **Numbers that are calculated, not written.** The money section is
   prose from a model, and models are poor at arithmetic. Keep the inputs
   — price, costs, how many buy, how many leave — in a small file she
   fills in and you correct, and let code work out break-even, what a
   customer is worth against what one costs to win, and twelve months of
   cash, in three columns: a bad year, the expected one, a good one. The
   plan then quotes figures that add up, and "what if the price is a
   fifth lower?" is a change to one cell.
2. **Assumptions on a list, riskiest first.** Every plan rests on a few
   things nobody has checked. Pull them out of the prose into a tracked
   list: the assumption, the cheapest test of it, and — decided *before*
   the test — what result would count as a pass. Results go back into
   the plan, and each milestone ends with the question the plan exists to
   answer: carry on, change course, or stop.
3. **A sceptic's read before you approve.** One extra call at the gate,
   on a different model than the one that wrote the plan: assume the
   project has failed a year from now and say why (a premortem), then
   read the plan as a lender would and list the five questions they
   would ask. The answers she can find go in; the rest go on your list.
4. **A customer-interview kit.** "Talk to five people who have this
   problem" is on your list for nearly every idea. She can make it
   short: a script that asks about what people actually did rather than
   what they would do, a folder to drop recordings or notes into, her
   own speech recognition to transcribe them on this machine, and a
   tally of what was said — in their words — that updates the evidence
   in the plan's first section.
5. **Your list on the road and on the calendar.** Your items have no
   dates and block nothing today. Give each a lead time and a due date,
   let her tasks depend on them ("payments wait for the merchant account
   you are opening"), draw them on the Gantt, and have her say — in the
   app, by notification, by voice — when the thing the project is
   waiting for is you.
6. **Country packs.** What she writes about registration, tax, licences
   and personal data comes from a model's memory, checked against
   whatever a search finds. A cited checklist per country, kept as a
   file and chosen by *Where you are based*, would put the official
   steps on your list with their links, and leave the model to explain
   rather than recall.
7. **One source, several documents.** The same facts, laid out for who is
   reading: a one-page canvas, a project proposal, a business case, a
   grant application — or a funder's own template, dropped into the
   project folder for her to fill. With an export to PDF or Word, since
   nobody outside reads Markdown.
8. **Is the name free?** For each candidate name: the domain, the usual
   code and package registries, and links to the trade-mark and company
   registers for where you are based. The choosing stays yours; the
   looking-up does not have to be.
9. **A watch on the market.** The competitor research is a snapshot from
   the day the idea was developed. Run it again each month for active
   projects and report only what changed: a new entrant, a price, a
   product that closed.
10. **Which idea first?** With a dozen proposals waiting, choosing is the
    work. A view across all of them: what each would earn or save, how
    sure the evidence is, and how long it would take — the last from
    this machine's measured pace rather than anyone's guess.
11. **Money actually spent.** Domains, hosting, licences, fees: a ledger
    per project beside the plan's figures, so the plan's costs become
    measured the way the time estimates did.
12. **Where the money could come from.** A research run for grants,
    competitions and loans that fit the project and the place, each with
    its deadline and its own template — and the application on your list
    with her draft attached.

### Project making

1. **The check written before the work.** A milestone is "an outcome
   that can be checked", and today a judge model does the checking.
   Where the outcome can be tested, write the test first — the planner
   states it, a task is done when it passes — so "done" is decided by
   the project's own tests rather than by an opinion.
2. **Must, should, could.** Requirements carry no priority, so when time
   runs short everything is equally late. Mark each must / should /
   could / not this time, keep the musts to about three fifths of the
   effort, plan them first — and when a budget in hours is set, have her
   say at the measured pace what fits, and drop the coulds out loud.
3. **A finish date, not only hours.** "38 hours of work left" does not
   say when. She knows when this machine has actually been working —
   which hours, how many a day — so the estimate can be a day: "at this
   pace, Thursday evening; on a bad week, Sunday". And a line on how
   her estimates have done so far on this machine.
4. **Risks that stay alive.** The plan lists risks once. Keep them as a
   register she reads at every audit: is this one more likely than it
   was, has that one happened — and a risk that has happened becomes a
   task, not a sentence.
5. **Starter kits by kind of project.** A command-line tool, a web app, a
   data pipeline, a book, a service: each with its usual milestones,
   what "done" means for it, the risks that kind of project runs, and
   the checks that go with it — accessibility and personal data for
   anything with a screen and users.
6. **Licences, secrets and known holes, checked before a push.** What
   licence does each dependency carry and does it fit the project's own;
   is there a key or a password in the tree; does a dependency have a
   published vulnerability. Mechanical, quick, and the kind of thing
   that is only ever found afterwards.
7. **Decisions on record.** Your answers at the gate, your decisions on
   waiting tasks and your steering are scattered across the board and
   the reports. One file per project — what was decided, when, why,
   what it rules out — answers "why is it built this way?" a year on,
   for you and for her next plan.
8. **A look back when a project ends.** Estimate against actual, the
   premortem against what really went wrong, what was sent back most
   and why — and the lessons promoted to the memory every planner
   reads (items 8 and 14 of the list above).
9. **Five people try it.** Once something runs: a short script for
   watching someone use it, a place for what you saw, and the findings
   turned into tasks — the same loop as the interview kit, pointed at
   the thing instead of the idea.

### Where to start

Three would change the most for the least: **calculated numbers** (the
part of a business plan a model does worst), **the sceptic's read** (one
call, at the moment a mistake is cheapest), and **your list on the road**
(the usual reason a project stands still is a thing only you can do).
