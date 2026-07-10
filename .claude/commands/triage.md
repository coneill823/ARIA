---
description: Classify every inbox item and file it to the right pipeline stage
argument-hint: [optional: specific inbox file]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(mv:*), Bash(mkdir:*), Bash(date:*), Task
---

Triage the A.R.I.A. inbox. If a specific file was given ($ARGUMENTS), triage
only that file; otherwise triage every note in `pipeline/00-inbox/` whose
frontmatter says `status: inbox`.

For each note:

1. Delegate classification to the `aria-triage` subagent (parallelize across
   notes when there are several). It returns: `type`, `slug`, `confidence`,
   `reasoning`, and for tasks the best-matching list.
2. Apply the outcome per `system/config.yml`:
   - **project** → set `type: project`, `status: processing`, assign `slug`,
     move the file to `pipeline/01-processing/<slug>.md`.
   - **task** → append a `- [ ]` line (with date) to the matching list in
     `pipeline/knowledge/lists/` (grocery items to `grocery.md`, otherwise
     `tasks.md`), then move the original to `pipeline/archive/` with
     `status: archived`.
   - **note** → set `type: note`, `status: filed`, move to
     `pipeline/knowledge/notes/`.
   - **content** (video/TikTok/post ideas) → `status: filed`, move to
     `pipeline/knowledge/content-ideas/`.
   - **someday** (real idea, not now) → `status: filed`, move to
     `pipeline/knowledge/someday/`.
   - **confidence below the threshold in config** → set `type: unclear`,
     leave the file in the inbox, and append the subagent's clarifying
     questions under `## Questions from A.R.I.A.`.
3. Update `pipeline/LEDGER.md`: current-state row for each item, plus one
   history line per move.

Never delete a note and never rewrite the user's raw capture text.

Finish with a summary table: file → type → destination, and call out
anything left `unclear` that needs the user's input. If any items became
projects, suggest running `/develop`.
