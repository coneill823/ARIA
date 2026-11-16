# Alexa → A.R.I.A. (voice from anywhere in the house)

Alexa's speech-to-text can only leave Amazon through an **Alexa Skill**, so
the architecture keeps A.R.I.A. private by exposing exactly one thing: a
single n8n webhook route, tunneled privately. The chain:

```
"Alexa, ask aria to capture …"
   └─ Alexa STT (Amazon cloud — inherent to Alexa)
        └─ your Alexa Skill → AWS Lambda (10 lines)
             └─ HTTPS → tunnel → n8n webhook  (secret header checked)
                  └─ http://127.0.0.1:8700/api/say   (A.R.I.A., local)
                       └─ reply text → Lambda → Alexa speaks it
```

A.R.I.A. itself stays bound to 127.0.0.1; nothing else is reachable.

## 0. What's already built for this

`POST /api/say` with `{"text": "..."}` returns `{"reply": "..."}` — it
executes real commands (capture, run triage, trigger webhooks, missions,
autopilot start/stop; long runs detach to the background) and answers
anything else as fast-tier chat. Test it locally first:

```bash
curl -s -X POST http://127.0.0.1:8700/api/say \
  -d '{"text":"capture a smart bird feeder idea"}'
```

## 1. Tunnel ONE route to n8n (not to A.R.I.A.)

Use a private tunnel so the webhook URL is public but obscure+authed:
`cloudflared tunnel` (free) or Tailscale Funnel both work. Point it at
n8n's port only. In n8n create a workflow:

1. **Webhook node** — POST, path `alexa-aria`. 
2. **IF node** — header `x-aria-token` equals a long random secret, else
   respond 403.
3. **HTTP Request node** — POST `http://127.0.0.1:8700/api/say` with JSON
   `{"text": "{{ $json.body.text }}"}`.
4. **Respond to Webhook node** — body `{"reply": "{{ $json.reply }}"}`.

## 2. The Alexa Skill (developer.amazon.com → Create Skill → Custom)

Invocation name: `aria`. Interaction model (paste into the JSON editor):

```json
{"interactionModel": {"languageModel": {
  "invocationName": "aria",
  "intents": [
    {"name": "TalkIntent",
     "slots": [{"name": "text", "type": "AMAZON.SearchQuery"}],
     "samples": ["{text}", "to {text}", "that {text}"]},
    {"name": "AMAZON.StopIntent", "samples": []},
    {"name": "AMAZON.HelpIntent", "samples": []},
    {"name": "AMAZON.CancelIntent", "samples": []}
  ]}}}
```

## 3. The Lambda (Skill backend, "Alexa-hosted: Python" is easiest)

```python
import json, os, urllib.request

WEBHOOK = os.environ["ARIA_WEBHOOK"]        # your tunneled n8n URL
TOKEN = os.environ["ARIA_TOKEN"]            # the shared secret

def handler(event, context):
    rtype = event["request"]["type"]
    if rtype == "LaunchRequest":
        return speak("Online. What shall we do?", end=False)
    if rtype != "IntentRequest":
        return speak("Goodbye.")
    intent = event["request"]["intent"]
    if intent["name"] != "TalkIntent":
        return speak("Goodbye.")
    text = intent["slots"]["text"].get("value", "")
    try:
        req = urllib.request.Request(
            WEBHOOK, data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json",
                     "x-aria-token": TOKEN})
        with urllib.request.urlopen(req, timeout=6) as r:
            reply = json.load(r).get("reply", "Done.")
    except Exception:
        reply = ("That's taking longer than Alexa allows — "
                 "check the A.R.I.A. app for the answer.")
    return speak(reply, end=False)

def speak(text, end=True):
    return {"version": "1.0", "response": {
        "outputSpeech": {"type": "PlainText", "text": text[:7900]},
        "shouldEndSession": end}}
```

Set `ARIA_WEBHOOK` and `ARIA_TOKEN` as Lambda environment variables, set
the skill's endpoint to this Lambda, build, and enable testing in
"Development" — the skill works on every Echo on your account without
publishing.

## 4. Use it

- "Alexa, ask aria to **capture** a solar powered bird bath heater"
- "Alexa, ask aria to **run triage**"
- "Alexa, ask aria to **trigger deploy-site**"
- "Alexa, ask aria **what's waiting on me**"

## Honest constraints

- **8 seconds.** Alexa kills responses slower than ~8s. Commands are fine
  (they detach to the background); open-ended chat depends on your fast
  tier — qwen3:30b sometimes exceeds it, llama3.1:8b rarely does. The
  Lambda's timeout fallback handles the misses gracefully.
- **Amazon hears the request.** Alexa's STT is cloud-based by nature;
  everything after the webhook is local. For fully-private voice, the
  app's mic (faster-whisper) remains the pure path.
- Keep the token long and random; rotate it if the tunnel URL ever leaks.
