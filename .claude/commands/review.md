---
description: Interactive review of a proposed plan — the human-in-the-loop stage
argument-hint: <slug of a project in 02-potential-projects>
allowed-tools: Read, Write, Edit, Glob, Grep, WebSearch, WebFetch
---

Run a review session for the proposed plan at
`pipeline/02-potential-projects/$ARGUMENTS/proposed-plan.md`.

If no slug was given, list the projects currently awaiting review (frontmatter
`status: proposed`) with one-line pitches and ask which one to review.

Session flow:

1. Present a tight summary of the proposed plan: the pitch, the approach,
   scope, milestones, and the open questions from `research.md`.
2. Take the user's feedback — "I like this part, I don't like this part" —
   and **revise `proposed-plan.md` in place** after each round. Record each
   decision under `## Review log` at the bottom of the plan (date, what
   changed, why).
3. If feedback demands new information, do targeted research and update
   `research.md` too.
4. Keep iterating until the user is satisfied. If they say they approve
   ("approve it", "I'm ready, let's book it", "ship it"), invoke the
   `/approve $ARGUMENTS` flow immediately — don't make them run it
   separately.

Rules: this command never sets `approved: true` on its own, never promotes,
and never starts execution. Disagreement with the user's feedback is worth
voicing once, clearly — then do what they decided.
