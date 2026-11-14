# Upgrade checklist — from 8B to 1T

Each stage is independent; stop at whichever rung suits you. Verify lines
tell you what success looks like before moving on.

## Stage 0 — sync (2 min)
- [ ] `cd ~/ARIA && git pull origin main`
- [ ] `bin/aria test` → expect 21+ PASS incl. `gpu acceleration`

## Stage 1 — smart tier: qwen3:30b (~19GB download)
- [ ] `ollama pull qwen3:30b`
- [ ] App ⚙ Settings → **smart:** `ollama/qwen3:30b` → Save
- [ ] `bin/aria test` → `develop — live model` should PASS on the new model
- [ ] If chat quality matters more than snappiness: also set
      **fast:** `ollama/qwen3:30b` (MoE, only 3B active — still quick);
      revert to `ollama/llama3.1:8b` if it feels sluggish

## Stage 2 — deep tier: gpt-oss:120b (~65GB download, fits RAM+VRAM)
- [ ] `ollama pull gpt-oss:120b`
- [ ] ⚙ Settings → **deep:** `ollama/gpt-oss:120b` → Save
- [ ] In chat: `ask the strategist to find the fatal flaw in my current
      favourite project idea` → the strategist agent runs on deep
- [ ] Watch `free -h` during the run; if the system swaps hard, keep deep
      for overnight use only

## Stage 3 (optional) — Vulkan speed path
- [ ] `bin/aria llama install`
- [ ] Download a smart-tier GGUF (see bin/aria-llama header), then
      `bin/aria llama serve smart` and set **smart:** `llamacpp/qwen3-30b`
- [ ] Only keep it if tok/s beats Ollama on your card

## Stage 4 — the giant: DeepSeek 671B, then Kimi K2 1T (NVMe-streamed)
- [ ] Check disk: `df -h ~` (need ~200GB free for DeepSeek, ~300GB for K2)
- [ ] `bin/aria llama install` (if not done in stage 3)
- [ ] On HuggingFace open `unsloth/DeepSeek-R1-GGUF` (or current V3.x),
      pick the **UD-TQ1_0** folder, and for EACH part:
      `bin/aria llama get <part-url>`   (resumable; rerun if interrupted)
- [ ] `bin/aria llama serve giant vendor/models/<first-part-00001-of-N>.gguf`
      → wait for "server is listening"; first load reads the whole file once
- [ ] ⚙ Settings → **deep:** `llamacpp/giant` → Save
- [ ] Overnight job: `ask the strategist to <hard question>` — expect
      ~0.5–2 tok/s; minutes of silence before the first word is normal
- [ ] Happy? Repeat the downloads with `unsloth/Kimi-K2-Instruct-GGUF`
      (1T params, ~250GB+) and serve that instead
- [ ] Keep it permanent: `bin/aria llama service giant`
- [ ] NEVER run giants through Ollama (it disables mmap → OOM); details in
      docs/BIG-MODELS.md

## Stage 5 — finishing touches
- [ ] `pip install faster-whisper` → voice input in Firefox (last SKIP)
- [ ] ⚙ Settings → Obsidian vault path → "→ Obsidian" export goes live
- [ ] n8n flows under `aria.webhooks` in system/config.yml → "trigger
      <name>" in chat does real things
- [ ] `bin/aria test` one more time — everything you enabled gets verified
