# Integrations

How A.R.I.A. connects to the wider world — what is wired in, what is one
switch away, and what is documented-only (with the reason). Guiding rule:
**every reach is something you switched on, and each has a boundary she
cannot talk her way across** — the web but never your local network,
GitHub but never your base branch, your vault but only her own folder,
webhooks but only the ones you named, code but only its tests, in a box.

## Her reach

### The internet

`web_access: true` (Settings → Internet) makes three things real:

- **In chat and by voice** — "search for …" runs a search, reads the best
  pages and answers from them with numbered sources; "read <url>" fetches
  a page and summarises it. With the switch off she says plainly that she
  cannot browse — she never pretends.
- **In the develop stage** — a new idea is researched on the live web
  before its plan is drafted, and `research.md` lists its sources. With
  the switch off the research is labelled as unverified recall.
- **In autopilot** — tasks the planner marks *research* (and anything the
  `researcher` agent does) get sourced material attached, and the sources
  travel into the change record and the final report.

Where searches go is `web_search`: `auto` tries your SearxNG (if
`web_search_url` is set), then Brave (if `ARIA_KEY_BRAVE` is set), then
DuckDuckGo, then Wikipedia. The last two need no key and no setup.

What she cannot do with it, whatever a web page tells her: reach your
**local network** (private, loopback and link-local addresses are refused
on every redirect hop, and the connection is pinned to the address that
was checked), send anything but a read request, or act on instructions
found in a page — fetched text is handed to the model marked as untrusted
material, and the model has no tools to act with anyway. `web_allowlist`
narrows fetching to sites you name.

The honest cost: with this on, search queries leave your machine.

### GitHub

Local first: when a project is its own git repository, autopilot commits
every verified task to the branch `aria/autopilot`
(`autopilot_git: true`). She stages only the files a task wrote and never
commits to your base branch.

`github_access: true` (Settings → Git & GitHub) adds the remote:

```bash
aria github init <slug>      # make a project its own repository
aria github publish <slug>   # create a private GitHub repo and push it
aria github push <slug>      # push aria/autopilot, open a pull request
```

At the end of every run she pushes the branch and makes sure a pull
request is open against `github_base_branch`. Nothing is ever forced and
the base branch is never pushed to — merging is yours. She stores no
credentials: GitHub is reached through the `gh` CLI you signed in with
(`gh auth login`), or a token in `ARIA_GITHUB_TOKEN`.

To work on a repository she did not create:
`aria adopt <git-url | owner/repo | folder> "what you want done"`.

### Your Obsidian vault

Set `obsidian_vault` (Settings → Obsidian brain) and she writes into
`<vault>/ARIA/` — and nowhere else in the vault:

| Where | What |
|---|---|
| `ARIA/<project>.md` + `ARIA/<project>/` | a hub note linking the idea, research, plan and requirements, and — once autopilot has run — her assessment, memory, changelog and final report. Refreshed after every batch. |
| `ARIA/Notes/` | things you tell her to keep: "note to brain: …" |
| `ARIA/Research/` | web answers you save from chat, with their sources |
| `ARIA/Journal/YYYY-MM-DD.md` | one line for each thing she did that day |

Every note carries tags and wikilinks, so it joins your graph.

### Running code

Two different things, with a hard line between them.

**She runs a project's tests** — after every code change, in a sandbox
(bubblewrap): no network, the machine read-only with home folders, other
drives and the desktop's sockets not there at all, a scratch copy of the
project as the only writable place, a time limit. A change that breaks
passing tests is rolled back. No bubblewrap, no execution — the report
says "not tested by running it". `autopilot_checks: sandbox | direct |
off`. Details: [AUTOPILOT.md](AUTOPILOT.md#she-runs-the-tests).

**You run the program** — the Run tab shows the command and starts it on
your click, in the project's folder, sandboxed to that folder where
bubblewrap is installed. Nothing else can start one: the route answers
only to the app's own page, chat and voice only open the tab, and
autopilot has no way to call it.

### Your desktop

From the Files tab (or "open the folder for *project*", `aria open`):
the project's folder in your file manager, the project or a file in your
editor, a terminal in its directory — only ever a project's own folder,
only on your click, and a file goes to an editor command, never to
"whatever opens this type" (a model-written `.desktop` file or script is
not launched by looking at it). She also sends a desktop notification
when a task waits on your decision and when a run ends
(`desktop_notifications`, needs `notify-send`).

`bin/aria app` opens her in a window of her own (Qt WebEngine or
WebKitGTK when installed; a chromeless browser window otherwise). In
that window, links to anywhere else open in your browser and the
microphone is granted to her own page only.

### Giant models

Larger-than-RAM models get a role — `general`, `research`, `coder` —
and she starts the right `llama-server` when a task needs it, unloading
the others first. One that is still downloading is skipped; one that
fails to load is reported and stepped around. She only ever starts and
stops servers she started herself. See
**[MODEL-GARAGE.md](MODEL-GARAGE.md)**.

### Voice

The app's microphone works everywhere (Chrome's built-in recognition, or
local whisper elsewhere — including her own desktop window). For Echo devices see **[ALEXA.md](ALEXA.md)**:
a skill, a small Lambda, and `aria voice-gateway` — the one
authenticated route a tunnel is ever pointed at.

## Wired in

| Project | What it is | How A.R.I.A. uses it |
|---|---|---|
| [anthropics/skills](https://github.com/anthropics/skills) | Official Agent Skills | Default skill source (`system/skill-sources.txt`) |
| [prompt-master](https://github.com/nidhinjs/prompt-master) | Turns rough asks into optimized prompts | Skill source — `use skill prompt-master` in chat, or assign it to an agent |
| [andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills) | Karpathy-derived coding discipline | Skill source; also worth appending its CLAUDE.md to a promoted project's PM |
| [marketingskills](https://github.com/coreyhaines31/marketingskills) | 53 marketing skills | Skill source — give the `scribe` or a project's market-researcher agent `skills: [copywriting, launch, seo-audit]` |
| [n8n](https://github.com/n8n-io/n8n) | Self-hosted workflow automation | **A.R.I.A.'s hands.** Name n8n webhook URLs under `aria.webhooks`, then say "trigger morning-digest" in chat or `aria trigger deploy-site`. n8n's 400+ nodes (email, calendars, home automation, deploys) become things she can set in motion — only the flows you listed. |
| [uptime-kuma](https://github.com/louislam/uptime-kuma) | Self-hosted monitoring | Set `aria.heartbeat_url` to a Kuma *push* monitor; every successful auto-pipeline pass pings it, so you get alerted if A.R.I.A. stops running. |
| [OpenHands](https://github.com/OpenHands/openhands) | Autonomous AI coding agent | `aria handoff <slug>` generates `openhands.sh` in the project: Docker launch, workspace mounted, LLM pointed at your local smart tier, task seeded from requirements. Generated-not-executed — review before running. |
| [OmniRoute](https://github.com/cheaper-inference/OmniRoute) | Self-hosted AI gateway (1 endpoint, 1400+ models) | Any OpenAI-compatible server is a backend now: uncomment `omniroute:` under `aria.backends`, add its key under `backend_keys` (or `ARIA_KEY_OMNIROUTE`), set e.g. `deep: omniroute/auto`. Note: routing to cloud providers leaves your machine — keep tiers local where privacy matters. |
| [colibri](https://github.com/JustVugg/colibri) | Tiny C engine that streams MoE experts from disk | Another way to run huge models on your 16GB/64GB box: `coli serve` exposes `/v1`, so `backends: colibri: http://localhost:8338` + `deep: colibri/<model>`. Candidate replacement for the llama.cpp deep preset if RAM gets tight. |
| [Soup](https://github.com/MakazhanAlpamys/Soup) | Fine-tune LLMs from one YAML | Two hooks: `soup serve` is OpenAI-compatible (add as a backend), and longer-term you can fine-tune a personal triage model on your own ledger history, export to GGUF, `ollama create`, and point `fast` at it. |
| [ECC](https://github.com/affaan-m/ecc) | ~330 skills and ~70 specialist agents for coding harnesses | An `engine-only` skill source (`system/skill-sources.txt`): `aria install-skills` links its skills into `skills-library/` and its `agents/` folder becomes the **agent library**. Autopilot never loads them wholesale — for each project the planner is offered the specialists and skills that fit the goal (a Django project sees `django-reviewer` and `django-patterns`; a marketing one sees `seo-specialist`). For Claude Code itself, install it as a plugin: `/plugin marketplace add https://github.com/affaan-m/ECC`. |

## Documented — use alongside, not inside

- **[Proxmox VE](https://www.proxmox.com/en/products/proxmox-virtual-environment/overview) / [Coolify](https://github.com/coollabsio/coolify) / [homelab-1](https://github.com/orinocoz/homelab-1) / [awesome-selfhosted](https://github.com/awesome-selfhosted/awesome-selfhosted)** —
  the natural home for the full stack: one host (VM or Coolify project)
  running Ollama + A.R.I.A. + n8n + Uptime Kuma. A.R.I.A. itself stays a
  plain systemd service; nothing in it assumes a hypervisor. The
  awesome-selfhosted list and homelab-1 are seeded into the researcher
  agent as reference leads.
- **[awesome-tunneling](https://github.com/anderspitman/awesome-tunneling)** —
  for reaching A.R.I.A. from your phone. She binds 127.0.0.1 **on
  purpose** and has no auth layer, so never expose her with a public
  tunnel; use a private overlay (Tailscale/WireGuard class) and keep the
  LAN boundary.
- **[build-your-own-x](https://github.com/codecrafters-io/build-your-own-x)** —
  reference lead for the researcher agent: when a captured idea is
  "build my own …", the matching tutorial belongs in research.md.
- **[agent-reach](https://github.com/Panniantong/agent-reach)** — read-only
  CLI access to X/Reddit/YouTube/etc. Install it yourself
  (`~/.agent-reach/`) and use it from Claude Code sessions, where there's
  a real tool harness; A.R.I.A.'s local chat deliberately has no shell
  execution, so it is not auto-wired. Paste its output into `/review` or
  a capture when research needs gated sources.
- **[remotion](https://github.com/remotion-dev/remotion)** — videos in
  React. Fits the `content-ideas` lane: a promoted content project's PM
  (or CrewAI crew) can scaffold a Remotion project; not wired by default.
- **[TauricResearch](https://github.com/TauricResearch) (TradingAgents)** —
  multi-agent trading framework. Deliberately NOT integrated: wiring an
  autonomous pipeline to financial decisions is a different risk class.
  If you explore it, run it standalone, paper-trading only.

## The control model

Everything above follows one pattern: **a switch you flip, and a boundary
that holds whatever a model says.**

- She has no shell. Agents produce text and files; files land only inside
  their project, never over the approved requirements or her own records.
  The one thing she executes is a project's own tests, inside a sandbox
  with no network and nothing writable but a scratch copy.
- Programs are started by you, from the Run tab — never by chat, voice
  or autopilot.
- The web is read-only and stops at your local network.
- Git stays on `aria/autopilot`; nothing is forced; merging is yours.
- The vault is written only under `ARIA/`.
- Webhooks, backends, skills and agents are the ones you named.
- The server binds 127.0.0.1 and has no login — never expose it. It also
  refuses what a web page could send it from your own browser: a foreign
  Host (DNS rebinding), a request marked as coming from another site —
  another local port included, such as a project's own web app — and a
  POST with a foreign Origin. (`ARIA_ALLOWED_HOSTS=name,…` adds hostnames
  if you reach it through your own private proxy.) The only thing built
  to face the internet is `aria voice-gateway`.
- The human gate: nothing is executed without a plan you approved, a
  task she marks as yours to decide waits for your answer, and a voice
  can give neither.
