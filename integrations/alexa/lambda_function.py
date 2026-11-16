"""Alexa skill backend for A.R.I.A. — paste this into the skill's Lambda.

Alexa turns speech into text and hands it to this function as an intent
plus one free-text slot. All this does is rebuild the sentence you said
and pass it, with a shared secret, to the A.R.I.A. voice gateway on your
own machine (bin/aria-voice-gateway, reached through your tunnel). The
gateway's reply is what Alexa speaks.

Fill in the two values below — or set them as environment variables when
you host the Lambda in your own AWS account. No libraries needed.
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
        "project. Status of a project. Search for something. Or ask what "
        "I'm working on.")


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


def lambda_handler(event, context):
    app = (event.get("session", {}).get("application", {})
           .get("applicationId", ""))
    if SKILL_ID and app != SKILL_ID:
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
        return speak(ask_aria(FIXED[name]))
    if name in PREFIX:
        said = (intent.get("slots", {}).get("text", {}).get("value") or "").strip()
        if not said:
            return speak("I didn't catch the rest of that. " + HELP)
        return speak(ask_aria(PREFIX[name] + said))
    return speak("I didn't follow that. " + HELP)


handler = lambda_handler      # for Lambdas configured with the older name
