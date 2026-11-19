# Alexa → A.R.I.A. (voice from anywhere in the house)

Alexa turns what you say into text. A small Alexa Skill hands that text to
A.R.I.A. on your own machine and speaks her reply — so you can ask an Echo
how a project is getting on, what needs you, or tell her to capture an
idea, without opening the app.

```
"Alexa, ask my aria how far along is the meal planner"
   └─ Alexa (Amazon's cloud): speech → text, picks the intent
        └─ HTTPS → your tunnel
             └─ aria-voice-gateway   127.0.0.1:8701   ← the ONLY thing exposed
                  └─ A.R.I.A.        127.0.0.1:8700/api/say
                       └─ reply text → Alexa speaks it
```

A.R.I.A.'s own server has no login and stays on 127.0.0.1. The gateway is a
separate, tiny process that answers two requests — an Alexa skill request
at a secret address, and `POST /say` with a secret header — and nothing
else.

**`bin/aria alexa` shows where you are**: what is set up, what is left,
and the exact address to give Amazon. Run it whenever you are unsure.

## What you can say

| You say (after "Alexa, ask my aria …") | What happens |
|---|---|
| "…**what are you working on**" / "…**status**" | where every running project stands |
| "…**how far along is** the meal planner" | milestones reached, what is in progress, what is still ahead, roughly how much work is left |
| "…**what's left on** the meal planner" | the same, from the roadmap |
| "…**what needs me**" | plans awaiting approval, tasks awaiting your decision, runs that paused themselves |
| "…**status of** the meal planner" | that project: iteration, tasks done, current task |
| "…**what changed in** the meal planner" | the latest verified changes |
| "…to **capture** a solar powered bird bath heater" | lands in the inbox; the pipeline plans it |
| "…to **work on** the meal planner" | starts autopilot (the plan must already be approved) |
| "…to **stop working on** the meal planner" | finishes the step in flight, writes the report |
| "…to **search for** exif date parsing in python" | a web search, answered from what it finds |
| "…**note to brain** the feeder needs a solar panel" | a note in your Obsidian vault |
| "…to **run triage**" / "…to **develop the plans**" | pipeline stages, in the background |
| "…**what** / **how** / **why** / **is** …" | plain conversation |

Questions about progress are answered from her records, not by a model —
so they come back at once even while every model is busy with a task.

Three things she will not take by voice, even from your own skill:
**approving a plan**, **answering a task that waits on your decision**,
and **pushing to GitHub**. Those decisions are yours; a voice in the room
should not be able to make them.

## Setting it up

Four steps. The first two are on your machine, the third is in Amazon's
console, the fourth takes ten seconds.

### 1. Start the door

```bash
bin/aria alexa service
```

That installs and starts `aria-voice.service` (a systemd user service, next
to `aria.service`), so the gateway runs from login on `127.0.0.1:8701`.
Without systemd, run `bin/aria voice-gateway` in a terminal instead.

### 2. Give it an address Amazon can reach

Amazon's servers have to reach the gateway, so it needs an HTTPS address.
Point the tunnel at **8701 — never at 8700**.

**Tailscale Funnel** keeps the same address for good and needs no domain
of your own:

```bash
sudo pacman -S tailscale
```

```bash
sudo systemctl enable --now tailscaled
```

```bash
sudo tailscale up
```

```bash
tailscale funnel --bg 8701
```

It prints an address like `https://your-machine.your-tailnet.ts.net`.
(Funnel has to be switched on for your tailnet once; the command tells
you where if it is not.)

**Cloudflare** works for a first try — `cloudflared tunnel --url
http://127.0.0.1:8701` — but that address changes every time it starts,
and you would have to give Amazon the new one each time. **ngrok** with
its free static domain is the third option.

Then tell her the address:

```bash
bin/aria alexa url https://your-machine.your-tailnet.ts.net
```

She prints the full address to give Amazon — it ends in a long secret, so
treat it like a password.

### 3. Create the skill

At <https://developer.amazon.com/alexa/console/ask>:

1. **Create Skill** → any name → model **Custom** → hosting **Provision
   your own** → start from scratch.
2. **Build → Interaction Model → JSON Editor**: replace everything with
   [`integrations/alexa/interaction-model.json`](../integrations/alexa/interaction-model.json),
   **Save**, then **Build skill**.
3. **Build → Endpoint → HTTPS → Default Region**: paste the address from
   step 2 (`https://…/alexa/<secret>`). For the certificate type choose
   *"My development endpoint has a certificate from a trusted certificate
   authority"* (Tailscale, ngrok) — or, for a `trycloudflare.com` address,
   *"…is a sub-domain of a domain that has a wildcard certificate…"*.
   **Save endpoints**.
4. **Test** tab → skill testing in **Development**.

The invocation name in that file is `my aria` ("Alexa, ask my aria …").
Amazon wants two words unless the name is your own brand; if your account
accepts plain `aria`, change `invocationName` and rebuild.

A skill in development works on every Echo signed in to your Amazon
account — there is nothing to publish.

### 4. Lock it to your skill, and try it

The skill's id is on its page in the console (`amzn1.ask.skill.…`):

```bash
bin/aria alexa skill amzn1.ask.skill.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

From then on the gateway answers that skill only. Now try your side
without Amazon — she plays Amazon's part herself, first on this machine,
then through your public address:

```bash
bin/aria alexa test "how far along are you"
```

If both lines answer, everything from the internet to her works; say it
to an Echo: *"Alexa, ask my aria what needs me."*

## What protects it

- **The address is the lock.** The gateway answers an Alexa request only
  at `/alexa/<secret>`, where the secret is 32 random bytes kept in
  `system/voice-token` (readable only by you, ignored by git). Five wrong
  secrets lock the door for a minute; thirty requests a minute is the
  limit.
- **Only fresh requests.** Amazon stamps every request with the time; one
  older than two and a half minutes is refused, so a recorded request
  cannot be replayed later.
- **Only your skill**, once you have run `bin/aria alexa skill …`.
- **The gate stays shut.** Approving, deciding and pushing are refused by
  voice whoever asks.
- **What is not checked:** Amazon's cryptographic signature on each
  request. Verifying it needs certificate-chain validation that Python's
  standard library does not offer, and A.R.I.A. installs nothing else.
  For a private skill that never leaves development, the secret address
  does that job. If the address ever leaks: delete `system/voice-token`,
  restart the gateway (`systemctl --user restart aria-voice`), run
  `bin/aria alexa` and paste the new address into the console.

## The other way in: a Lambda

If you would rather not point Amazon at your machine directly — or you
publish the skill, where Amazon's signature check matters — put Amazon's
own Lambda in between. Choose hosting **Alexa-hosted (Python)** instead of
*Provision your own*, and in the skill's **Code** tab replace
`lambda_function.py` with
[`integrations/alexa/lambda_function.py`](../integrations/alexa/lambda_function.py),
filling in the two values at the top:

```python
GATEWAY = "https://your-machine.your-tailnet.ts.net/say"
TOKEN = "…the output of: bin/aria voice-gateway token…"
```

**Deploy.** The Lambda rebuilds what you said and calls the gateway's
`/say` with the secret in a header. It is the same skill logic the gateway
runs itself in the direct setup.

## Not only Alexa

`POST /say` does not know what Alexa is: anything that can send one HTTPS
request can talk to A.R.I.A. — a phone shortcut, Home Assistant, a script.

```bash
curl -s -X POST https://your-machine.your-tailnet.ts.net/say \
  -H "X-Aria-Token: $(bin/aria voice-gateway token)" \
  -d '{"text":"what needs me"}'
```

## Honest constraints

- **Eight seconds.** Alexa gives up on slow replies. Commands and
  progress questions return at once. Conversation depends on your fast
  model: when it cannot answer in time — autopilot has the models — she
  says so, and where things stand, instead of leaving the line silent.
- **She answers; she does not announce.** Alexa speaks when spoken to.
  Having an Echo say "the meal planner is finished" on its own needs
  Amazon's proactive-events permissions and is not built. The desktop
  notification and the `mission-done` webhook cover that today.
- **Amazon hears the request.** Alexa's speech recognition is Amazon's
  cloud; everything from the gateway inward is yours. For voice that never
  leaves the house, use the app's microphone — it is recognised on this
  machine (`bin/aria voice install`).
- **What is tested.** `bin/aria test` drives the gateway with simulated
  Alexa requests — the direct route and the Lambda — including a stale
  request, a wrong secret and a foreign skill. No real Echo and no real
  tunnel were involved in building this; the console steps follow
  Amazon's documented flow, and they rename their screens now and then.
