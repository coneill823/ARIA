# A.R.I.A. — Autonomous Reasoning & Intelligence Assistant

A.R.I.A. turns middle-of-the-night ideas into executed projects with as little
human effort as possible. It is a **Claude Code–native pipeline**: a folder
structure (Obsidian-compatible markdown), a set of slash commands that move
ideas through the stages, subagents that do the classification / research /
planning, and a Project Manager (PM) scaffold that executes and maintains
each promoted project for its whole lifespan.

> *"I have way more project ideas than I have time to do those projects."*
> A.R.I.A. exists so the only thing you do by hand is (1) capture the idea and
> (2) review + approve the plan. Everything else is automated.

---

## The pipeline

```
  capture                triage                 develop
 ┌──────────┐  project  ┌───────────────┐      ┌────────────────────────┐
 │ 00-inbox │ ────────▶ │ 01-processing │ ───▶ │ 02-potential-projects/ │
 └──────────┘           └───────────────┘      │   <slug>/              │
      │                                        │     idea.md            │
      │ not a project                          │     research.md        │
      ▼                                        │     proposed-plan.md   │
 knowledge/                                    └────────────────────────┘
   notes/          random ideas & reference               │
   lists/          grocery items, todos          ★ HUMAN IN THE LOOP ★
   content-ideas/  "funny TikTok" material        /review  ↔  you
   someday/        not now, maybe later                   │ /approve
                                                          ▼
                                               requirements.md (approved)
                                                          │ /promote-project
                                                          ▼
                                              active-projects/<slug>/
                                                CLAUDE.md   ← the PM
                                                .claude/agents/ ← its team
                                                STATUS.md, docs/, src/
```

**The only human touchpoint** is the review/approve gate. Nothing gets
promoted or executed without an approved `requirements.md`.

## Quickstart

```bash
git clone https://github.com/coneill823/ARIA && cd ARIA
claude            # start Claude Code; it reads CLAUDE.md and becomes A.R.I.A.
```

Then drive the pipeline with slash commands:

| Command | What it does |
|---|---|
| `/capture <rambling idea>` | Drops the idea verbatim into `pipeline/00-inbox/` with frontmatter. |
| `/triage` | Classifies every inbox item (project / task / note / content / someday) and files it. Projects move to `01-processing/`. |
| `/develop [slug]` | Reads "the ramblings of a madman", researches existing tools & prior art on the web, and produces a **proposed plan** in `02-potential-projects/<slug>/`. |
| `/review <slug>` | Interactive review session: you say what you like and don't like; the plan is revised. |
| `/approve <slug>` | Converts the accepted plan into a full **requirements document** and marks it approved. |
| `/promote-project <slug>` | Moves the project out of the pipeline to the active-projects directory, scaffolds a **PM** (`CLAUDE.md`) and its project-specific **subagents**, and hands over execution. |
| `/status` | Dashboard: what's in each stage, what's stalled, full ledger. |

### Quick capture without opening Claude

```bash
bin/aria capture "app that turns my grocery receipts into a meal planner"
```

This writes the inbox note instantly (no LLM call). Other CLI verbs wrap
headless Claude Code (`claude -p`) so they can run from cron or any
automation:

```bash
bin/aria triage      # classify the inbox
bin/aria develop     # turn processing items into proposed plans
bin/aria status      # print the pipeline dashboard
```

Example cron ("scheduled agent" mode — see OpenJarvis below):

```cron
# triage the inbox every morning at 7am, develop plans at 7:15
0 7 * * *  cd /path/to/aria && bin/aria triage
15 7 * * * cd /path/to/aria && bin/aria develop
```

## Life after promotion

A promoted project is **a directory that is its own Claude Code workspace**.
Its `CLAUDE.md` is the Project Manager: it knows the requirements, maintains
`STATUS.md`, creates and delegates to project-specific subagents
(developer, researcher, market-analyst — whatever the requirements call for),
and remains your interface for the whole lifespan of the project:

```bash
cd active-projects/<slug>
claude
> begin execution          # PM works the milestone plan
> status report            # PM summarizes progress, blockers, next steps
> run analytics            # PM measures whatever the project defines as success
```

By default projects promote to `aria/active-projects/`. On your own machine,
point `system/config.yml → promotion_target` at e.g. `~/Documents/Projects`.

## Layout

```
aria/
├── CLAUDE.md                  # A.R.I.A.'s operating manual (read by Claude Code)
├── .claude/
│   ├── commands/              # the slash commands above
│   ├── agents/                # aria-triage, aria-researcher, aria-planner
│   └── settings.json
├── system/
│   ├── config.yml             # paths, categories, promotion target
│   └── templates/             # idea note, plan, requirements, PM, subagent, status
├── pipeline/
│   ├── 00-inbox/              # raw captures land here
│   ├── 01-processing/         # classified as "project", awaiting development
│   ├── 02-potential-projects/ # proposed plans awaiting YOUR review
│   ├── knowledge/             # everything that wasn't a project
│   ├── archive/               # processed inbox originals
│   └── LEDGER.md              # every idea's journey through the stages
├── active-projects/           # default promotion target (configurable)
└── bin/aria                   # capture + headless automation CLI
```

## Design notes & prior art

- **[open-jarvis/OpenJarvis](https://github.com/open-jarvis/OpenJarvis)** —
  borrowed its execution-mode taxonomy: *on-demand* agents (the slash
  commands), *scheduled* agents (`bin/aria` + cron), and *continuous* agents
  (the PM that lives with a project). Its orchestrator→specialist pattern is
  mirrored in `/promote-project`'s PM + subagent scaffold.
- **[garrytan/gbrain](https://github.com/garrytan/gbrain)** — borrowed its
  signal pipeline shape (capture → classify → enrich → background
  maintenance) and its "schema packs" idea: every note here carries typed
  YAML frontmatter so automation can route it without guessing. Its overnight
  "dream cycle" is the model for the cron examples above. If A.R.I.A.
  outgrows folders-as-database, GBrain is the natural memory backend
  (it speaks MCP, so Claude Code can plug straight into it).
- **[eugeniughelbur/obsidian-second-brain](https://github.com/eugeniughelbur/obsidian-second-brain)** —
  the closest existing implementation of this workflow (inbox triage, idea
  "graduation" into specs, scheduled vault maintenance). Worth mining for
  extra commands; A.R.I.A.'s vault stays Obsidian-compatible on purpose.
- **[VoltAgent/awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents)**
  and **[wshobson/commands](https://github.com/wshobson/commands)** — pattern
  libraries the PM can raid when it designs a project's subagent team.

## Guardrails

1. Nothing is ever deleted — inbox originals are archived, not removed.
2. Nothing is promoted without `approved: true` in `requirements.md`
   frontmatter, set only by `/approve`, which runs only when *you* say so.
3. Triage never guesses on low confidence — items it can't classify are
   marked `type: unclear` with clarifying questions, and wait for you.
