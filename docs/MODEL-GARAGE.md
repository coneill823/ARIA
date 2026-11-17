# The model garage — install every model, let every agent choose

The end state: five-plus models installed, each agent assigned the brain
that fits its job, and background **missions** so the slow giants work
while you sleep. Agents pick a model in their frontmatter
(`system/agents/*.md`) — either a tier (`fast`/`smart`/`deep`) or a
**direct spec** like `ollama/gpt-oss:120b` or `kimi/k2`.

## 1. The Ollama shelf (always loaded on demand)

```bash
ollama pull llama3.1:8b    # tiny & instant — triage fallback      (done)
ollama pull qwen3:30b      # the daily driver — fast + smart       (done)
ollama pull gpt-oss:120b   # the heavyweight that still fits RAM   (done)
```

All three are available to every agent immediately, no serving needed:
Ollama loads/unloads on demand (your drop-in allows 3 loaded at once).

## 2. The giants — one role each, loaded when the work needs them

Your largest models are bigger than RAM, so llama.cpp streams them from
NVMe. Each gets a **role**; A.R.I.A. starts the right one when a task needs
it and unloads the others first — they share one GPU and one page cache.

| Role | For | Typical model (search unsloth + "GGUF") | ~Size |
|---|---|---|---|
| `general` | all-round reasoning, planning, writing | DeepSeek-R1 671B — `unsloth/DeepSeek-R1-GGUF` (not "Prover": that one only does math proofs) | ~170GB |
| `research` | research, broad knowledge | Kimi K2 1T — `unsloth/Kimi-K2-Instruct-GGUF` | ~250GB |
| `coder` | writing and reviewing code | Qwen3-Coder 480B — `unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF` | ~180GB |

Download each part of a model (`bin/aria llama get "<part url>"`, resumable
— or any downloader, into any folder), then tell her which is which:

```bash
bin/aria llama install
```

```bash
bin/aria models scan
```

`scan` looks in `vendor/models`, `~/models`, `~/Downloads` and a few other
usual places (`bin/aria models scan <folder>` for anywhere else), counts a
split model once, flags one that is still downloading, and prints the
command to use each:

```bash
bin/aria models use general "/path/to/DeepSeek-R1-UD-Q2_K_XL-00001-of-00005.gguf"
```

```bash
bin/aria models use research "/path/to/Kimi-K2-Instruct-UD-IQ1_M-00001-of-00007.gguf"
```

```bash
bin/aria models use coder "/path/to/Qwen3-Coder-480B-A35B-Instruct-UD-Q2_K_XL-00001-of-00006.gguf"
```

The same lives in the app: **Settings → Giant models → Scan → Use**.

```bash
bin/aria models
```

shows what each role has and what is loaded. What then happens on its own:

- **On demand.** The first task that needs a role starts its
  `llama-server` (the `giant` preset: experts on CPU, weights mmap'd).
  The first load can take several minutes; the Live tab and the sidebar
  say what is loading.
- **One at a time.** Before a giant loads, the least recently used one is
  unloaded — never in the middle of an answer. `giants_loaded: 2` allows
  two at once if your RAM takes it.
- **Grouped.** Tasks that can run on the giant already loaded go before
  tasks that would need a swap.
- **Still downloading?** A model with parts missing, or a part still being
  written, is not offered to the planner; work that asked for it runs on
  the smart tier and says so. It joins in by itself once the files are
  complete.
- **A load that fails** (out of memory, a damaged file) is reported with
  the server's own error in Settings and `aria models`, and stepped around
  until the files change or you press Retry. Logs: `system/garage/<role>.log`.
- **Idle.** `giants_idle_minutes` unloads a giant nobody has used for that
  long; 0 keeps the last one in memory.

If a load runs out of VRAM beside your everyday Ollama model, give the
giants fewer GPU layers: `giants_flags: -ngl 20` in `system/config.yml`.

> **Earlier setup?** If you created the per-model services from the
> previous version of this guide (`aria-llama-deepseek`, `-kimi`, `-coder`
> on ports 8080–8082), they keep all three loaded at once. The roles
> above replace them — stop the old ones so the memory is free:
> `systemctl --user disable --now aria-llama-deepseek aria-llama-kimi aria-llama-coder`

## 3. Who uses which model

**Autopilot decides, task by task** (`giants_use`, Settings → Giant models):

| `giants_use` | What happens |
|---|---|
| `auto` (default) | The planner sees the giants with their price — about 0.5–2 words a second, so one task can take an hour — and gives a task one only when the work is worth that wait. |
| `always` | Every coding task goes to `coder`, research to `research`, everything else to `general`. Strongest, and slowest by far. |
| `never` | Giants are used only where you name them yourself. |

Planning, verification and memory stay on the `fast`/`smart` tiers either
way — the loop makes many small calls, and those should be quick. Keep
those tiers on GPU-speed models (`ollama pull qwen3:30b`, then
`smart: ollama/qwen3:30b`).

**You can also pin an agent** in its frontmatter (`system/agents/*.md`) —
a role name is a model like any other:

```yaml
# coder.md — always the coding giant
model: coder
# researcher.md
model: research
# strategist.md
model: general
# critic.md — judgment, still interactive
model: ollama/gpt-oss:120b
# scribe.md — formatting, cheap and instant
model: ollama/llama3.1:8b
```

A tier can point at a role too (`deep: general`), and
`ask the strategist to …` in chat then waits for that giant like any
other task would. For interactive asks prefer missions (below).

## 4. Missions — agents running in the background

A normal "ask the critic to…" streams live into your chat. A **mission**
runs server-side: it survives closed tabs, logouts of the browser, even
server restarts (interrupted missions re-queue and start over).

```text
you (in chat):  mission strategist to stress-test the meal planner as a business
A.R.I.A.:       Mission queued for the strategist — it runs in the background…
```

- Queue from anywhere: chat ("mission/overnight/background <agent> to …"),
  CLI (`aria mission strategist "…"`), one at a time in queue order.
- Watch them: the **Agents page** lists missions live (click one to read
  its output), `aria missions` in a terminal, or just ask A.R.I.A. —
  mission state is in her head.
- Results persist in `pipeline/missions/<id>.md` under `## Output`.
- Get pinged when one finishes: add a `mission-done` webhook under
  `aria.webhooks` (n8n → phone notification).
- The pipeline is never blocked: a week-long K3 mission runs beside
  hourly triage/develop without contention for anything but the hardware.
- No server running? `aria missions run` drains the queue in a terminal.

The intended rhythm: interactive agents on the Ollama shelf during the
day; before bed, three or four missions to the giants; coffee + outputs
in the morning.
