# Alexa → A.R.I.A. (voice from anywhere in the house)

Alexa turns what you say into text. A small Alexa Skill hands that text to
A.R.I.A. on your own machine and speaks her reply — so everything she does
from the chat box, she does from an Echo.

```
"Alexa, ask my aria to work on the meal planner"
   └─ Alexa (Amazon's cloud): speech → text, picks the intent
        └─ your skill's Lambda          integrations/alexa/lambda_function.py
             └─ HTTPS + shared secret → your tunnel
                  └─ aria-voice-gateway   127.0.0.1:8701   ← the ONLY thing exposed
                       └─ A.R.I.A.        127.0.0.1:8700/api/say
                            └─ reply text → Lambda → Alexa speaks it
```

A.R.I.A.'s own server has no login and stays on 127.0.0.1. The gateway is a
separate, tiny process that answers exactly one request — `POST /say` with
the right secret — and nothing else.

## What you can say

| You say (after "Alexa, ask my aria …") | What happens |
|---|---|
| "…to **capture** a solar powered bird bath heater" | lands in the inbox; the pipeline plans it |
| "…**what are you working on**" / "…**status**" | where every running project stands |
| "…**status of** the meal planner" | that project: iteration, tasks done, current task |
| "…to **work on** the meal planner" | starts autopilot (the plan must already be approved) |
| "…to **stop working on** the meal planner" | finishes the step in flight, writes the report |
| "…**what changed in** the meal planner" | the latest verified changes |
| "…to **search for** exif date parsing in python" | a web search, answered from what it finds |
| "…**note to brain** the feeder needs a solar panel" | a note in your Obsidian vault |
| "…to **run triage**" / "…to **develop the plans**" | pipeline stages, in the background |
| "…**what** / **how** / **why** / **is** …" | plain conversation |

Three things she will not take by voice, even with the right secret:
**approving a plan**, **answering a task that waits on your decision**,
and **pushing to GitHub**. Those decisions are yours; a voice in the room
should not be able to make them.

## 1. Start the gateway

```bash
bin/aria voice-gateway          # listens on 127.0.0.1:8701
```

```bash
bin/aria voice-gateway token    # the shared secret — you paste it into the Lambda
```

The secret is created on first run in `system/voice-token` (readable only
by you, ignored by git). To keep the gateway running, add a user service
next to `aria.service`:

```ini
# ~/.config/systemd/user/aria-voice.service
[Unit]
Description=A.R.I.A. voice gateway
After=aria.service

[Service]
ExecStart=/usr/bin/env python3 %h/ARIA/bin/aria-voice-gateway 8701
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now aria-voice.service
```

(Adjust the path if your clone is not in `~/ARIA`.) Check it locally before
involving Amazon:

```bash
curl -s -X POST http://127.0.0.1:8701/say \
  -H "X-Aria-Token: $(bin/aria voice-gateway token)" \
  -d '{"text":"what are you working on"}'
```

## 2. Give the gateway a public address

Amazon's servers have to reach it, so it needs an HTTPS address. Point the
tunnel at **8701 — never at 8700**.

**Tailscale Funnel** gives a stable address with no domain of your own:

```bash
tailscale funnel 8701
```

It prints a URL like `https://your-machine.your-tailnet.ts.net`; your
gateway is that plus `/say`. (Funnel has to be enabled for your tailnet
once; `tailscale funnel --help` shows the current options.)

**Cloudflare Tunnel** works too. For a first try:

```bash
cloudflared tunnel --url http://127.0.0.1:8701
```

That address changes every time it starts; for a permanent one use a named
tunnel on a domain you own.

## 3. Create the skill

At <https://developer.amazon.com/alexa/console/ask>: **Create Skill** →
name it anything → model **Custom** → hosting **Alexa-hosted (Python)** →
start from scratch.

Then **Interaction Model → JSON Editor**: replace everything with
[`integrations/alexa/interaction-model.json`](../integrations/alexa/interaction-model.json),
**Save**, **Build**.

The invocation name in that file is `my aria` ("Alexa, ask my aria …").
Amazon wants two words unless the name is your own brand; if your account
accepts plain `aria`, change `invocationName` and rebuild.

Why the model has so many small intents: Alexa only passes free-form
speech through a slot that must follow a fixed phrase, and it hands the
Lambda the slot *without* that phrase. So "capture …", "work on …",
"what …", "how …" are separate intents, and the Lambda puts the words back.

## 4. The Lambda

In the skill's **Code** tab, replace the contents of `lambda_function.py`
with [`integrations/alexa/lambda_function.py`](../integrations/alexa/lambda_function.py)
and fill in the two values at the top:

```python
GATEWAY = "https://your-machine.your-tailnet.ts.net/say"
TOKEN = "…the output of: bin/aria voice-gateway token…"
```

**Deploy**. (Hosting the Lambda in your own AWS account instead? Leave the
file as it is and set `ARIA_GATEWAY_URL`, `ARIA_TOKEN` and — to reject any
other skill — `ARIA_SKILL_ID` as environment variables.)

## 5. Use it

In the **Test** tab set testing to **Development** and type or say:

> ask my aria what are you working on

A skill in development works on every Echo signed in to your Amazon
account — there is nothing to publish.

## Not only Alexa

The gateway does not know what Alexa is: anything that can send one HTTPS
request can talk to A.R.I.A. — a phone shortcut, Home Assistant, a script.

```bash
curl -s -X POST https://your-machine.your-tailnet.ts.net/say \
  -H "X-Aria-Token: <secret>" -d '{"text":"capture an idea for later"}'
```

Prefer n8n as the door? Point the tunnel at an n8n webhook instead, check
a secret header there, and have it POST `{"text": …}` to
`http://127.0.0.1:8700/api/say` — the Lambda does not care which answers.

## Honest constraints

- **Eight seconds.** Alexa gives up on slow replies. Commands return at
  once (long work detaches into the background). Conversation and web
  searches depend on your fast model; when they overrun, she says the
  answer will be in the app rather than failing.
- **Amazon hears the request.** Alexa's speech recognition is Amazon's
  cloud; everything from the gateway inward is yours. For voice that never
  leaves the house, use the app's microphone (local whisper).
- **The secret is the lock.** Five wrong secrets lock the door for a
  minute, and there is a limit of thirty requests a minute. If the address
  or secret ever leaks: delete `system/voice-token`, restart the gateway,
  paste the new secret into the Lambda.
- **What is tested.** `bin/aria test` drives the gateway and the Lambda
  with simulated Alexa requests. The console steps above follow Amazon's
  documented flow; they rename their screens now and then.
