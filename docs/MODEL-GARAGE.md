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

## 2. The giants (llama.cpp, NVMe-streamed)

```bash
bin/aria llama install     # once (repaired binaries auto-detected)
```

For each giant: open its HuggingFace repo in a browser → Files → the
smallest **UD-** folder (usually `UD-IQ1_S`; the exact name varies per
repo — the UD- prefix is what matters) → copy each part's download link →

```bash
bin/aria llama get "<part url>"        # repeat per part, resumable
```

| Giant | Repo (search unsloth + "GGUF") | ~Size | Serve as |
|---|---|---|---|
| DeepSeek-R1 671B — **reasoning** | unsloth/DeepSeek-R1-GGUF (NOT "Prover" — that one only does math proofs) | ~170GB | `deepseek`, port 8080 |
| Kimi K2 1T — **research/knowledge** | unsloth/Kimi-K2-Instruct-GGUF | ~250GB | `kimi`, port 8081 |
| Qwen3-Coder 480B — **coding** | unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF | ~180GB | `coder`, port 8082 |
| Kimi K3 2.8T — summit attempt | unsloth/Kimi-K3-GGUF | ~594GB | needs ~600GB free; see BIG-MODELS.md |

The three specialists together ≈ 600GB — fine on a 1.8TB drive.

Each giant gets its **own named service on its own port**, so they're all
reachable at once (they share your NVMe bandwidth — expect them to be
slower when used simultaneously):

```bash
bin/aria llama service deepseek vendor/models/<deepseek-part-00001…>.gguf 8080
bin/aria llama service kimi     vendor/models/<kimi-part-00001…>.gguf     8081
bin/aria llama service coder    vendor/models/<qwen3coder-part-00001…>.gguf 8082
```

Then name them as backends in `system/config.yml`:

```yaml
  backends:
    ollama: http://localhost:11434
    llamacpp: http://localhost:8080
    deepseek: http://localhost:8080
    kimi: http://localhost:8081
    coder: http://localhost:8082
```

## 3. Assign brains to agents

Edit the frontmatter in `system/agents/*.md` — effective on the next run:

```yaml
# strategist.md — general reasoning giant
model: deepseek/r1
# researcher.md — biggest knowledge base for prior-art recall
model: kimi/k2            # (interactive asks get slow — use missions)
# coder.md — the coding giant
model: coder/qwen3-480b   # model name is a label; the backend serves
                          # whatever gguf its service loaded
# critic.md — judgment, still interactive
model: ollama/gpt-oss:120b
# scribe.md — formatting, cheap and instant
model: ollama/llama3.1:8b
```

Tiers still work (`model: deep`) and follow whatever ⚙ Settings says —
use tiers for roles you want to retune centrally, direct specs for roles
married to one model.

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
