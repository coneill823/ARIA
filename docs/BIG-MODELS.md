# Running huge models (up to ~1T parameters)

How far your box (RX 9070 XT 16GB + 62GB RAM + NVMe) can actually go, and
how A.R.I.A. is wired for it. Short version: **yes, even 1T-class models
run — slowly — because the big open models are MoE** (mixture-of-experts):
a 1T model like Kimi K2 only *activates* ~32B parameters per token. Fast
memory holds the always-used layers; the rest streams from NVMe.

## The memory ladder on this machine

| Fits where | Feels like | Models (low-bit "dynamic" quants) |
|---|---|---|
| VRAM (16GB) | instant | Qwen3-30B-A3B Q4 (~19GB, partly in RAM) — the `smart` default |
| RAM+VRAM (~78GB) | fast (5–20 t/s) | gpt-oss-120b (~63GB), GLM-4.5-Air, Qwen3-235B-A22B at ~2-bit (~85GB, borderline) |
| NVMe-streamed | **0.5–2 t/s** | DeepSeek-V3.x **671B** (37B active, ~170–230GB), **Kimi K2 1T** (32B active, ~250–380GB) |
| NVMe-streamed, summit | **~0.05–0.25 t/s** | **Kimi K3 2.8T** (104B active — UD-IQ1_S ~594GB). Runs, but ~22GB of experts stream per token: answers take overnight-to-days. Needs ~600GB+ free disk; skip the 861GB 2-bit on a 1.8TB drive. |

Rules of thumb: weights-on-disk works because of mmap — the OS pages in
only the experts each token touches. Generation speed is then your SSD's
random-read speed, not your CPU. First response after load is the slowest.
You have ~1.6TB free, so even Kimi K2 fits on disk comfortably.

## Path A — llama.cpp `giant` preset (built in)

```bash
bin/aria llama install          # once: prebuilt Vulkan llama-server
# example: DeepSeek-R1 671B at 1.58-bit (~170GB, multi-part GGUF) —
# download every part listed on the model's HuggingFace page:
bin/aria llama get <url-of-part-00001> 
bin/aria llama get <url-of-part-00002>   # ... repeat per part
bin/aria llama serve giant vendor/models/<first-part-00001-of-000NN>.gguf
```

That serves one giant by hand on :8080 (`deep: llamacpp/giant`). You
rarely need to: give the model a **role** instead and A.R.I.A. starts and
swaps the giants herself —

```bash
bin/aria models scan
```

```bash
bin/aria models test general
```

— the second loads the giant you gave that role, asks it one question and
times both, so you know it really runs on this machine before a night's
work depends on it. See **[MODEL-GARAGE.md](MODEL-GARAGE.md)**. Either
way the preset keeps
**mmap on** (that's the disk streaming), puts every expert on CPU
(`--n-cpu-moe 999`), attention on the GPU, and shrinks/quantizes the KV
cache to protect your 62GB of RAM.

Where to get the quants: look for **Unsloth "UD" dynamic quants** on
HuggingFace (`unsloth/DeepSeek-R1-GGUF`, `unsloth/Kimi-K2-Instruct-GGUF` —
the TQ1_0/Q2_K_XL variants). Sizes move with releases; check the model
card and pick the largest quant your *disk* (not RAM) comfortably holds.

**Important:** giants must go through `llama-server` or colibri — **not
Ollama**, which disables mmap on CPU loads and would try (and fail) to pull
the whole file into RAM.

## Path B — colibri (from your integration list)

[colibri](https://github.com/JustVugg/colibri) is purpose-built for this
exact trick: a tiny C engine that keeps per-token layers in RAM, streams
routed experts from disk, and holds the hottest experts on the GPU. It
serves an OpenAI-compatible API, so it's two settings away:

```bash
bin/aria settings set backends.colibri http://localhost:8338
bin/aria settings set deep colibri/<model-name>
```

Its `docs/AI_SETUP.md` is written for an AI to follow — paste it into a
Claude Code session and let it do the install. Worth benchmarking against
Path A; whichever gives you more tok/s wins the `deep` slot.

## Using a giant well

A 1 tok/s model is not a chat partner — it's an **overnight deep thinker**,
which is exactly what A.R.I.A.'s tier system is for:

- Keep `fast`/`smart` on GPU-speed models (qwen3:30b) for triage, chat,
  and agents.
- Set `deep` to the giant. Give an agent `model: deep` in its frontmatter
  (e.g. a `strategist.md`) and hand it the hard questions before bed:
  "ask the strategist to stress-test the meal-planner business model".
- Engine timeouts already allow it (an hour of silence before the first
  token is tolerated), and the hourly auto-pipeline keeps using fast/smart
  so the system stays responsive while the giant grinds.

## Honest expectations

- **Prompt processing is the painful part** on CPU/disk: a long prompt can
  take minutes before any output. Keep deep-tier prompts lean.
- Quality at 1.58–2 bits is reduced vs the full model — a 671B at 2-bit
  still generally beats a 30B at 4-bit on hard reasoning, but verify on
  your tasks; sometimes qwen3:30b at full speed wins on usefulness/hour.
- These presets are informed starting points, not lab-verified on your
  exact box — expect to tune `--ctx-size` and watch `free -h` on first
  runs. If the system swaps hard (not just mmap reads), drop to a smaller
  quant.
- A giant and an everyday Ollama model do not fit in 16GB of VRAM
  together, so they take turns (`giants_free_memory`): Ollama unloads
  before a giant loads, and a giant that has finished its answer is
  unloaded before the next everyday call. The price is a reload at each
  hand-over — seconds for the everyday model, longer for the giant.
