---
description: Turn a processing idea into a researched, proposed project plan
argument-hint: [optional: slug of a specific idea in 01-processing]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(mv:*), Bash(mkdir:*), WebSearch, WebFetch, Task
---

Develop project ideas into proposed plans. If a slug was given ($ARGUMENTS),
develop only that idea; otherwise develop every note in
`pipeline/01-processing/`.

This is the "read the ramblings of a madman and turn them into a proposed
plan" stage. For each idea:

1. Create the project folder `pipeline/02-potential-projects/<slug>/` and
   move the idea note into it as `idea.md` (keep the raw capture verbatim).
2. Delegate to the `aria-researcher` subagent (check
   `system/config.yml → research_web_access` first): it investigates prior
   art ("this already exists"), candidate tools and frameworks ("maybe use
   these tools, try this website"), rough market/audience notes, feasibility,
   and open questions — and writes `research.md` in the project folder, with
   sources.
3. Delegate to the `aria-planner` subagent: given `idea.md` + `research.md`,
   it writes `proposed-plan.md` from `system/templates/proposed-plan.md` —
   the plan the human will review. Frontmatter `status: proposed`.
4. Update `pipeline/LEDGER.md` (state row + history line).

Run research/planning for independent ideas in parallel.

Finish by telling the user which proposed plans are now awaiting their
review, with a one-line pitch for each, and remind them: `/review <slug>` to
give feedback, `/approve <slug>` when ready. Do NOT approve anything
yourself — this is the human's gate.
