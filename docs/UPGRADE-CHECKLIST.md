# Upgrade checklist — from 8B to 1T

Each stage is independent; stop at whichever rung suits you. Verify lines
tell you what success looks like before moving on.

## Stage 0 — sync (2 min)
- [ ] `cd ~/ARIA && bin/aria update` — the newest version of the branch
      you are on, your settings kept (hand edits to `system/config.yml`
      are moved into `system/config.local.yml`, which updates never
      touch), the server restarted unless autopilot is mid-step.
      No `aria update` yet? Then once, by hand:
      `cp system/config.yml ~/aria-config-backup.yml`,
      `git checkout -- system/config.yml`, `git pull`,
      `bin/aria settings import ~/aria-config-backup.yml`,
      `systemctl --user restart aria`
- [ ] The bottom of the sidebar shows the version that is running
      (`version <commit> · <date>`); **⟳ update ready — restart** there
      means the code on disk is newer than the running server
- [ ] `bin/aria test --mock` → every check PASS or SKIP, in seconds
- [ ] `bin/aria test` → the same against your models. If the only FAIL is
      `backends reachable`, Ollama is not running:
      `sudo systemctl enable --now ollama`
- [ ] `bin/aria doctor` → one line per thing that is switched on, missing
      or optional

## Stage 0.5 — the three small installs that unlock the most
- [ ] `sudo pacman -S bubblewrap` → she runs each project's tests in a
      sandbox after every change, and a change that breaks them is rolled
      back (without it nothing is executed, and reports say so)
- [ ] `sudo pacman -S python-pyqt6-webengine` → `bin/aria app` opens her
      in a window of her own instead of a browser
- [ ] `sudo pacman -S python-pytest libnotify` → full test support for
      Python projects; desktop notifications when she needs you
- [ ] `bin/aria voice install` → her voice and her ears, on this machine
      (a private environment under `vendor/voice/`, about 700 MB; ends
      with her saying a sentence and understanding it again). The
      desktop window has no speech of its own, so this is what makes 🎙
      and 🔊 work there. `bin/aria voice test` plays it through your
      speakers

## Stage 1 — smart tier: qwen3:30b (~19GB download)
- [ ] `ollama pull qwen3:30b`
- [ ] App ⚙ Settings → **smart:** `ollama/qwen3:30b` → Save
- [ ] `bin/aria test` → `develop — live model` should PASS on the new model
- [ ] If chat quality matters more than snappiness: also set
      **fast:** `ollama/qwen3:30b` (MoE, only 3B active — still quick);
      revert to `ollama/llama3.1:8b` if it feels sluggish
- [ ] qwen3 reasons before it answers. On the default setting
      (`thinking: auto`) chat and triage skip the reasoning and stay
      quick, and plans and reviews reason when the window has room. To
      let her think on the big calls too, give her a larger window —
      ⚙ Settings → Models → Context window `16384` — if `ollama ps`
      still shows most of the model on the GPU afterwards

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

## Stage 4 — the giants: a general model, a researcher, a coder
- [ ] Check disk: `df -h ~` (~170GB DeepSeek-R1, ~250GB Kimi K2, ~180GB
      Qwen3-Coder)
- [ ] `bin/aria llama install` (if not done in stage 3)
- [ ] Download every part of each model (`bin/aria llama get <part-url>`,
      resumable — or any downloader, any folder)
- [ ] `bin/aria models scan` → lists what it found, flags a model that is
      still downloading, and prints one `bin/aria models use …` line per
      model. Run those (or: ⚙ Settings → Giant models → Scan → Use)
- [ ] `bin/aria models` → each role shows its model and "ready to load".
      "cannot start" means `llama-server` is missing: `bin/aria llama
      install`
- [ ] **Prove each one:** `bin/aria models test general` (then `research`,
      `coder`) → loads it, asks it a question, prints the load time, the
      answer time and the answer — or the server's own error. First
      load: minutes. The **Test** button in Settings does the same
- [ ] That is all. She starts the right one when a task needs it, asks
      Ollama to free its memory and unloads the other giants first (one
      in memory at a time), and skips one that is still downloading.
      Watch it happen: the sidebar says which giant is loaded or loading,
      the task's card shows the giant and why; logs are in
      `system/garage/<role>.log`
- [ ] Use one by hand: in chat `ask the strategist to <hard question>`
      with `model: general` in `system/agents/strategist.md`
- [ ] If you made the old per-model services, stop them — the roles
      replace them: `systemctl --user disable --now aria-llama-deepseek
      aria-llama-kimi aria-llama-coder`
- [ ] Too slow for everyday tasks? That is expected (~0.5–2 words a
      second). `giants_use: auto` gives a giant the tasks the planner
      marks as hard and the last attempt at anything sent back twice;
      `always` sends them everything; `never` keeps them for agents you
      pin yourself
- [ ] NEVER run giants through Ollama (it disables mmap → OOM); details in
      docs/BIG-MODELS.md and docs/MODEL-GARAGE.md

## Stage 5 — finishing touches
- [ ] Voice not installed in stage 0.5? `bin/aria voice install` (the
      last SKIP in the self-test). `sudo pacman -S espeak-ng` alone gives
      her a plain voice with no download
- [ ] Talk to her from an Echo: `bin/aria alexa` says what is set up and
      what is left — the steps are in docs/ALEXA.md
- [ ] ⚙ Settings → Obsidian vault path → "→ Obsidian" export goes live
- [ ] n8n flows: `bin/aria settings set webhooks.<name> <url>` → "trigger
      <name>" in chat does real things
- [ ] `bin/aria test` one more time — everything you enabled gets verified
