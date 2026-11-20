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

shows what each role has and what is loaded. Then **prove it**, before a
night's work depends on it:

```bash
bin/aria models test general
```

loads that giant, asks it one question and reports how long the load
took, how long the answer took and what it said — or the server's own
error if it could not. The **Test** button beside each giant in Settings
does the same and keeps the result on the page. A first load of a model
larger than your RAM takes minutes; that is the test working, not
hanging.

What then happens on its own:

- **On demand.** The first task that needs a role starts its
  `llama-server` (the `giant` preset: experts on CPU, weights mmap'd).
  The first load can take several minutes; the Live tab and the sidebar
  say what is loading.
- **One at a time.** Before a giant loads, the least recently used one is
  unloaded — never in the middle of an answer. `giants_loaded: 2` allows
  two at once if your RAM takes it.
- **They take turns with Ollama.** Before a giant loads, Ollama is asked
  to unload whatever it is holding, so the giant gets the graphics memory
  and the RAM. The other way round too: once the giant has finished its
  answer, the next call to an everyday model (the verifier, the planner, a
  chat) unloads it first, so qwen3:30b gets the whole card back instead of
  being squeezed onto the CPU beside a giant that is doing nothing. A giant
  in the middle of an answer is never unloaded. Each swap is written in
  the project's activity log. The price is a reload when the next hard
  task comes — seconds to minutes, since the weights stream from disk
  anyway. `giants_free_memory: false` turns both directions off.
- **Grouped.** Tasks that can run on the giant already loaded go before
  tasks that would need a swap.
- **Still downloading?** A model with parts missing, or a part still being
  written, is not offered to the planner; work that asked for it runs on
  the smart tier and says so. It joins in by itself once the files are
  complete.
- **A load that fails** (out of memory, a damaged file) is reported with
  the server's own error in Settings and `aria models`, and stepped around
  until the files change or you press Retry. Logs: `system/garage/<role>.log`.
- **No `llama-server`, no giants.** A model file can be complete and
  still have nothing to run it. Settings then says **cannot start** in
  place of "ready to load", `aria models` and `aria doctor` say the same,
  and the giants are not offered to the planner. `bin/aria llama install`
  builds it.
- **Idle.** A giant nobody has used for `giants_idle_minutes` (20 by
  default) is unloaded, so it does not sit on the memory while nothing
  needs it; 0 keeps the last one in memory.

If a load runs out of VRAM beside your everyday Ollama model, give the
giants fewer GPU layers: `bin/aria settings set giants_flags -ngl 20`.

> **Earlier setup?** If you created the per-model services from the
> previous version of this guide (`aria-llama-deepseek`, `-kimi`, `-coder`
> on ports 8080–8082), they keep all three loaded at once. The roles
> above replace them — stop the old ones so the memory is free:
> `systemctl --user disable --now aria-llama-deepseek aria-llama-kimi aria-llama-coder`

## 3. Who uses which model

**Autopilot decides, task by task** (`giants_use`, Settings → Giant models):

| `giants_use` | What happens |
|---|---|
| `auto` (default) | A giant gets the **hard** tasks: the planner marks the few an everyday model is likely to get wrong — a design the rest of the project hangs on, an intricate algorithm, a change that has to stay consistent across several files, subtle debugging, weighing a lot of research. And a task an everyday model has had sent back twice gets its last attempt on a giant. Everything else stays on the quick models. |
| `always` | Every coding task goes to `coder`, research to `research`, everything else to `general`. Strongest, and slowest by far. |
| `never` | Giants are used only where you name them yourself. |

Which giant: the coder for code, the researcher for research, the
general one for the rest — and when the one that fits is not set up, the
next best you have (with no coder giant, hard coding work goes to the
general one). The task's card on the board shows the giant and why it
was chosen; the Live tab says when one is loading.

Planning, verification and memory stay on the `fast`/`smart` tiers either
way — the loop makes many small calls, and those should be quick. Keep
those tiers on GPU-speed models (`ollama pull qwen3:30b`, then
`smart: ollama/qwen3:30b`).

**Never seen a giant used?** Three things to check, in this order:
`bin/aria doctor` (is `llama-server` there?), `bin/aria models test
<role>` (does it load and answer on this machine?), and `giants_use`
(with `never`, only an agent you pin gets one). To see every task go to
one regardless of difficulty, `bin/aria settings set giants_use always`.

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
- Get pinged when one finishes: add a `mission-done` webhook —
  `bin/aria settings set webhooks.mission-done <url>` (n8n → phone
  notification).
- The pipeline is never blocked: a week-long K3 mission runs beside
  hourly triage/develop without contention for anything but the hardware.
- No server running? `aria missions run` drains the queue in a terminal.

The intended rhythm: interactive agents on the Ollama shelf during the
day; before bed, three or four missions to the giants; coffee + outputs
in the morning.
