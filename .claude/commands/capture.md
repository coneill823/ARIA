---
description: Capture a raw idea into the A.R.I.A. inbox, verbatim
argument-hint: <the idea, rambling welcome>
allowed-tools: Write, Read, Bash(date:*)
---

Capture the following idea into the pipeline inbox:

$ARGUMENTS

Steps:

1. If no idea text was provided above, ask for it and stop.
2. Get the current timestamp (`date -u +%Y-%m-%dT%H:%MZ` and `date +%Y%m%d-%H%M`).
3. Create `pipeline/00-inbox/YYYY-MM-DD-HHMM-<short-snippet>.md` where
   `<short-snippet>` is a 2–4 word kebab-case hint of the topic, using the
   idea-note template at `system/templates/idea-note.md`:
   - frontmatter: `status: inbox`, `type: unclassified`, `source: claude`,
     `slug:` left empty (assigned at triage)
   - body under `## Raw capture`: the idea text **verbatim** — do not fix
     grammar, do not summarize, do not editorialize.
4. Confirm with one line: the file path, and a reminder that `/triage` will
   classify it.

Do NOT classify, research, or plan here. Capture must stay instant and dumb —
that's what makes it trustworthy at 3am.
