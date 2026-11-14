# Integrations

How A.R.I.A. connects to the wider ecosystem — what's wired in, what's one
config line away, and what's documented-only (with the reason). Guiding
rule: **A.R.I.A. gets as much reach as possible through allowlists you
control** — named webhooks, named backends, named skills — never arbitrary
shell access for a local model.

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
| [ECC](https://github.com/affaan-m/ecc) | 300+ skills, 71 agents for coding harnesses | Opt-in line in `skill-sources.txt` (it's huge). Better in Claude Code sessions as a plugin: `/plugin marketplace add https://github.com/affaan-m/ECC` — promoted-project PMs benefit most. |

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

Everything above follows one pattern: **capability through named
allowlists**. A.R.I.A. can only hit webhooks you listed, backends you
named, skills you installed, agents you defined. She has no shell, no
arbitrary URLs, and the server never leaves 127.0.0.1. That's what "as
much control as possible" means here: maximum reach, no blank cheque.
