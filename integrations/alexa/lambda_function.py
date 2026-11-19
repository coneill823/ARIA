"""Alexa skill logic for A.R.I.A.

Alexa turns speech into text and hands it over as an intent plus one
free-text slot. All this does is rebuild the sentence you said, pass it to
A.R.I.A., and wrap her reply in the shape Alexa speaks.

It is used in two ways:

  * by A.R.I.A.'s own voice gateway (bin/aria-voice-gateway), when the
    skill's endpoint is pointed straight at your machine — the simple
    setup: no Lambda, nothing to paste. See docs/ALEXA.md.
  * as an AWS Lambda (or an Alexa-hosted skill's code): paste this file in
    and fill in the two values below — or set them as environment
    variables. The Lambda then calls the gateway's /say with the shared
    secret. No libraries needed.
"""

import json
import os
import urllib.error
import urllib.request

GATEWAY = os.environ.get("ARIA_GATEWAY_URL", "https://YOUR-TUNNEL-HOST/say")
TOKEN = os.environ.get("ARIA_TOKEN", "PASTE-THE-OUTPUT-OF-aria-voice-gateway-token")
SKILL_ID = os.environ.get("ARIA_SKILL_ID", "")   # optional: amzn1.ask.skill.…

# Alexa's free-text slot must follow a fixed carrier phrase, and it only
# hands back the slot — so each intent knows the words to put back in front.
PREFIX = {
    "CaptureIntent": "capture ",
    "WorkIntent": "work on ",
    "StopWorkIntent": "stop working on ",
    "StatusIntent": "status of ",
    "ChangesIntent": "what changed in ",
    "SearchIntent": "search for ",
    "BrainIntent": "note to brain: ",
    "SayIntent": "",
    "AskWhatIntent": "what ", "AskWhatsIntent": "what's ", "AskHowIntent": "how ",
    "AskWhyIntent": "why ", "AskWhenIntent": "when ", "AskWhereIntent": "where ",
    "AskWhoIntent": "who ", "AskWhichIntent": "which ", "AskIsIntent": "is ",
    "AskAreIntent": "are ", "AskCanIntent": "can ", "AskCouldIntent": "could ",
    "AskDoIntent": "do ", "AskDoesIntent": "does ", "AskDidIntent": "did ",
    "AskShouldIntent": "should ", "AskWillIntent": "will ",
}
FIXED = {
    "StatusAllIntent": "what are you working on",
    "TriageIntent": "run triage",
    "DevelopIntent": "develop the plans",
}
HELP = ("You can say: capture, followed by an idea. Work on, followed by a "
        "project. Status of a project. How far along is a project. What "
        "needs me. Search for something. Or ask what I'm working on.")


def speak(text, end=False):
    text = " ".join(str(text).split())[:6000] or "Done."
    response = {"outputSpeech": {"type": "PlainText", "text": text},
                "shouldEndSession": end}
    if not end:
        response["reprompt"] = {"outputSpeech": {
            "type": "PlainText", "text": "Anything else?"}}
    return {"version": "1.0", "response": response}


def ask_aria(text):
    request = urllib.request.Request(
        GATEWAY, data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json", "X-Aria-Token": TOKEN})
    try:
        with urllib.request.urlopen(request, timeout=7) as response:
            return json.load(response).get("reply") or "Done."
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "The gateway rejected my token. Check it in the Lambda."
        if exc.code == 429:
            return "The gateway is asking me to slow down. Try again shortly."
        return "The gateway answered with an error."
    except Exception:
        return ("I couldn't reach A.R.I.A. — check that the gateway and the "
                "tunnel are running.")


def skill_id_of(event):
    """The skill a request claims to come from."""
    return ((event.get("session") or {}).get("application") or {}).get(
        "applicationId") or (((event.get("context") or {}).get("System")
                              or {}).get("application") or {}).get(
        "applicationId") or ""


def handle(event, ask, skill_id=""):
    """One Alexa request in, one Alexa response out. `ask(text)` is how
    the sentence reaches A.R.I.A. and returns her reply."""
    if skill_id and skill_id_of(event) != skill_id:
        raise ValueError("request from an unexpected skill")
    request = event.get("request", {})
    kind = request.get("type")
    if kind == "LaunchRequest":
        return speak("A.R.I.A. online. What shall we do?")
    if kind == "SessionEndedRequest":
        return {"version": "1.0", "response": {}}
    if kind != "IntentRequest":
        return speak("Goodbye.", end=True)
    intent = request.get("intent", {})
    name = intent.get("name", "")
    if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent",
                "AMAZON.NavigateHomeIntent"):
        return speak("Standing by.", end=True)
    if name == "AMAZON.HelpIntent":
        return speak(HELP)
    if name in FIXED:
        return speak(ask(FIXED[name]))
    if name in PREFIX:
        said = (intent.get("slots", {}).get("text", {}).get("value") or "").strip()
        if not said:
            return speak("I didn't catch the rest of that. " + HELP)
        return speak(ask(PREFIX[name] + said))
    return speak("I didn't follow that. " + HELP)


def lambda_handler(event, context):
    return handle(event, ask_aria, SKILL_ID)


handler = lambda_handler      # for Lambdas configured with the older name
