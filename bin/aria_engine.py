"""A.R.I.A. engine library — the local pipeline as importable functions that
report progress through an `emit(event)` callback.

Used by bin/aria-ollama (CLI, prints event logs) and bin/aria-ui (web UI,
streams events to the browser so you can watch the model think).

Events are dicts with an "event" key; most carry a human-readable "log":
  start / note / prompt / thinking / token / decision / question /
  moved / file_written / error / stage_done

This module is the hub: config, the model client, the pipeline stages,
chat, skills, agents, missions and the project registry. Her reach lives
in sibling modules that know nothing about the engine — aria_web (internet),
aria_brain (Obsidian), aria_git (git/GitHub) — and the autonomous project
loop lives in aria_autopilot, which builds on everything here.
"""

import collections
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import aria_brain
import aria_garage
import aria_git
import aria_run
import aria_voice
import aria_web

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "pipeline" / "00-inbox"
PROCESSING = ROOT / "pipeline" / "01-processing"
POTENTIAL = ROOT / "pipeline" / "02-potential-projects"
KNOWLEDGE = ROOT / "pipeline" / "knowledge"
ARCHIVE = ROOT / "pipeline" / "archive"
LEDGER = ROOT / "pipeline" / "LEDGER.md"

CATEGORIES = ("project", "task", "note", "content", "someday", "unclear")
KNOWLEDGE_FOLDERS = {"note": "notes", "content": "content-ideas", "someday": "someday"}


# ---------------------------------------------------------------- config

CONFIG_FILE = ROOT / "system" / "config.yml"
# The owner's own settings. Whatever is set here wins over config.yml, and
# git never touches it — so pulling a new version cannot collide with them.
# The Settings page and `aria models use` write here.
LOCAL_FILE = ROOT / "system" / "config.local.yml"
LOCAL_HEADER = """# Your own settings. They override system/config.yml, and `git pull` never
# touches this file. The Settings page writes here; you can edit it too —
# same keys as config.yml, two spaces in. Delete a line to go back to the
# default.
aria:
"""


# headings whose children are plain settings; headings read as a whole
CONTAINERS = ("aria", "models", "ollama")
BLOCKS = ("backends", "backend_keys", "webhooks")


def _local_text():
    try:
        return LOCAL_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


def _scalars(text):
    """The plain `key: value` settings in a config text — top-level ones
    and those under `models:` — as {key: value}. A key that heads a block
    (backends, webhooks…) is not one."""
    out = {}
    lines = text.splitlines()
    inside = None                       # indent of the block being skipped
    for i, ln in enumerate(lines):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = re.match(r"^([ \t]*)([\w.-]+):[ \t]*(.*?)[ \t]*$", ln)
        if not m:
            continue
        indent, key = len(m.group(1)), m.group(2)
        value = m.group(3).split(" #")[0].strip()
        if value.startswith("#"):
            value = ""
        if inside is not None and indent > inside:
            continue                    # a child of a block
        inside = None
        if not value:
            nxt = next((x for x in lines[i + 1:] if x.strip()
                        and not x.lstrip().startswith("#")), "")
            deeper = len(nxt) - len(nxt.lstrip()) > indent
            if key in CONTAINERS:
                continue                # its children are plain settings
            if deeper:
                inside = indent         # a block heading: skip its children
                continue
        out.setdefault(key, value)
    return out


def _block(text, key):
    """The key: value pairs nested under a `<key>:` heading in a config
    text — {} for a heading with nothing under it, None when the text has
    no such heading."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(rf"^([ \t]*){re.escape(key)}:[ \t]*(?:#.*)?$", ln)
        if not m:
            continue
        indent, out = len(m.group(1)), {}
        for sub in lines[i + 1:]:
            if not sub.strip() or sub.lstrip().startswith("#"):
                continue
            sm = re.match(r"^([ \t]*)([\w.-]+):[ \t]*(\S.*?)[ \t]*$", sub)
            if not sm or len(sm.group(1)) <= indent:
                break
            out[sm.group(2)] = sm.group(3).split(" #")[0].strip()
        return out
    return None


def config():
    base = CONFIG_FILE.read_text(encoding="utf-8")
    local_text = _local_text()
    mine = _scalars(local_text) if local_text else {}

    def find(key, default):
        if key in mine:                 # set by the owner — also to nothing
            return mine[key] or default
        # [ \t]* (not \s*) so an empty value can't swallow the newline and
        # match the next line as the value
        m = re.search(rf"^[ \t]*{re.escape(key)}:[ \t]*(\S.*?)[ \t]*$", base,
                      re.M)
        return m.group(1) if m else default

    def block(key):
        """key: value pairs nested under a `<key>:` heading — the owner's
        own block if they wrote one (an empty one means none), else the
        default one."""
        own = _block(local_text, key)
        return own if own is not None else (_block(base, key) or {})

    def flag(key, default):
        return find(key, default).split(" #")[0].strip().lower() \
            in ("true", "yes", "on", "1")

    def number(key, default):
        try:
            return float(find(key, str(default)).split("#")[0].strip())
        except ValueError:
            return float(default)

    backends = {"ollama": "http://localhost:11434",
                "llamacpp": "http://localhost:8080"}
    backends.update(block("backends"))
    if os.environ.get("ARIA_OLLAMA_URL"):
        backends["ollama"] = os.environ["ARIA_OLLAMA_URL"]
    if os.environ.get("ARIA_LLAMACPP_URL"):
        backends["llamacpp"] = os.environ["ARIA_LLAMACPP_URL"]

    return {
        "url": os.environ.get(
            "ARIA_OLLAMA_URL", find("base_url", "http://localhost:11434")
        ).rstrip("/"),
        "model": os.environ.get("ARIA_OLLAMA_MODEL", find("model", "llama3.1:8b")),
        "threshold": float(find("triage_confidence_threshold", "0.7")),
        "promotion_target": find("promotion_target", "./active-projects"),
        "auto_pipeline": flag("auto_pipeline", "true"),
        # the business side of an idea: written with every plan, fitted to
        # where the owner is based
        "business_plans": flag("business_plans", "true"),
        "based_in": find("based_in", "").split(" #")[0].strip(),
        "obsidian_vault": os.environ.get(
            "ARIA_OBSIDIAN_VAULT", find("obsidian_vault", "")),
        "brain_journal": flag("brain_journal", "true"),
        "stt_model": find("stt_model", "base"),
        # her voice and her ears (aria_voice): where speech is made and
        # recognised, which voice, how fast
        "voice_engine": find("voice_engine", "auto").split(" #")[0].strip()
            .lower(),
        "voice_input": find("voice_input", "auto").split(" #")[0].strip()
            .lower(),
        "voice_name": find("voice_name", "").split(" #")[0].strip(),
        "voice_speed": number("voice_speed", 1.0),
        # Alexa (docs/ALEXA.md): where Amazon reaches the voice gateway,
        # and the one skill it answers
        "alexa_url": find("alexa_url", "").split(" #")[0].strip().rstrip("/"),
        "alexa_skill_id": find("alexa_skill_id", "").split(" #")[0].strip(),
        "tiers": {"fast": find("fast", ""), "smart": find("smart", ""),
                  "deep": find("deep", "")},
        "backend_urls": {k: v.rstrip("/") for k, v in backends.items()},
        "backend_keys": block("backend_keys"),
        "context_tokens": int(number("context_tokens", 8192)),
        # how a model that reasons before it answers (qwen3, deepseek-r1…)
        # is used on the everyday tiers: see thinking_for()
        "thinking": find("thinking", "auto").split(" #")[0].strip().lower(),
        # the everyday model server: started when she starts, the quick
        # model loaded, and kept loaded this long between uses
        "ollama_autostart": flag("ollama_autostart", "true"),
        "models_warm": flag("models_warm", "true"),
        # (set to nothing by the owner, it is nothing: Ollama decides)
        "ollama_keep_alive": (mine["ollama_keep_alive"]
                              if "ollama_keep_alive" in mine else
                              find("ollama_keep_alive", "30m"))
            .split(" #")[0].strip().lower(),
        "ollama_command": find("ollama_command", "").split(" #")[0].strip(),
        "webhooks": block("webhooks"),
        "heartbeat_url": find("heartbeat_url", ""),
        "web_access": flag("web_access", "true"),
        "web_search": find("web_search", "auto").split(" #")[0].strip().lower(),
        "web_search_url": os.environ.get(
            "ARIA_WEB_SEARCH_URL", find("web_search_url", "")),
        "web_allowlist": [h.strip().lower()
                          for h in find("web_allowlist", "").split(",")
                          if h.strip()],
        "github_access": flag("github_access", "false"),
        "github_base_branch": find("github_base_branch", "main"),
        "autopilot_git": flag("autopilot_git", "true"),
        "autopilot_after_goal": find("autopilot_after_goal", "improve")
            .split(" #")[0].strip().lower(),
        "autopilot_max_tasks": int(number("autopilot_max_tasks", 0)),
        "autopilot_max_iterations": int(number("autopilot_max_iterations", 0)),
        "autopilot_max_hours": number("autopilot_max_hours", 0),
        "autopilot_stall_limit": max(
            1, int(number("autopilot_stall_limit", 3))),
        "autopilot_idle_minutes": number("autopilot_idle_minutes", 30),
        # running a project's own tests after each change: sandbox (only
        # inside bubblewrap), direct (unsandboxed — your call), off
        "autopilot_checks": find("autopilot_checks", "sandbox")
            .split(" #")[0].strip().lower(),
        "autopilot_check_seconds": max(
            5, int(number("autopilot_check_seconds", 300))),
        "autopilot_refine": flag("autopilot_refine", "true"),
        # the giants: one role each, started and swapped by aria_garage
        "giants": {r: find(f"giant_{r}", "").split(" #")[0].strip()
                   for r in aria_garage.ROLES},
        "giants_use": find("giants_use", "auto").split(" #")[0].strip()
            .lower(),
        "giants_loaded": max(1, int(number("giants_loaded", 1))),
        "giants_idle_minutes": number("giants_idle_minutes", 20),
        "giants_port": int(number("giants_port", 8090)),
        "giants_ctx": int(number("giants_ctx", 8192)),
        "giants_free_memory": flag("giants_free_memory", "true"),
        "giants_flags": find("giants_flags", "").split(" #")[0].strip(),
        "editor": find("editor", "").split(" #")[0].strip(),
        "terminal": find("terminal", "").split(" #")[0].strip(),
        "desktop_notifications": flag("desktop_notifications", "true"),
    }


def _split_spec(spec):
    backend, _, model = (spec or "").partition("/")
    backend = backend.strip().lower()
    model = model.strip()
    if not model:  # bare model name means ollama
        backend, model = "ollama", backend
    return backend, model


def _kept_loaded(backend, model, cfg):
    """How long Ollama is asked to keep this model in memory after a call
    (None: Ollama decides). Only the everyday models — the fast and smart
    tiers — are kept: they are what a chat or the next task waits for. A
    deep-tier model the size of the machine's RAM is let go on Ollama's
    own, shorter clock."""
    if backend != "ollama":
        return None
    everyday = [_split_spec(cfg["tiers"].get(t)) for t in ("fast", "smart")
                if cfg.get("tiers", {}).get(t)]
    if everyday and (backend, model) not in everyday:
        return None
    return _keep_alive(cfg.get("ollama_keep_alive"))


def _parse_spec(spec, cfg):
    """Parse a 'backend/model' spec against configured backends; None if
    the backend name is unknown."""
    backend, model = _split_spec(spec)
    url = cfg["backend_urls"].get(backend)
    if url is None:
        return None
    kind = "ollama" if backend == "ollama" else "openai"
    key = (os.environ.get(f"ARIA_KEY_{backend.upper()}")
           or cfg["backend_keys"].get(backend) or "local")
    return {"kind": kind, "url": url, "model": model,
            "backend": backend, "key": key, "spec": f"{backend}/{model}",
            "num_ctx": cfg.get("context_tokens", 0),
            "keep_alive": _kept_loaded(backend, model, cfg)}


def _legacy_target(cfg, tier):
    return {"kind": "ollama", "url": cfg["url"], "model": cfg["model"],
            "backend": "ollama", "spec": f"ollama/{cfg['model']}",
            "num_ctx": cfg.get("context_tokens", 0), "tier": tier,
            "keep_alive": _keep_alive(cfg.get("ollama_keep_alive"))}


def resolve_tier(tier, cfg=None):
    """Turn a tier name (fast/smart/deep) into a callable backend target:
    {"kind": "ollama"|"openai", "url", "model", "spec", "tier"}. Unset
    tiers fall back deep -> smart -> fast -> legacy ollama keys."""
    cfg = cfg or config()
    order = {"deep": ("deep", "smart", "fast"),
             "smart": ("smart", "fast"),
             "fast": ("fast",)}.get(tier, ("fast",))
    spec = next((cfg["tiers"][t] for t in order if cfg["tiers"].get(t)), "")
    if not spec:  # legacy single-model config
        return _legacy_target(cfg, tier)
    if giant_role(spec):                 # a tier may name a giant's role
        target = _giant_target(giant_role(spec), cfg)
        target["tier"] = tier
        return target
    target = _parse_spec(spec, cfg)
    if target is None:  # unknown backend name — fail soft to legacy ollama
        return _legacy_target(cfg, tier)
    target["tier"] = tier
    return target


def resolve_model(spec, cfg=None, default="smart"):
    """A target from a tier name, a giant's role (general/research/coder)
    or a direct backend/model spec; None when the spec names a backend
    that is not configured."""
    cfg = cfg or config()
    spec = (spec or default).strip()
    if spec in ("fast", "smart", "deep"):
        return resolve_tier(spec, cfg)
    if giant_role(spec):
        return _giant_target(giant_role(spec), cfg)
    return _parse_spec(spec, cfg)


def giant_role(spec):
    """'coder' or 'giant/coder' -> 'coder'; anything else -> ''."""
    spec = (spec or "").strip().lower()
    if spec.startswith("giant/"):
        spec = spec[6:]
    return spec if spec in aria_garage.ROLES else ""


def _giant_target(role, cfg):
    """The target for a giant's role — or, when that giant cannot be used
    (not set, still downloading, failed to load), the next best thing with
    a note saying why: the smart tier, then the fast one. A tier that is
    itself pointed at an unusable giant is passed over, so this never
    loops."""
    ok, why = aria_garage.available(role, cfg)
    if ok:
        info = aria_garage.configured(cfg)[role]
        return {"kind": "openai", "url": f"http://127.0.0.1:{info['port']}",
                "model": info["label"], "backend": "giant", "key": "local",
                "spec": f"giant/{role}", "giant": role, "tier": role,
                "num_ctx": 0}
    target = None
    for tier in ("smart", "fast"):
        spec = cfg["tiers"].get(tier, "")
        other = giant_role(spec)
        if other:
            if other != role and aria_garage.available(other, cfg)[0]:
                target = _giant_target(other, cfg)
        elif spec:
            target = _parse_spec(spec, cfg)
        if target is not None:
            target["tier"] = tier
            break
    if target is None:
        target = _legacy_target(cfg, "smart")
    target["fallback"] = f"the {role} giant is unavailable — {why}"
    return target


def task_role(spec, cfg=None):
    """The giant a model spec ends up on ('' when it is not a giant) — the
    spec may be a role, or a tier pointed at one."""
    cfg = cfg or config()
    role = giant_role(spec)
    if role:
        return role
    if spec in ("fast", "smart", "deep"):
        order = {"deep": ("deep", "smart", "fast"), "smart": ("smart", "fast"),
                 "fast": ("fast",)}[spec]
        tier = next((cfg["tiers"][t] for t in order if cfg["tiers"].get(t)),
                    "")
        return giant_role(tier)
    return ""


class BackendDown(RuntimeError):
    """The model server could not be reached or dropped the connection —
    a reason to wait and retry, not a failure of the work itself."""


class GenerationTimeout(RuntimeError):
    """One generation ran past its wall-clock limit."""


class ReplyCutOff(RuntimeError):
    """The model ran out of room before it had finished: its window was
    full of prompt, reasoning and answer, or it reached its output limit.
    `reply` is what it had written, `thought` how many characters of
    reasoning came before it."""

    def __init__(self, message, reply="", thought=0):
        super().__init__(message)
        self.reply, self.thought = reply, thought


# What the last model call on this thread looked like — how fast it was,
# whether it reasoned first, whether it was cut off. Callers that care
# (run_agent, the self-test) read it with last_call().
_CALL = threading.local()
CALLS = collections.deque(maxlen=60)    # the recent ones, any thread


def last_call():
    return dict(getattr(_CALL, "info", None) or {})


def _note_call(target, **facts):
    info = {"model": target.get("model", ""), "tier": target.get("tier", ""),
            "at": time.time(), **facts}
    _CALL.info = info
    CALLS.append(info)


# A model's reasoning as it streams. It is shown (the Live tab, a "thinking"
# status in chat) and counts as a sign of life — it is never part of the
# answer. Callables take the text.
THINK_LISTENERS = []


def _thinking_note(text, on_thought=None):
    for fn in ([on_thought] if on_thought else []) + list(THINK_LISTENERS):
        try:
            fn(text)
        except Exception:
            pass


def backend_trouble(exc):
    """Is this exception the model server being away (vs the task failing)?"""
    if isinstance(exc, BackendDown):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, ConnectionError,
                            TimeoutError))


def strip_think(text):
    """Drop <think>…</think> reasoning that some local models put inline."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    return re.sub(r"^.*?</think>", "", text, flags=re.S | re.I).strip() \
        if "</think>" in text.lower() else text.strip()


def parse_json_reply(reply):
    """The JSON value in a model reply — tolerant of reasoning preambles and
    code fences. Returns None when there is none."""
    text = strip_think(reply or "")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    esc = (ch == "\\") and not esc
                    if ch == '"' and not esc:
                        in_str = False
                    if ch != "\\":
                        esc = False
                elif ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except ValueError:
                            break
            start = text.find(opener, start + 1)
    return None


def chat_stream(target, prompt, system=None, force_json=False, on_token=None,
                timeout=3600, max_seconds=0, think=None, on_thought=None):
    """Single-prompt convenience wrapper around chat_messages."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat_messages(target, messages, force_json=force_json,
                         on_token=on_token, timeout=timeout,
                         max_seconds=max_seconds, think=think,
                         on_thought=on_thought)


CHARS_PER_TOKEN = 3.2    # how prompts are sized; low for prose, fair for code
THINK_ROOM = 4000        # tokens a reasoning model needs beyond its prompt
                         # to think AND still answer


def thinking_for(target, messages, cfg=None):
    """Should a model that can "think" — reason at length before it
    answers, as qwen3 and deepseek-r1 do — think on this call? None leaves
    it to the model; False asks for the answer directly.

    Reasoning is paid for twice: in time, and in the window, which it
    shares with the prompt and the answer. A plan that is reasoned about
    for two thousand tokens and then has no room left to be written down
    is no plan. So with `thinking: auto`:
      - the quick tier never reasons — triage, chat and summaries have to
        be quick, and a chat that thinks for a minute before its first
        word looks like a chat that hangs;
      - the smart tier reasons when its window has room for it — give her
        a bigger window (context_tokens) and she thinks more;
      - the deep tier, a model you name yourself and the giants are left
        as they are: reasoning is what they are for.
    `always` leaves every model to itself; `never` asks every Ollama model
    for the answer alone."""
    if target.get("kind", "ollama") != "ollama" or target.get("giant"):
        return None                 # only Ollama takes the instruction
    mode = (cfg or config()).get("thinking", "auto")
    if mode == "always":
        return None
    if mode == "never" or target.get("tier") == "fast":
        return False
    if target.get("tier") == "smart":
        window = int(target.get("num_ctx") or 0)
        used = sum(len(m.get("content") or "") for m in messages
                   if isinstance(m.get("content"), str)) / CHARS_PER_TOKEN
        if window and window - used < THINK_ROOM:
            return False
    return None


def chat_messages(target, messages, force_json=False, on_token=None,
                  timeout=3600, max_seconds=0, think=None, on_thought=None):
    """Stream a chat completion from the target backend; returns the full
    reply text. `target` comes from resolve_tier() — a legacy config dict
    (url+model, no kind) is treated as Ollama. `timeout` is how long the
    server may stay silent; `max_seconds` (0 = no limit) caps the whole
    generation. `think` — None: thinking_for() decides; False: ask a
    reasoning model for the answer alone. `on_thought` receives a model's
    reasoning as it streams (it is never part of the reply).

    A reply that is cut off because the model's reasoning filled its
    window is asked for again, for the answer alone. One that is cut off
    all the same comes back as far as it got (last_call() says so) — or,
    for JSON, raises ReplyCutOff: half a JSON object is no answer."""
    deadline = time.time() + max_seconds if max_seconds else 0

    def call(target, think):
        try:
            return _chat_call(target, messages, force_json, on_token,
                              timeout, deadline, think, on_thought)
        except ReplyCutOff as cut:
            if cut.thought and think is not False \
                    and target.get("kind", "ollama") == "ollama":
                note = (f"{target['model']} reasoned until its window was "
                        "full — asking again for the answer alone")
                _garage_note(note)
                if on_token:
                    on_token(f"\n\n[{note}]\n\n")
                try:
                    return _chat_call(target, messages, force_json, on_token,
                                      timeout, deadline, False, on_thought)
                except ReplyCutOff as again:
                    cut = again
            if force_json:
                raise cut
            return cut.reply

    if target.get("giant"):
        # a giant: started (and others unloaded) on demand, and held so it
        # cannot be swapped out mid-answer
        cfg = config()
        try:
            with aria_garage.serving(target["giant"], cfg,
                                     notify=_garage_note):
                return call(target, None)
        except aria_garage.GarageBusy as exc:
            raise BackendDown(str(exc))
        except aria_garage.GarageError as exc:
            _garage_note(f"{exc} — using the smart tier instead")
            target = resolve_tier("smart", cfg)
    if not target.get("giant") and target.get("kind", "ollama") == "ollama":
        _free_for_everyday(target)
    if think is None:
        think = thinking_for(target, messages)
    try:
        return call(target, think)
    except BackendDown:
        # the model server is not running: start it (where it is hers to
        # start) and ask again — once, and not more than once a minute
        cfg = config()
        # (a start that could not be made — no right to, no Ollama found —
        # is not tried again for ten minutes: nothing will have changed)
        hold = 600 if WAKE["note"] else 60
        if target.get("kind", "ollama") != "ollama" \
                or not cfg.get("ollama_autostart", True) \
                or time.time() - WAKE["at"] < hold or ollama_up(cfg):
            raise
        if not ollama_start(cfg, notify=_garage_note)["up"]:
            raise
        return call(target, think)


GARAGE_LISTENERS = []           # callables that want giant load notices


# ---- two model servers, one machine --------------------------------------
#
# Ollama serves the everyday tiers and llama.cpp the giants; they share one
# GPU and one pool of RAM without knowing of each other. So they take
# turns: before a giant loads, Ollama is asked to let go of what it holds;
# before an everyday model is used, a giant that has finished its answer is
# unloaded. And a chat message does not make Ollama throw out the model
# autopilot is in the middle of using.

def ollama_loaded(cfg=None):
    """The models Ollama has in memory right now ([] when it cannot say)."""
    cfg = cfg or config()
    url = cfg["backend_urls"].get("ollama")
    if not url:
        return []
    try:
        with urllib.request.urlopen(url + "/api/ps", timeout=3) as r:
            return [m.get("name", "") for m in json.load(r).get("models", [])
                    if m.get("name")]
    except Exception:
        return []


def ollama_release(cfg=None):
    """Ask Ollama to unload everything it has in memory. Returns the names
    it was asked to let go of."""
    cfg = cfg or config()
    url = cfg["backend_urls"].get("ollama")
    released = []
    for name in ollama_loaded(cfg):
        try:
            req = urllib.request.Request(
                url + "/api/generate", method="POST",
                data=json.dumps({"model": name, "keep_alive": 0}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).close()
            released.append(name)
        except Exception:
            continue
    return released


def _free_for_giant(role, cfg, notify):
    if not cfg.get("giants_free_memory", True):
        return
    released = ollama_release(cfg)
    if released and notify:
        notify(f"asked Ollama to unload {', '.join(released)} so the {role} "
               "giant gets the memory — it loads again when next needed")


aria_garage.BEFORE_START.append(_free_for_giant)


def _free_for_everyday(target, cfg=None):
    """The other half of taking turns: before an everyday Ollama model is
    used, a giant that has finished its work is unloaded. Left loaded it
    holds most of the graphics card and much of the RAM, and Ollama then
    either refuses the everyday model or squeezes it onto the CPU, where
    it crawls. A giant in the middle of an answer is never touched. The
    Ollama models that were loaded beside it are let go as well, so they
    load again with the whole card instead of the corner they were given.
    The giant loads again when the next hard task needs it."""
    cfg = cfg or config()
    if not cfg.get("giants_free_memory", True) \
            or not aria_garage.loaded_roles(cfg):
        return []
    freed = aria_garage.stop_idle()
    if freed:
        squeezed = ollama_release(cfg)
        _garage_note(
            f"unloaded the {' and '.join(freed)} giant so "
            f"{target.get('model', 'the everyday model')} gets the memory"
            + (f" (and let Ollama reload {', '.join(squeezed)} with it)"
               if squeezed else "")
            + " — the giant loads again when a hard task needs it")
    return freed


def _in_memory(model, loaded):
    return model in loaded or f"{model}:latest" in loaded or (
        ":" not in model and any(n.split(":")[0] == model for n in loaded))


def chat_target(cfg=None):
    """The model that answers chat: the fast tier — unless autopilot is
    mid-step on another Ollama model that is in memory while the fast one
    is not. Then chat goes to that one: answering on the fast tier would
    make Ollama swap autopilot's model out, and autopilot swap it back in
    a moment later, each swap costing both of them the load time."""
    cfg = cfg or config()
    fast = resolve_tier("fast", cfg)
    if fast.get("kind", "ollama") != "ollama" or fast.get("giant") \
            or not autopilot_busy():
        return fast
    loaded = ollama_loaded(cfg)
    if not loaded or _in_memory(fast["model"], loaded):
        return fast
    smart = resolve_tier("smart", cfg)
    if smart.get("kind", "ollama") == "ollama" and not smart.get("giant") \
            and _in_memory(smart["model"], loaded):
        return smart
    return fast


# ---- the everyday model server: up when she is ---------------------------
#
# She cannot work without it, and "start Ollama first" is a step nobody
# should have to remember. When her server starts — and whenever a call
# finds the model server away — she starts it the way it is run on this
# machine, then loads the quick-tier model so the first reply does not
# wait for it. What she cannot do is start a service only an administrator
# may start; then she says so, with the one command that fixes it for good.

WAKE = {"at": 0.0, "trying": False, "how": "", "note": "", "started": False,
        "warmed": "", "warming": "", "log": "", "away": False}
_WAKE_LOCK = threading.Lock()
_WAKE_PROCS = []                        # an `ollama serve` she started
ENABLE_OLLAMA = "sudo systemctl enable --now ollama"
OLLAMA_UNIT = "aria-ollama"             # hers, when she runs `ollama serve`


def _is_local(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def uses_ollama(cfg=None):
    """Does any tier run on Ollama? Where none does, its server is not
    hers to start."""
    cfg = cfg or config()
    return any(not t.get("giant") and t.get("kind", "ollama") == "ollama"
               for t in (resolve_tier(n, cfg)
                         for n in ("fast", "smart", "deep")))


def ollama_up(cfg=None, timeout=2):
    cfg = cfg or config()
    url = cfg["backend_urls"].get("ollama")
    if not url:
        return False
    try:
        urllib.request.urlopen(url + "/api/tags", timeout=timeout).close()
        return True
    except Exception:
        return False


def _keep_alive(text):
    """The keep-loaded setting as Ollama's API takes it: a count of seconds
    goes as a number (a bare "-1" or "90" in a string is refused), a time
    with its unit as text. None: nothing is sent, and Ollama decides."""
    text = str(text or "").strip().lower()
    if not text:
        return None
    return int(text) if re.fullmatch(r"-?\d+", text) else text


def _systemctl(*args, timeout=5):
    try:
        return subprocess.run(["systemctl", *args], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _unit_known(*scope):
    """Does systemd know an ollama.service (among the user's units, or the
    system's)?"""
    done = _systemctl(*scope, "cat", "ollama.service")
    return bool(done) and done.returncode == 0


def _unit_state(*scope, unit="ollama.service"):
    """active, activating, inactive, failed … ("" when it cannot be told)"""
    done = _systemctl(*scope, "is-active", unit)
    return done.stdout.strip() if done else ""


def _unit_enabled(*scope):
    done = _systemctl(*scope, "is-enabled", "ollama.service")
    return None if done is None else done.returncode == 0


def _own_store():
    """Has this user Ollama models of their own? That is the sign of an
    Ollama that is run as them (`ollama serve` in a terminal) rather than
    as the system's service, which keeps its models elsewhere — and a
    server started the other way would come up with none of them."""
    store = Path(os.environ.get("OLLAMA_MODELS")
                 or Path.home() / ".ollama" / "models")
    try:
        return any(p.is_file() for p in (store / "manifests").rglob("*"))
    except OSError:
        return False


def ollama_how(cfg=None):
    """How the model server is started on this machine:
      "command"      the owner's own command (the ollama_command setting)
      "user-unit"    an ollama.service among the user's own units
      "system-unit"  the system's ollama.service
      "binary"       `ollama serve`, run as the user
      ""             no Ollama was found"""
    if os.environ.get("ARIA_OLLAMA_COMMAND") \
            or (cfg or config()).get("ollama_command"):
        return "command"
    if not shutil.which("ollama"):
        return ""
    if _unit_known("--user"):
        return "user-unit"
    if _unit_known() and (_unit_state() in ("active", "activating")
                          or _unit_enabled() or not _own_store()):
        return "system-unit"
    return "binary"


def ollama_boots(cfg=None):
    """Will the model server be there after the computer restarts, without
    anyone starting it? True: its own service is set to. False: it is not.
    None: it has no service (she starts it herself, when she starts)."""
    how = ollama_how(cfg)
    if how == "user-unit":
        return _unit_enabled("--user")
    if how == "system-unit":
        return _unit_enabled()
    return None


def _ollama_env(url):
    """What an `ollama serve` she starts is given beyond the usual: the
    Ollama and graphics-card settings of her own environment, and the
    address she expects it at when that is not Ollama's usual one."""
    env = {k: v for k, v in os.environ.items()
           if k.startswith(("OLLAMA_", "CUDA_", "HSA_", "ROCR_", "HIP_",
                            "GPU_DEVICE_"))}
    at = urllib.parse.urlparse(url)
    if (at.port or 11434) != 11434:
        env["OLLAMA_HOST"] = f"{at.hostname}:{at.port}"
    return env


def _spawn_ollama(how, url, cfg):
    """Start it. Returns (why not, "") — or ("", where its log is) when
    the start was issued."""
    port = urllib.parse.urlparse(url).port or 11434
    log = ROOT / "system" / "ollama.log"
    env = dict(os.environ)
    if how == "command":
        argv = [part.replace("{port}", str(port)) for part in shlex.split(
            os.environ.get("ARIA_OLLAMA_COMMAND")
            or cfg.get("ollama_command", ""))]
    elif how in ("user-unit", "system-unit"):
        scope = ["--user"] if how == "user-unit" else []
        journal = "journalctl " + " ".join(scope + ["-u", "ollama", "-e"])
        if _unit_state(*scope) in ("active", "activating"):
            return "", journal          # on its way up already: wait for it
        done = _systemctl(*scope, "--no-ask-password", "start",
                          "ollama.service", timeout=60)
        if done is not None and done.returncode == 0:
            return "", journal
        if how == "user-unit":
            return ("systemd could not start ollama.service: " + " ".join(
                (done.stderr or done.stdout).split())[:200]
                if done is not None else
                "systemd did not answer when asked to start ollama.service"
            ), ""
        if _unit_enabled():
            return ("Ollama's service is set to start with the computer but "
                    "is not running, and only an administrator can start "
                    "it: sudo systemctl start ollama   (why it stopped: "
                    + journal + ")"), ""
        return ("Ollama is installed as a system service, which only an "
                "administrator can start. Make it start with the computer "
                f"— once, in a terminal: {ENABLE_OLLAMA}"), ""
    elif how == "binary":
        argv = [shutil.which("ollama"), "serve"]
        extra = _ollama_env(url)
        env.update(extra)
        if shutil.which("systemd-run"):
            # a unit of its own, so that it outlives a restart of hers
            journal = f"journalctl --user -u {OLLAMA_UNIT} -e"
            if _unit_state("--user", unit=OLLAMA_UNIT + ".service") in (
                    "active", "activating"):
                return "", journal
            try:
                done = subprocess.run(
                    ["systemd-run", "--user", "--collect", "--quiet",
                     "--unit", OLLAMA_UNIT,
                     "--description", "Ollama, started by A.R.I.A.",
                     *[f"--setenv={k}={v}" for k, v in extra.items()],
                     *argv], capture_output=True, text=True, timeout=30)
                if done.returncode == 0:
                    return "", journal
            except (OSError, subprocess.TimeoutExpired):
                pass                    # no user manager here: run it plainly
    else:
        return ("I cannot find Ollama on this machine to start it (there "
                "is no `ollama` command). If it is started some other way, "
                "give me the command: aria settings set ollama_command "
                "\"…\""), ""
    if not argv:
        return "the command to start the model server is empty", ""
    try:                                # a log that does not grow for ever
        if log.stat().st_size > 5_000_000:
            log.replace(log.with_name(log.name + ".1"))
    except OSError:
        pass
    with open(log, "ab") as fh:
        _WAKE_PROCS.append(subprocess.Popen(
            argv, stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, env=env))
    return "", str(log)


def ollama_start(cfg=None, wait=45, notify=None):
    """Make sure the everyday model server is answering; start it if it is
    not and this is a machine she may start it on. Returns the state of
    that: {up, how, started, note}. Never raises."""
    cfg = cfg or config()
    url = cfg["backend_urls"].get("ollama", "")
    with _WAKE_LOCK:
        if ollama_up(cfg):
            WAKE.update(trying=False, note="", away=False)
            return {"up": True, "how": WAKE["how"], "started": False,
                    "note": ""}
        if not url or not _is_local(url):
            note = (f"the model server at {url} is not answering, and it "
                    "is not on this machine for me to start")
            WAKE.update(trying=False, how="remote", note=note, away=True)
            return {"up": False, "how": "remote", "started": False,
                    "note": note}
        how = ollama_how(cfg)
        WAKE.update(at=time.time(), trying=True, how=how, note="")
        if notify:
            notify("the model server is not running — starting it")
        try:
            note, log = _spawn_ollama(how, url, cfg)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            note, log = f"could not start the model server: {exc}", ""
        up = False
        if not note:
            deadline = time.time() + wait
            while time.time() < deadline and not up:
                time.sleep(0.5)
                up = ollama_up(cfg, timeout=1)
            if not up:
                note = ("the model server was started but is not answering "
                        f"yet — see {log}")
        WAKE.update(trying=False, note=note, log=log, away=not up,
                    started=up or WAKE["started"])
        if notify:
            notify("the model server is up" if up else note)
        return {"up": up, "how": how, "started": up, "note": note}


def ollama_warm(cfg=None, notify=None):
    """Load the quick-tier model now, so the first reply does not wait for
    it. Left alone when it would get in the way: a giant has the memory,
    or autopilot is mid-step and will load what it needs. Returns the
    model loaded, or ""."""
    cfg = cfg or config()
    target = resolve_tier("fast", cfg)
    if target.get("kind", "ollama") != "ollama" or target.get("giant") \
            or target.get("keep_alive") == 0 \
            or aria_garage.loaded_roles(cfg) or autopilot_busy():
        return ""
    model = target["model"]
    if _in_memory(model, ollama_loaded(cfg)):
        WAKE["warmed"] = model
        return model
    WAKE["warming"] = model
    if notify:
        notify(f"loading {model} so the first reply is quick")
    body = {"model": model}
    if target.get("keep_alive") is not None:
        body["keep_alive"] = target["keep_alive"]
    if target.get("num_ctx"):
        # the window she will ask for anyway: a different one later would
        # make Ollama load the model a second time
        body["options"] = {"num_ctx": int(target["num_ctx"])}
    try:
        req = urllib.request.Request(
            target["url"] + "/api/generate", method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=900).close()
        WAKE.update(warmed=model, warming="")
        if notify:
            notify(f"{model} is loaded")
        return model
    except Exception as exc:
        WAKE.update(warming="")
        if notify:
            notify(f"{model} could not be loaded ahead of time: "
                   + " ".join(str(exc).split())[:160])
        return ""


def models_wake(notify=None):
    """What her server does when it starts: the model server up, the quick
    model loaded. Each part can be switched off in Settings."""
    cfg = config()
    if not uses_ollama(cfg):
        return wake_state()
    state = {"up": ollama_up(cfg), "note": ""}
    if not state["up"] and cfg.get("ollama_autostart", True):
        state = ollama_start(cfg, notify=notify)
    WAKE["away"] = not state["up"]
    if state["up"] and cfg.get("models_warm", True):
        ollama_warm(cfg, notify=notify)
    return wake_state()


def wake_seen(up):
    """Told what a look at the model server found. Returns True when it
    has just come back from being away — started by hand, or by the
    computer a moment after she started: the time to load the quick model
    after all."""
    back = bool(up) and WAKE["away"]
    WAKE["away"] = not up
    if up and not WAKE["trying"]:
        WAKE["note"] = ""               # what was wrong no longer is
    return back


def wake_state():
    """For the sidebar and doctor: is she starting or loading something,
    and if the model server is away, what would fix that."""
    return {k: WAKE[k] for k in ("trying", "how", "note", "started",
                                 "warmed", "warming")}


def _garage_note(text):
    for fn in list(GARAGE_LISTENERS):
        try:
            fn(text)
        except Exception:
            pass


def _chat_call(target, messages, force_json, on_token, timeout, deadline,
               think=None, on_thought=None):
    try:
        if target.get("kind", "ollama") == "openai":
            return _openai_stream(target, messages, force_json, on_token,
                                  timeout, deadline, on_thought)
        return _ollama_stream(target, messages, force_json, on_token,
                              timeout, deadline, think, on_thought)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if exc.code == 400 and think is False and "think" in detail.lower():
            # a server or model that will not take the instruction: ask
            # the way it has always been asked
            return _chat_call(target, messages, force_json, on_token,
                              timeout, deadline, None, on_thought)
        if exc.code >= 500 or (exc.code == 503 and target.get("giant")):
            raise BackendDown(f"{target['model']}: HTTP {exc.code} {detail}")
        raise RuntimeError(f"{target['model']}: HTTP {exc.code} {detail}")
    except (urllib.error.URLError, ConnectionError, TimeoutError,
            json.JSONDecodeError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            raise BackendDown(f"{target['model']}: garbled stream ({exc})")
        raise BackendDown(f"{target['model']} unreachable at "
                          f"{target['url']}: {getattr(exc, 'reason', exc)}")


def _ollama_stream(target, messages, force_json, on_token, timeout, deadline,
                   think=None, on_thought=None):
    body = {"model": target["model"], "stream": True, "messages": messages}
    if force_json:
        body["format"] = "json"
    window = int(target.get("num_ctx") or 0)
    if window:
        # Without num_ctx Ollama silently truncates long prompts to its
        # small default window — fatal for a planner reading project state.
        # And no more writing than the window has room for: a reply that
        # outgrows it has the oldest part of its own prompt dropped to make
        # room, after which a model can go on writing, lost, for hours.
        # (The prompt is counted generously short here, so a reply that
        # would have fitted is never cut.)
        chars = sum(len(m.get("content") or "") for m in messages
                    if isinstance(m.get("content"), str))
        body["options"] = {"num_ctx": window,
                           "num_predict": max(256, window - int(chars / 4.5))}
    if think is False:
        body["think"] = False       # a reasoning model: the answer alone
    if target.get("keep_alive") is not None:
        # stay loaded between uses: a chat half an hour apart should not
        # wait for the model to be read from disk again
        body["keep_alive"] = target["keep_alive"]
    req = urllib.request.Request(
        target["url"] + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    parts, thought, last, t0 = [], 0, {}, time.time()
    # generous timeout: a larger-than-RAM "giant" model can sit silent for
    # a long time processing the prompt before the first token streams
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            raw = raw.strip()
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("error"):
                raise RuntimeError(f"{target['model']}: {data['error']}")
            message = data.get("message") or {}
            reasoning = message.get("thinking") or ""
            if reasoning:                   # shown, never part of the reply
                thought += len(reasoning)
                _thinking_note(reasoning, on_thought)
            tok = message.get("content", "")
            if tok:
                parts.append(tok)
                if on_token:
                    on_token(tok)
            if data.get("done"):
                last = data
                break
            if deadline and time.time() > deadline:
                raise GenerationTimeout(
                    f"{target['model']} ran past its time limit")
    reply = "".join(parts)
    read, wrote = (int(last.get("prompt_eval_count") or 0),
                   int(last.get("eval_count") or 0))
    took = float(last.get("eval_duration") or 0) / 1e9
    cut = last.get("done_reason") == "length" \
        or bool(window and read and read + wrote >= window)
    _note_call(target, think=think, thought=thought, cut_off=cut,
               prompt_tokens=read, tokens=wrote,
               seconds=round(time.time() - t0, 1),
               rate=round(wrote / took, 1) if took and wrote else 0,
               window=window)
    if cut:
        raise ReplyCutOff(
            f"{target['model']} ran out of room — its window of "
            f"{window or 'unknown size'} tokens was full"
            + (f" after {read} tokens of prompt and {wrote} of "
               + ("reasoning and answer" if thought else "answer")
               if read else ""), reply, thought)
    return reply


def _openai_stream(target, messages, force_json=False, on_token=None,
                   timeout=3600, deadline=0, on_thought=None):
    """Streaming client for any OpenAI-compatible server (llama.cpp
    llama-server, LM Studio, vLLM...)."""
    body = {"model": target["model"], "stream": True, "messages": messages}
    if force_json:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        target["url"].rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + target.get("key", "local")},
    )
    parts, thought, ended, timings, t0 = [], 0, "", {}, time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            raw = raw.strip()
            if not raw.startswith(b"data:"):
                continue
            payload = raw[5:].strip()
            if payload == b"[DONE]":
                break
            data = json.loads(payload)
            choices = data.get("choices") or [{}]
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content") or ""
            if reasoning:                   # shown, never part of the reply
                thought += len(reasoning)
                _thinking_note(reasoning, on_thought)
            tok = delta.get("content") or ""
            if tok:
                parts.append(tok)
                if on_token:
                    on_token(tok)
            ended = choices[0].get("finish_reason") or ended
            timings = data.get("timings") or timings      # llama-server
            if deadline and time.time() > deadline:
                raise GenerationTimeout(
                    f"{target['model']} ran past its time limit")
    reply = "".join(parts)
    _note_call(target, think=None, thought=thought, cut_off=ended == "length",
               prompt_tokens=int(timings.get("prompt_n") or 0),
               tokens=int(timings.get("predicted_n") or 0),
               seconds=round(time.time() - t0, 1),
               rate=round(float(timings.get("predicted_per_second") or 0), 1),
               window=0)
    if ended == "length":
        raise ReplyCutOff(
            f"{target['model']} ran out of room — its window was full "
            "before it had finished"
            + (" (its reasoning took the room)" if thought and not reply
               else ""), reply, thought)
    return reply


def ask_json(target, prompt, system, on_token=None, timeout=3600, tries=2):
    """Ask for JSON and get a parsed value back. A reply that is not JSON
    — or that was cut off before it was whole — is asked for once more,
    this time for the answer alone, with no reasoning first."""
    last, cut = "", None
    for attempt in range(tries):
        try:
            last = chat_stream(
                target, prompt if attempt == 0 else
                prompt + "\n\nYour previous reply was not valid JSON. "
                         "Reply with ONLY the JSON object, nothing else.",
                system, force_json=True, on_token=on_token, timeout=timeout,
                think=None if attempt == 0 else False)
            cut = None
            data = parse_json_reply(last)
        except ReplyCutOff as exc:
            # whole, or nothing: the lenient reader would find a complete
            # inner piece in a reply that stops short — one task of a plan
            # — and hand that back as if it were the answer
            last, cut = exc.reply, exc
            try:
                data = json.loads(strip_think(last))
            except (ValueError, TypeError):
                data = None
        if data is not None:
            return data
    if cut is not None:
        raise ValueError(
            f"{cut} — the reply did not fit. Raise the context window "
            "(Settings → Models, or: aria settings set context_tokens "
            "16384), or use a model that needs less room")
    raise ValueError(f"model did not return JSON: {last[:160]!r}")


def clip(text, limit, marker="\n[… trimmed …]\n"):
    """Keep text inside a character budget, preserving its head and tail."""
    text = text or ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit < 200:
        return text[:limit]
    head = int(limit * 0.7)
    return text[:head] + marker + text[-(limit - head - len(marker)):]


def prompt_budget(cfg=None, spec=""):
    """Characters a prompt may use: about half the context window, leaving
    the rest for the model's answer. With `spec` (a task's model) the
    window is that model's own — a giant is started with giants_ctx, which
    is usually smaller than what the everyday models are given."""
    cfg = cfg or config()
    tokens = cfg.get("context_tokens") or 8192
    if spec and task_role(spec, cfg):
        tokens = min(tokens, cfg.get("giants_ctx") or 8192)
    return max(6000, int(tokens * 3.2 * 0.5))


# ---------------------------------------------------------------- notes

def parse_note(path):
    """Return (frontmatter_dict, raw_frontmatter, body)."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}, "", text
    fm = dict(re.findall(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$", m.group(1), re.M))
    return fm, m.group(1), m.group(2)


def update_frontmatter(path, **updates):
    fm, raw, body = parse_note(path)
    raw_lines = raw.split("\n") if raw else []
    for key, value in updates.items():
        pattern = re.compile(rf"^{re.escape(key)}:.*$")
        for i, line in enumerate(raw_lines):
            if pattern.match(line):
                raw_lines[i] = f"{key}: {value}"
                break
        else:
            raw_lines.append(f"{key}: {value}")
    path.write_text("---\n" + "\n".join(raw_lines) + "\n---\n" + body, encoding="utf-8")


def slugify(text, limit=5):
    words = re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()
    return "-".join(words[:limit]) or "untitled"


def unique_path(path):
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def excerpt(body, limit=160):
    text = body.split("## Raw capture")[-1]
    text = re.split(r"^## ", text, maxsplit=1, flags=re.M)[0]
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def capture_note(text, source="ui"):
    """Instant, no-LLM capture — same format as `aria capture`."""
    text = text.strip()
    if not text:
        raise ValueError("empty capture")
    now_local = datetime.datetime.now()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    INBOX.mkdir(parents=True, exist_ok=True)
    snippet = slugify(text, limit=4)[:40] or "idea"
    path = unique_path(INBOX / f"{now_local:%Y-%m-%d-%H%M}-{snippet}.md")
    path.write_text(
        f"---\n"
        f"id: {now_local:%Y%m%d-%H%M}\n"
        f"captured: {now_utc:%Y-%m-%dT%H:%MZ}\n"
        f"status: inbox\ntype: unclassified\nslug:\nsource: {source}\ntags: []\n"
        f"---\n\n## Raw capture\n\n{text}\n\n"
        f"<!-- Sections below are added by the pipeline, never above this line. -->\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------- ledger

def ledger_update(name, typ, status, location, captured, replaces=None):
    today = datetime.date.today().isoformat()
    row = f"| {name} | {typ} | {status} | {location} | {captured} | {today} |"
    lines = LEDGER.read_text(encoding="utf-8").splitlines() if LEDGER.exists() else []
    keys = [f"| {n} " for n in (name, replaces) if n]
    for i, line in enumerate(lines):
        if any(line.startswith(k) for k in keys):
            lines[i] = row
            break
    else:
        for i, line in enumerate(lines):
            if line.startswith("|---"):
                lines.insert(i + 1, row)
                break
        else:
            lines += ["## Current state", "",
                      "| Slug / file | Type | Status | Location | Captured | Last moved |",
                      "|---|---|---|---|---|---|", row]
    lines.append(f"- {today} — {name} — → {location} ({status}) — by aria-ollama")
    LEDGER.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- triage

TRIAGE_SYSTEM = """You are a triage classifier for a personal idea pipeline.
Classify the note into exactly one category:
- project: something to be built or executed in multiple steps (an app, a
  website, research, an event). The bar: would it need a plan?
- task: one atomic action or list item (buy milk, email Sam).
- note: reference material, a thought worth keeping, a quote, a link.
- content: an idea for something to publish (video, TikTok, blog post).
- someday: a genuine project idea the note itself defers ("someday", "one day").
- unclear: you genuinely cannot tell. Do not force it.
Judge intent, not polish — rambling 3am notes are the normal case.
Respond with ONLY a JSON object, no other text:
{"type": "<category>", "confidence": <0.0-1.0>,
 "slug": "<kebab-case-2-to-5-words>", "list": "<grocery|tasks>",
 "reasoning": "<one sentence>", "questions": ["<only when unclear>"]}"""


def classify(target, body, on_token=None):
    data = ask_json(target, "Classify this note:\n\n" + body.strip(),
                    TRIAGE_SYSTEM, on_token=on_token)
    if not isinstance(data, dict) or data.get("type") not in CATEGORIES:
        got = data.get("type") if isinstance(data, dict) else data
        raise ValueError(f"model returned unknown type {got!r}")
    try:
        data["confidence"] = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        data["confidence"] = 0.0
    return data


def triage_run(emit, files=None):
    cfg = config()
    target = resolve_tier("fast", cfg)
    if files:
        files = [Path(f) if Path(f).is_absolute() else ROOT / f for f in files]
    else:
        files = sorted(p for p in INBOX.glob("*.md") if p.name != "README.md")
    files = [f for f in files if parse_note(f)[0].get("status") == "inbox"]
    if not files:
        emit({"event": "stage_done", "stage": "triage",
              "log": "inbox is empty — nothing to triage"})
        return {"failures": 0, "projects": []}

    emit({"event": "start", "stage": "triage", "count": len(files),
          "log": f"triaging {len(files)} inbox note(s) with {target['model']}"})
    failures = 0
    projects = []
    for f in files:
        fm, _, body = parse_note(f)
        captured = (fm.get("captured") or "?")[:10]
        emit({"event": "note", "file": f.name, "excerpt": excerpt(body),
              "log": f"reading {f.name}"})
        emit({"event": "prompt", "label": "classification prompt",
              "text": TRIAGE_SYSTEM + "\n\n---\n\nClassify this note:\n\n" + body.strip()})
        emit({"event": "thinking", "label": "classifying"})
        try:
            result = classify(target, body,
                              on_token=lambda t: emit({"event": "token", "text": t}))
        except Exception as exc:  # leave the note untouched on any failure
            emit({"event": "error", "file": f.name,
                  "log": f"ERROR  {f.name}: {exc} — left in inbox untouched"})
            failures += 1
            continue

        typ = result["type"]
        forced = ""
        if result["confidence"] < cfg["threshold"] and typ != "unclear":
            forced = f" (confidence {result['confidence']:.2f} below threshold — demoted to unclear)"
            typ = "unclear"
        emit({"event": "decision", "file": f.name, "type": typ,
              "confidence": result["confidence"],
              "reasoning": result.get("reasoning", ""),
              "log": f"decision: {typ} ({result['confidence']:.0%}) — "
                     f"{result.get('reasoning', '')}{forced}"})

        if typ == "unclear":
            questions = result.get("questions") or ["What did you want done with this?"]
            with f.open("a", encoding="utf-8") as fh:
                fh.write("\n## Questions from A.R.I.A.\n\n"
                         + "".join(f"- {q}\n" for q in questions))
            update_frontmatter(f, type="unclear")
            emit({"event": "question", "file": f.name, "questions": questions,
                  "log": f"unclear  {f.name}: needs your input"})
            continue

        if typ == "project":
            slug = result.get("slug") or slugify(body)
            dest = unique_path(PROCESSING / f"{slug}.md")
            update_frontmatter(f, type="project", status="processing", slug=dest.stem)
            f.rename(dest)
            ledger_update(dest.stem, "project", "processing",
                          "pipeline/01-processing/", captured, replaces=f.stem)
            projects.append(dest.stem)
            emit({"event": "moved", "file": f.name,
                  "to": f"pipeline/01-processing/{dest.name}",
                  "log": f"project  {f.name} -> 01-processing/{dest.name}"})
        elif typ == "task":
            list_name = "grocery" if result.get("list") == "grocery" else "tasks"
            list_file = KNOWLEDGE / "lists" / f"{list_name}.md"
            item = excerpt(body, limit=300)
            with list_file.open("a", encoding="utf-8") as fh:
                fh.write(f"- [ ] {item}  *(captured {captured})*\n")
            update_frontmatter(f, type="task", status="archived")
            dest = unique_path(ARCHIVE / f.name)
            f.rename(dest)
            ledger_update(f.stem, "task", "archived",
                          f"pipeline/knowledge/lists/{list_name}.md", captured)
            emit({"event": "moved", "file": f.name,
                  "to": f"pipeline/knowledge/lists/{list_name}.md",
                  "log": f"task     {f.name} -> {list_name}.md (original archived)"})
        else:  # note / content / someday
            folder = KNOWLEDGE_FOLDERS[typ]
            dest = unique_path(KNOWLEDGE / folder / f.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            update_frontmatter(f, type=typ, status="filed")
            f.rename(dest)
            ledger_update(f.stem, typ, "filed",
                          f"pipeline/knowledge/{folder}/", captured)
            emit({"event": "moved", "file": f.name,
                  "to": f"pipeline/knowledge/{folder}/",
                  "log": f"{typ:8} {f.name} -> knowledge/{folder}/"})

    emit({"event": "stage_done", "stage": "triage", "failures": failures,
          "projects": projects,
          "log": "done — run develop for any new projects, "
                 "answer questions on anything unclear"})
    return {"failures": failures, "projects": projects}


def auto_run(emit, files=None):
    """Capture-to-plan automation: triage, then develop whatever became a
    project — the 'idea goes in, proposed plan comes out' path. Pings the
    configured heartbeat URL (e.g. an Uptime Kuma push monitor) when a
    pass completes without failures."""
    result = triage_run(emit, files=files)
    if result["projects"]:
        develop_run(emit, slugs=result["projects"])
    heartbeat = config()["heartbeat_url"]
    if heartbeat and not result["failures"]:
        try:
            with urllib.request.urlopen(heartbeat, timeout=10):
                pass
            emit({"event": "heartbeat",
                  "log": "heartbeat sent (monitoring knows I'm alive)"})
        except Exception as exc:
            emit({"event": "error",
                  "log": f"heartbeat failed (non-fatal): {exc}"})
    return result


# ---------------------------------------------------------------- webhooks

def trigger_webhook(name, message=""):
    """POST to a webhook from the allowlist in system/config.yml
    (aria.webhooks). This is A.R.I.A.'s hand into the outside world —
    n8n flows, Home Assistant, deploy hooks — and she can only reach the
    ones YOU name there."""
    cfg = config()
    url = cfg["webhooks"].get(name)
    if not url:
        have = ", ".join(sorted(cfg["webhooks"])) or "none configured"
        raise ValueError(f"no webhook named '{name}' (configured: {have})")
    req = urllib.request.Request(
        url, data=json.dumps({"source": "aria", "webhook": name,
                              "message": message}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status


# ------------------------------------------------------------ internet
#
# Thin wrappers over aria_web: the leaf module does the fetching and the
# safety checks, this adds the engine's config and — for research — the
# model that turns a question into search queries.

WebError = aria_web.WebError


def web_fetch(url, limit=6000):
    """Read one public web page as text (dict: url, title, text, …)."""
    return aria_web.fetch(url, config(), limit=limit)


def web_search(query, limit=6):
    """Search the web: [{title, url, snippet, via}, …]."""
    return aria_web.search(query, config(), limit=limit)


def web_ready(cfg=None):
    """Can she search right now? (switch on, and a provider configured)"""
    cfg = cfg or config()
    return bool(cfg["web_access"] and aria_web.provider_chain(cfg))


QUERY_SYSTEM = """You turn a research need into web search queries.
Respond with ONLY JSON: {"queries": ["...", "..."]} — one to three short
keyword queries (3-8 words each), the kind a skilled researcher would type.
No quotes around whole queries, no site: operators, no personal details."""

_STOPWORDS = set("""a an and are as at be but by can could do does for from
get had has have how i if in into is it its just like me my not of on or
our so some than that the their them then there these they this to up us
was we were what when where which who why will with would you your about
make need want something thing stuff really maybe also""".split())


def keywords(text, limit=10):
    """The content words of a text, in order, without duplicates."""
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9+#.-]*", (text or "").lower()):
        w = w.strip(".-")
        if len(w) > 2 and w not in _STOPWORDS and w not in out:
            out.append(w)
    return out[:limit]


def search_queries(question, limit=3):
    """Search queries for a research need: asked of the fast model, with a
    plain keyword query as the fallback when the model is unavailable."""
    fallback = " ".join(keywords(excerpt(question, 400), 8))
    try:
        data = ask_json(resolve_tier("fast"),
                        "Research need:\n" + clip(question.strip(), 1500),
                        QUERY_SYSTEM, timeout=120, tries=1)
        queries = [" ".join(str(q).split())[:120]
                   for q in (data.get("queries") or []) if str(q).strip()]
    except Exception:
        queries = []
    return (queries or ([fallback] if fallback else []))[:limit]


def research(question, emit=None, pages=2, limit=6, budget=7000):
    """Research a question on the live web. Returns
    {"block": text for a prompt, "sources": [{title, url}], "queries": []}.
    `block` is empty when the web is off or nothing was found — callers
    then fall back to recalled knowledge and say so."""
    emit = emit or (lambda ev: None)
    cfg = config()
    empty = {"block": "", "sources": [], "queries": []}
    if not web_ready(cfg):
        return empty
    queries = search_queries(question)
    results, seen = [], set()
    for q in queries:
        emit({"event": "thinking", "label": f"searching the web: {q}"})
        try:
            rows = aria_web.search(q, cfg, limit=limit)
        except aria_web.WebError as exc:
            emit({"event": "error", "log": f"web search failed: {exc}"})
            continue
        for r in rows:
            if r["url"] not in seen:
                seen.add(r["url"])
                results.append(r)
    results = results[:limit + 2]
    if not results:
        return {**empty, "queries": queries}
    lines = [aria_web.UNTRUSTED_NOTE, "Cite what you use as [n] with its "
             "URL; say plainly when the material does not cover something.",
             ""]
    per_page = max(600, (budget - 1200) // max(1, pages))
    read = 0
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']} — {r['url']}")
        if r.get("snippet"):
            lines.append(f"    {r['snippet']}")
        if read < pages:
            try:
                page = aria_web.fetch(r["url"], cfg, limit=per_page)
                if len(page["text"]) > 200:
                    emit({"event": "thinking",
                          "label": f"reading {r['url']}"})
                    lines.append("    Page excerpt: " + " ".join(
                        page["text"].split()))
                    read += 1
            except aria_web.WebError:
                pass
    emit({"event": "sources", "sources": [
        {"title": r["title"], "url": r["url"]} for r in results],
        "log": f"web research: {len(results)} source(s), {read} page(s) "
               f"read"})
    return {"block": clip("\n".join(lines), budget),
            "sources": [{"title": r["title"], "url": r["url"]}
                        for r in results],
            "queries": queries}


def format_sources(sources):
    return "\n".join(f"[{i}] {s.get('title') or s['url']} — {s['url']}"
                     for i, s in enumerate(sources, 1))


WEB_ANSWER_SYSTEM = """You are A.R.I.A., answering a question from live web
research. Use ONLY the research material you are given; cite sources inline
as [n]. If the material does not answer the question, say so plainly rather
than guessing. Be direct: two to five sentences, no preamble, no markdown
headings."""


def answer_with_web(question, on_token=None, emit=None):
    """Search, read, and answer — the chat's "search for …" path. Returns
    {"text", "sources"}; raises WebError when the web is off."""
    cfg = config()
    if not cfg["web_access"]:
        raise aria_web.WebError(
            "web access is off — turn it on in Settings")
    found = research(question, emit=emit, pages=2)
    if not found["block"]:
        raise aria_web.WebError(
            "the search came back empty — try different words, or check "
            "the search provider in Settings")
    text = chat_stream(
        resolve_tier("fast", cfg),
        f"Question: {question}\n\nRESEARCH MATERIAL\n{found['block']}",
        WEB_ANSWER_SYSTEM, on_token=on_token, timeout=600)
    return {"text": strip_think(text), "sources": found["sources"]}


PAGE_SUMMARY_SYSTEM = """You are A.R.I.A., summarising a web page the user
asked you to read. Say what the page is and give its key points in three to
six sentences. Stick to what the page says. No preamble."""


def summarize_page(url, on_token=None):
    """Fetch a page and summarise it. Returns {"text", "sources"}."""
    cfg = config()
    page = aria_web.fetch(url, cfg, limit=min(9000, prompt_budget(cfg)))
    text = chat_stream(
        resolve_tier("fast", cfg),
        f"{aria_web.UNTRUSTED_NOTE}\n\nURL: {page['url']}\n"
        f"Title: {page['title']}\n\n{page['text']}",
        PAGE_SUMMARY_SYSTEM, on_token=on_token, timeout=600)
    return {"text": strip_think(text),
            "sources": [{"title": page["title"] or page["url"],
                         "url": page["url"]}]}


# ---------------------------------------------------------------- develop

PLANNER_SYSTEM = """You write concise project plans from raw idea notes.
Output plain markdown (no code fences around the whole reply) with exactly
these sections:
# Proposed plan: <name>
> **Pitch:** <one sentence>
## What we're building
## Recommended approach
## Scope   (with **In:** and **Out (for v1):** bullet lists)
## Milestones   (markdown table: # | Milestone | Rough effort | Done means)
## Recommended team   (subagent roles a project manager would need)
## Decisions needed from you
## Risks
Stay faithful to the idea's intent. Right-size it: a weekend tool gets a
one-page plan. Pick one approach and say why, don't offer menus.
If the note contains "Refinement request" sections, they are the owner's
binding feedback on earlier drafts — the new plan must incorporate them."""

RESEARCH_PROMPT = (
    "From prior knowledge only, list for this idea: likely existing "
    "products/projects (prior art), candidate tools and building blocks, "
    "rough audience notes, feasibility (effort ballpark and hard parts), "
    "and open questions. Markdown with those five sections. Flag anything "
    "you are unsure about:\n\n"
)

RESEARCH_DISCLAIMER = """> **Generated by the local engine (no web access).**
> Everything below is the model's prior knowledge, NOT verified research.
> Treat every claim as a lead to check during /review — or rerun this stage
> with the claude engine for sourced research.
"""

RESEARCH_WEB_PROMPT = (
    "Using the live web research below plus the idea, list: likely "
    "existing products/projects (prior art), candidate tools and building "
    "blocks, rough audience notes, feasibility (effort ballpark and hard "
    "parts), and open questions. Markdown with those five sections. Cite "
    "sources as [n] next to every claim they back; flag anything the "
    "research does not cover as unverified.\n\n"
    "## Idea\n{idea}\n\n## Live web research\n{results}\n"
)

RESEARCH_WEB_DISCLAIMER = """> **Generated by the local engine from live web research.**
> Claims marked [n] cite the sources listed at the end. Anything unmarked is
> the model's own synthesis — treat it as a lead to confirm during /review.
"""


def develop_run(emit, slugs=None):
    cfg = config()
    target = resolve_tier("smart", cfg)
    files = sorted(p for p in PROCESSING.glob("*.md") if p.name != "README.md")
    if slugs:
        wanted = set(slugs)
        files = [f for f in files if f.stem in wanted]
    if not files:
        emit({"event": "stage_done", "stage": "develop",
              "log": "nothing in 01-processing to develop"})
        return 0

    emit({"event": "start", "stage": "develop", "count": len(files),
          "log": f"developing {len(files)} idea(s) with {target['model']}"})
    today = datetime.date.today().isoformat()
    for f in files:
        fm, _, body = parse_note(f)
        slug = fm.get("slug") or f.stem
        captured = (fm.get("captured") or "?")[:10]
        emit({"event": "note", "file": f.name, "excerpt": excerpt(body),
              "log": f"developing {slug}"})
        try:
            # research first, so the plan can build on what already exists
            found = research(excerpt(body, limit=600), emit=emit, pages=2)
            if found["block"]:
                research_prompt = RESEARCH_WEB_PROMPT.format(
                    results=found["block"],
                    idea=clip(body.strip(), 3000))
                disclaimer = RESEARCH_WEB_DISCLAIMER
                thinking = "writing research from live web sources"
            else:
                research_prompt = RESEARCH_PROMPT + body.strip()
                disclaimer = RESEARCH_DISCLAIMER
                thinking = "recalling prior art (unverified — no web sources)"
            emit({"event": "prompt", "label": "research prompt",
                  "text": research_prompt})
            emit({"event": "thinking", "label": thinking})
            research_body = strip_think(chat_stream(
                target, research_prompt,
                on_token=lambda t: emit({"event": "token", "text": t})))
            plan_prompt = ("Write the proposed plan for this idea:\n\n"
                           + body.strip()
                           + "\n\n---\nResearch findings to build on ("
                           + ("from live web sources" if found["sources"]
                              else "recalled, unverified") + "):\n\n"
                           + clip(research_body, 3500))
            if found["sources"]:
                research_body += ("\n\n## Sources\n\n"
                                  + format_sources(found["sources"]))
            emit({"event": "prompt", "label": "plan prompt",
                  "text": PLANNER_SYSTEM + "\n\n---\n\n" + plan_prompt})
            emit({"event": "thinking", "label": "drafting proposed plan"})
            plan_body = strip_think(chat_stream(
                target, plan_prompt, PLANNER_SYSTEM,
                on_token=lambda t: emit({"event": "token", "text": t})))
        except Exception as exc:
            emit({"event": "error", "file": f.name,
                  "log": f"ERROR  {f.name}: {exc} — left in 01-processing untouched"})
            continue

        proj = POTENTIAL / slug
        proj.mkdir(parents=True, exist_ok=True)
        f.rename(proj / "idea.md")
        (proj / "research.md").write_text(
            f"---\nslug: {slug}\ntype: research\ncreated: {today}\n"
            f"engine: {target['kind']}:{target['model']}\n---\n\n{disclaimer}\n"
            + research_body.strip() + "\n",
            encoding="utf-8",
        )
        (proj / "proposed-plan.md").write_text(
            f"---\nslug: {slug}\nstatus: proposed\ncreated: {today}\n"
            f"updated: {today}\nengine: {target['kind']}:{target['model']}\n---\n\n"
            + plan_body.strip() + "\n\n## Review log\n\n"
            "<!-- /review appends decisions here: date — what changed — why -->\n",
            encoding="utf-8",
        )
        update_frontmatter(proj / "idea.md", status="proposed")
        ledger_update(slug, "project", "proposed",
                      f"pipeline/02-potential-projects/{slug}/", captured)
        emit({"event": "file_written", "file": f"{slug}/proposed-plan.md",
              "to": f"pipeline/02-potential-projects/{slug}/",
              "log": f"proposed {slug} -> 02-potential-projects/{slug}/ (draft plan ready)"})
        brain_journal(f"drafted a plan for [[{slug}]]"
                      + (" from live web research" if found["sources"]
                         else ""))
        if cfg["business_plans"]:
            # The plan is what the owner approves, and it is ready. What
            # follows is groundwork beside it: if it fails, the plan stands.
            try:
                business_run(slug, emit, cfg)
            except Exception as exc:
                emit({"event": "error",
                      "log": f"{slug}: the plan is ready; its business plan "
                             f"could not be written ({exc}) — try again "
                             f"with: aria business {slug}"})

    emit({"event": "stage_done", "stage": "develop",
          "log": "drafts ready — review them, then approve one on the "
                 "Projects page (or /review <slug> in Claude Code)"})
    return 0


# ---------------------------------------------------------------- refine

def refine(slug, feedback):
    """Attach the owner's feedback to a potential project and send it back
    through the pipeline: old plan/research are versioned (never deleted),
    the idea returns to 01-processing, and the next develop run re-plans
    with the feedback as binding input."""
    feedback = feedback.strip()
    if not feedback:
        raise ValueError("empty feedback")
    proj = POTENTIAL / slug
    idea = proj / "idea.md"
    if not idea.exists():
        raise ValueError(f"no such potential project: {slug}")

    version = len(list(proj.glob("proposed-plan.v*.md"))) + 1
    for name in ("proposed-plan", "research") + BUSINESS_DOCS:
        f = proj / f"{name}.md"
        if f.exists():
            f.rename(proj / f"{name}.v{version}.md")
    # (actions.json stays: the next draft carries the owner's ticks over)

    today = datetime.date.today().isoformat()
    with idea.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## Refinement request ({today}, round {version + 1})\n\n"
                 f"{feedback}\n")

    dest = PROCESSING / f"{slug}.md"
    idea.rename(dest)
    update_frontmatter(dest, status="processing")
    fm = parse_note(dest)[0]
    ledger_update(slug, "project", "processing", "pipeline/01-processing/",
                  (fm.get("captured") or "?")[:10])
    return dest


# ---------------------------------------------------------------- business
#
# Beside the plan, develop writes what it takes to make the idea real as a
# venture: a business plan, the list of everything a complete proposal
# needs (and what is still missing), and who does what — her part, and the
# owner's. The questions come from docs/PROJECT-GROUNDWORK.md; the shape of
# the documents from system/templates/business-plan.md and action-list.md.

BUSINESS_DOCS = ("business-plan", "action-list")
PROJECT_DOCS = ("idea", "research", "proposed-plan", "requirements") \
    + BUSINESS_DOCS

# What a complete proposal needs: (key, what it is, the document that
# always answers it — "" where a model has to judge how far it is answered)
PROPOSAL_ITEMS = (
    ("idea", "The idea, in your words", "idea"),
    ("prior_art", "Prior art and alternatives", "research"),
    ("plan", "The plan: scope, approach, milestones", "proposed-plan"),
    ("problem", "The problem and who has it, with evidence", ""),
    ("market", "Market size and competitors", ""),
    ("offer", "The offer and its price", ""),
    ("money", "Costs, break-even and a cash forecast", ""),
    ("funding", "Funding: how much, from where", ""),
    ("legal", "Legal form and registrations", ""),
    ("rules", "Licences, data protection, ownership of the work", ""),
    ("people", "People: who does what, what is missing", ""),
    ("risks", "Risks and how each is handled", ""),
    ("measures", "Milestones, and how success is measured", ""),
    ("decisions", "The decisions that are yours", ""),
)
STATE_MARK = {"there": "✔", "assumed": "~", "missing": "✗"}

BUSINESS_SYSTEM = """You write the business plan for a project idea, for its
owner — one person deciding whether and how to do this.
Write exactly the sections you are given, in order, each under its '## '
heading exactly as given. Plain markdown. No preamble, no closing remarks.
- Specific to THIS idea: name the kinds of customer, the competitors, the
  channels, the costs. No sentence that would fit any business.
- Sized to the idea. A small personal tool gets a few honest lines a
  section; where a section does not apply, say so in one line and why.
- Numbers are estimates: show the arithmetic (40 customers x 9 a month =
  360 a month) and say what each figure rests on. A range beats a point.
- Never present a guess as a fact. Never invent a statistic, a source, a
  law or a company. Where the research gives a fact, cite it as [n]. What
  you do not know, say — and say what would have to be found out.
- %s
- Plain words. No hype."""

ACTIONS_SYSTEM = """You turn a project's plan and business plan into who
does what. Respond with ONLY a JSON object:

{"proposal": {"<key>": {"state": "there|assumed|missing",
                        "note": "<one line: where it is answered, or what is still needed>"}},
 "aria": [{"title": "short imperative title",
           "detail": "what will exist when it is done, naming the document",
           "why": "what it is needed for"}],
 "you": [{"title": "short imperative title",
          "why": "why this takes a person",
          "prepare": "what A.R.I.A. can prepare so that your part is short"}]}

Rules:
- "proposal": one entry for EVERY key under PROPOSAL ITEMS. "there" = the
  documents answer it with evidence; "assumed" = answered with an estimate
  or an assumption that still needs checking; "missing" = not answered.
- "aria": 4 to 10 things an AI can do alone by researching, reasoning,
  calculating and writing: a competitor table, pricing options, a cost and
  break-even sheet, a cash forecast, draft terms or a privacy notice, an
  interview guide, landing-page copy. Not the build itself — the plan
  covers that.
- "you": 3 to 10 things only a person can do: talk to customers, choose a
  name, decide to spend, register, open accounts, sign, pay, meet people,
  test with real users. A decision or a legal act is never under "aria".
- Concrete and specific to this project. Nothing on both lists."""


def template_sections(name):
    """The numbered `## ` sections of a template, each with the guidance
    written under it between {{ }}: [(heading, guidance)]. A template is
    the one place a document's shape is decided — for Claude Code, which
    fills it in, and for the engine, which asks a model for these sections."""
    text = (ROOT / "system" / "templates" / f"{name}.md").read_text(
        encoding="utf-8")
    return [(m.group(1).strip(),
             " ".join(re.sub(r"[{}]", "", m.group(2)).split()))
            for m in re.finditer(r"^## (\d+\. [^\n]+)\n(.*?)(?=^## |\Z)", text,
                                 re.S | re.M)]


def _sections_only(text):
    """A model's sections without what it wrapped around them: a fence
    round the whole reply, a sentence of preamble."""
    text = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", text.strip())
    start = re.search(r"^(?:> \*\*In one line|## )", text, re.M)
    return text[start.start():].strip() if start else text.strip()


def _actions_file(proj):
    for f in (proj / "docs" / "actions.json", proj / "actions.json"):
        if f.is_file():
            return f
    return None


_LIST_ROW = re.compile(r"^\|\s*([✔✓~✗xX-])\s*\|\s*(.+?)\s*\|\s*(.*?)\s*\|\s*$")
_LIST_HERS = re.compile(r"^\s*[-*]\s+\*\*\s*(a\d+)\s*[—–-]+\s*(.+?)\*\*"
                        r"\s*(?:[—–-]+\s*(.*))?$", re.I)
_LIST_MINE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+\*\*\s*(y\d+)\s*[—–-]+\s*"
                        r"(.+?)\*\*\s*(?:[—–-]+\s*(.*))?$", re.I)


def _actions_from_markdown(text, slug):
    """An action list read back from its markdown — the form Claude Code's
    planner writes (from system/templates/action-list.md) and the form the
    engine itself renders. So the list is the same list whichever of the
    two wrote it."""
    keys = {item.lower(): key for key, item, _doc in PROPOSAL_ITEMS}
    states = {"✔": "there", "✓": "there", "~": "assumed"}
    data = {"slug": slug, "proposal": [], "aria": [], "you": []}
    for line in text.splitlines():
        m = _LIST_ROW.match(line)
        if m and m.group(2).strip().lower() not in ("item", "---"):
            item = m.group(2).strip()
            data["proposal"].append({
                "key": keys.get(item.lower(), slugify(item, limit=3)),
                "item": item,
                "state": states.get(m.group(1), "missing"),
                "note": m.group(3).strip()})
            continue
        m = _LIST_HERS.match(line)
        if m:
            detail = (m.group(3) or "").strip()
            why = re.search(r"\s*\*\((.+?)\)\*\s*$", detail)
            data["aria"].append({
                "id": m.group(1).lower(), "title": m.group(2).strip(),
                "detail": detail[:why.start()].strip() if why else detail,
                "why": why.group(1) if why else ""})
            continue
        m = _LIST_MINE.match(line)
        if m:
            rest = (m.group(4) or "").strip()
            done_on = re.search(r"\s*\*\(done ([\d-]*)\)\*\s*$", rest)
            if done_on:
                rest = rest[:done_on.start()].strip()
            why, _, prepare = rest.partition("*She can prepare:*")
            data["you"].append({
                "id": m.group(2).lower(), "title": m.group(3).strip(),
                "why": why.strip().rstrip("."), "prepare": prepare.strip(),
                "done": m.group(1) != " ",
                "done_on": done_on.group(1) if done_on else ""})
    return data


def _load_actions(proj, slug):
    f = _actions_file(proj)
    if f:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            return None
        for key in ("proposal", "aria", "you"):
            data.setdefault(key, [])
        return data
    listed = _project_doc(proj, "action-list")
    if listed:                          # written by hand, or by Claude Code
        data = _actions_from_markdown(listed.read_text(encoding="utf-8"),
                                      slug)
        if data["proposal"] or data["aria"] or data["you"]:
            return data
    return None


def project_actions(slug):
    """A project's action list as data — what a complete proposal needs,
    her part, the owner's part — or None when none has been written."""
    return _load_actions(_project_dir(slug), slug)


def _save_actions(slug, data, folder=None):
    """Write the action list: actions.json (what the app works from) and
    action-list.md (the same, to read), side by side."""
    proj = _project_dir(slug)
    current = _actions_file(proj) or _project_doc(proj, "action-list")
    folder = folder or (current.parent if current else proj)
    data["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    (folder / "actions.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    plan = _project_doc(proj, "proposed-plan")
    title = _project_title(slug, plan.read_text(encoding="utf-8")
                           if plan else "")

    def cell(text):
        return " ".join(str(text).split()).replace("|", "/")

    lines = [f"---\nslug: {slug}\ntype: action-list\n"
             f"created: {str(data.get('made', ''))[:10]}\n"
             f"updated: {data['updated'][:10]}\n---", "",
             f"# What needs doing: {title}", "",
             "## What a complete proposal needs", "",
             "✔ there · ~ there as an assumption that still needs checking "
             "· ✗ missing", "", "| | Item | Where it stands |", "|---|---|---|"]
    lines += [f"| {STATE_MARK.get(p['state'], '✗')} | {cell(p['item'])} | "
              f"{cell(p['note'])} |" for p in data["proposal"]]
    lines += ["", "## A.R.I.A.'s part", "",
              "What she can research, draft, calculate or build. After you "
              "approve the plan these join the project's requirements, and "
              "autopilot does them.", ""]
    lines += [f"- **{t['id']} — {t['title']}** — {t['detail']}"
              + (f" *({t['why']})*" if t.get("why") else "")
              for t in data["aria"]] or ["- (nothing listed)"]
    lines += ["", "## Your part", "",
              "What takes a person. Only you tick these — in the app, or "
              "with `aria done " + slug + " <id>`.", ""]
    lines += [f"- [{'x' if t.get('done') else ' '}] **{t['id']} — "
              f"{t['title']}** — {t['why']}"
              + (f". *She can prepare:* {t['prepare']}" if t.get("prepare")
                 else "")
              + (f" *(done {t['done_on']})*" if t.get("done") else "")
              for t in data["you"]] or ["- (nothing listed)"]
    (folder / "action-list.md").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")
    return folder / "action-list.md"


def _line(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _listy(value):
    """A list where a model was asked for one (it may give an object, a
    single item or nothing)."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _actions_from(raw, slug, before=None):
    """The model's who-does-what, made safe to store: every proposal item
    present, ids given, sizes bounded — and the owner's ticks carried over
    from the list this one replaces."""
    raw = raw if isinstance(raw, dict) else {}
    judged = raw.get("proposal") if isinstance(raw.get("proposal"), dict) \
        else {}
    proj = _project_dir(slug)
    proposal = []
    for key, item, doc in PROPOSAL_ITEMS:
        if doc:
            have = _project_doc(proj, doc)
            proposal.append({
                "key": key, "item": item,
                "state": "there" if have else "missing",
                "note": f"`{have.name}`" if have else "not written yet"})
            continue
        row = judged.get(key) if isinstance(judged.get(key), dict) else {}
        state = str(row.get("state") or "").strip().lower()
        proposal.append({
            "key": key, "item": item,
            "state": state if state in STATE_MARK else "missing",
            "note": _line(row.get("note"), 220) or "not covered yet"})
    ticked = {_line(t.get("title"), 140).lower(): t
              for t in (before or {}).get("you", []) if t.get("done")}
    aria, you, seen = [], [], set()
    for t in _listy(raw.get("aria"))[:10]:
        title = _line(t.get("title") if isinstance(t, dict) else t, 140)
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        aria.append({"id": f"a{len(aria) + 1}", "title": title,
                     "detail": _line(t.get("detail"), 300)
                     if isinstance(t, dict) else "",
                     "why": _line(t.get("why"), 200)
                     if isinstance(t, dict) else ""})
    for t in _listy(raw.get("you"))[:10]:
        title = _line(t.get("title") if isinstance(t, dict) else t, 140)
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        was = ticked.get(title.lower(), {})
        you.append({"id": f"y{len(you) + 1}", "title": title,
                    "why": _line(t.get("why"), 240)
                    if isinstance(t, dict) else "",
                    "prepare": _line(t.get("prepare"), 240)
                    if isinstance(t, dict) else "",
                    "done": bool(was), "done_on": was.get("done_on", "")})
    return {"slug": slug, "proposal": proposal, "aria": aria, "you": you}


def business_run(slug, emit=None, cfg=None):
    """Write (or rewrite) a project's business plan and its action list,
    from its idea, research and plan — with live web research on the
    market when the web is on. Returns the files written. The plan is the
    document the owner approves; if this stage fails the plan still
    stands."""
    emit = emit or (lambda ev: None)
    cfg = cfg or config()
    proj = _project_dir(slug)
    idea_f = _project_doc(proj, "idea")
    plan_f = _project_doc(proj, "proposed-plan")
    if not plan_f:
        raise ValueError(f"{slug} has no plan yet — the business plan is "
                         "written from the idea and its plan; run develop "
                         "first")
    idea = parse_note(idea_f)[2].strip() if idea_f else ""
    plan = parse_note(plan_f)[2].strip()
    plan = re.sub(r"\n## Review log.*\Z", "", plan, flags=re.S)
    res_f = _project_doc(proj, "research")
    known = parse_note(res_f)[2].strip() if res_f else ""
    folder = plan_f.parent
    target = resolve_tier("smart", cfg)
    budget = prompt_budget(cfg)
    based = cfg.get("based_in", "")
    where = (f"The owner is based in {based}: name the bodies, "
             "registrations and rules that apply there."
             if based else
             "Where the owner is based is not known: for whatever depends "
             "on the country (legal form, tax, licences, data protection) "
             "say that it does, and what to check.")
    emit({"event": "start", "stage": "business", "count": 1,
          "log": f"writing the business plan for {slug} with "
                 f"{target['model']}"})
    found = research(
        "market size, customers, competitors and what they charge"
        + (f", and the rules for a new business in {based}" if based else "")
        + ", for: " + excerpt(idea or plan, limit=500),
        emit=emit, pages=2, budget=min(4500, budget // 3))
    share = max(900, (budget - len(found["block"]) - 2200) // 3)
    context = ([f"THE IDEA (the owner's words)\n{clip(idea, share)}"]
               if idea else []) \
        + [f"THE PROPOSED PLAN\n{clip(plan, share)}"]
    if known:
        context.append("RESEARCH ALREADY DONE\n" + clip(known, share))
    if found["block"]:
        context.append("LIVE WEB RESEARCH ON THE MARKET (cite as [n])\n"
                       + found["block"])
    sections = template_sections("business-plan")
    half = (len(sections) + 1) // 2
    written = []
    for n, part in enumerate((sections[:half], sections[half:]), 1):
        if not part:
            continue
        ask = "\n\n".join(
            context
            + (["THE FIRST HALF OF THIS BUSINESS PLAN (written already — "
                "build on it, do not repeat it)\n"
                + clip(written[0], share)] if written else [])
            + [("Begin with one line — > **In one line:** who it is for, "
                "what they get, and how it earns (or, plainly, that it is "
                "not a business). Then write " if n == 1 else "Write ")
               + "these sections:\n\n" + "\n\n".join(
                   f"## {h}\n({g})" for h, g in part)])
        emit({"event": "thinking",
              "label": f"business plan for {slug}, part {n} of 2"})
        written.append(_sections_only(strip_think(chat_stream(
            target, ask, BUSINESS_SYSTEM % where,
            on_token=lambda t: emit({"event": "token", "text": t})))))
    body = "\n\n".join(w for w in written if w)
    if len(body) < 400:
        raise ValueError("the business plan came back empty")
    today = datetime.date.today().isoformat()
    title = _project_title(slug, plan)
    disclaimer = (
        "> **Drafted by the local engine from live web research.** Claims "
        "marked [n] cite the sources at the end; everything else is an "
        "estimate or an assumption to check. Not legal, tax or financial "
        "advice.\n" if found["sources"] else
        "> **Drafted by the local engine without web access.** Every "
        "figure and every claim about the market is the model's own "
        "estimate — a lead to check, not a finding. Not legal, tax or "
        "financial advice.\n")
    out = folder / "business-plan.md"
    out.write_text(
        f"---\nslug: {slug}\ntype: business-plan\ncreated: {today}\n"
        f"updated: {today}\nengine: {target['kind']}:{target['model']}\n"
        f"---\n\n# Business plan: {title}\n\n{disclaimer}\n{body}\n"
        + ("\n## Sources\n\n" + format_sources(found["sources"]) + "\n"
           if found["sources"] else ""), encoding="utf-8")
    emit({"event": "file_written", "file": f"{slug}/business-plan.md",
          "log": f"business plan written for {slug}"})
    files = [out]
    try:
        emit({"event": "thinking",
              "label": f"working out who does what for {slug}"})
        raw = ask_json(target, "\n\n".join([
            f"THE PROPOSED PLAN\n{clip(plan, share)}",
            "THE BUSINESS PLAN\n" + clip(body, max(2400, budget // 2)),
            "PROPOSAL ITEMS (judge each one)\n" + "\n".join(
                f"- {key}: {item}" for key, item, doc in PROPOSAL_ITEMS
                if not doc)]), ACTIONS_SYSTEM,
            on_token=lambda t: emit({"event": "token", "text": t}))
        data = _actions_from(raw, slug, before=project_actions(slug))
        if not data["aria"] and not data["you"]:
            raise ValueError("no tasks came back")
        data["made"] = datetime.datetime.now().isoformat(timespec="seconds")
        data["engine"] = f"{target['kind']}:{target['model']}"
        files.append(_save_actions(slug, data, folder))
        emit({"event": "file_written", "file": f"{slug}/action-list.md",
              "log": f"who does what for {slug}: {len(data['aria'])} for "
                     f"her, {len(data['you'])} for you; "
                     + str(sum(1 for p in data["proposal"]
                               if p["state"] != "there"))
                     + " proposal item(s) still open"})
    except Exception as exc:
        if backend_trouble(exc):
            raise
        emit({"event": "error",
              "log": f"{slug}: the business plan is written, but the list "
                     f"of who does what could not be made ({exc}) — draft "
                     f"it again: aria business {slug}"})
    brain_journal(f"wrote a business plan for [[{slug}]]")
    emit({"event": "stage_done", "stage": "business",
          "log": f"business groundwork ready for {slug}"})
    return files


# The app starts a draft and comes back for it: {slug: {running, error,
# started, finished}}
BUSINESS_JOBS = {}


def business_start(slug, emit=None):
    """Draft a project's business groundwork in the background."""
    _project_dir(slug)                           # validates the slug
    job = BUSINESS_JOBS.get(slug)
    if job and job.get("running"):
        return job
    job = BUSINESS_JOBS[slug] = {"running": True, "error": "", "step": "",
                                 "started": time.time()}

    def heard(ev):                      # what she is doing, for the app
        said = ev.get("label") or ev.get("log")
        if said and ev.get("event") != "token":
            job["step"] = " ".join(str(said).split())[:200]
        if ev.get("event") == "error" and "could not be made" in (
                ev.get("log") or ""):
            job["error"] = job["step"]
        if emit:
            emit(ev)

    def work():
        try:
            business_run(slug, heard)
        except Exception as exc:
            job["error"] = " ".join(str(exc).split())[:400]
        job.update(running=False, finished=time.time())

    threading.Thread(target=work, daemon=True).start()
    return job


def project_business(slug, text=True):
    """What the app's Business tab shows: the plan (its text, unless
    `text` is False — a poll only needs to know whether it changed) and
    where it is, the action list, and whether a draft is being written
    right now."""
    proj = _project_dir(slug)
    plan = _project_doc(proj, "business-plan")
    actions = project_actions(slug)
    return {"plan": plan.read_text(encoding="utf-8") if plan and text
            else "",
            "has_plan": plan is not None,
            "stamp": int(plan.stat().st_mtime) if plan else 0,
            "path": str(plan.relative_to(proj)) if plan else "",
            "actions": actions,
            "list_path": (str(_project_doc(proj, "action-list")
                              .relative_to(proj))
                          if _project_doc(proj, "action-list") else ""),
            "job": dict(BUSINESS_JOBS.get(slug) or {}),
            "can_draft": _project_doc(proj, "proposed-plan") is not None}


def project_action_done(slug, item_id, done=True):
    """The owner ticks (or unticks) an item on their own list. Only ever
    called from the owner's own action — the checkbox, `aria done`, or
    saying so in chat. A.R.I.A. never ticks an item of theirs."""
    data = project_actions(slug)
    if not data:
        raise ValueError(f"{slug} has no action list yet")
    item = next((t for t in data["you"] if t["id"] == str(item_id).lower()),
                None)
    if item is None:
        raise ValueError(
            f"no item {item_id} on your list for {slug} — it has: "
            + (", ".join(t["id"] for t in data["you"]) or "nothing"))
    item["done"] = bool(done)
    item["done_on"] = datetime.date.today().isoformat() if done else ""
    _save_actions(slug, data)
    if done:
        brain_journal(f"you ticked off “{item['title']}” for [[{slug}]]")
    return item


def owner_list_text(slug=""):
    """What is on the owner's own list — for one project, or all of them —
    speakable."""
    slugs = [slug] if slug else [p["slug"] for p in list_projects()]
    parts = []
    for s in slugs:
        data = project_actions(s)
        todo = [t for t in (data or {}).get("you", []) if not t.get("done")]
        if not todo:
            continue
        parts.append(
            f"For {s.replace('-', ' ')}, {_count(len(todo), 'thing')} "
            f"{_is(len(todo))} yours: "
            + "; ".join(f"{t['id']}, {t['title']}" for t in todo[:5])
            + (f"; and {len(todo) - 5} more" if len(todo) > 5 else "") + ".")
    if parts:
        return " ".join(parts) + (" Tick one off in the app, or say "
                                  "'done' with its id and the project.")
    if slug and not project_actions(slug):
        return (f"{slug.replace('-', ' ')} has no action list yet — say "
                f"'business plan for {slug}' and I'll write one.")
    return "Nothing is waiting on your own lists."


# ---------------------------------------------------------------- brain
#
# Her Obsidian vault (aria.obsidian_vault). aria_brain does the writing and
# keeps every file inside <vault>/ARIA/; these wrappers add the config and
# gather what a project has to say. Journal lines are best-effort: a
# missing vault must never fail the work that wanted to log itself.

BrainError = aria_brain.BrainError


def brain_ready(cfg=None):
    cfg = cfg or config()
    return bool(cfg["obsidian_vault"]) and \
        Path(os.path.expanduser(cfg["obsidian_vault"])).is_dir()


def _vault(cfg=None):
    return aria_brain.vault_dir((cfg or config())["obsidian_vault"])


def brain_note(text, title=None, source="chat"):
    """Write something the owner wants remembered into ARIA/Notes/."""
    path = aria_brain.note(_vault(), text, title=title, source=source)
    brain_journal(f"noted: [[{path.stem}]]")
    return path


def brain_research(question, answer, sources):
    """Keep a web-research answer, with its sources, in ARIA/Research/."""
    return aria_brain.research(_vault(), question, answer, sources)


def brain_journal(line):
    """One line in today's journal note; silently skipped without a vault."""
    try:
        cfg = config()
        if cfg["brain_journal"] and brain_ready(cfg):
            return aria_brain.journal(_vault(cfg), line)
    except Exception:
        pass
    return None


def _project_doc(proj, name):
    for f in (proj / "docs" / f"{name}.md", proj / f"{name}.md"):
        if f.is_file():
            return f
    return None


def plan_pitch(proj):
    plan = _project_doc(proj, "proposed-plan")
    if not plan:
        return ""
    m = re.search(r"^\s*>\s*\*\*Pitch:\*\*\s*(.+)$",
                  plan.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else ""


def brain_sync_project(slug):
    """Mirror a project into the vault: its pipeline documents verbatim
    plus whatever autopilot has written (assessment, memory, changelog,
    final report), under one hub note. Returns the hub note's path."""
    vault = _vault()
    proj = _project_dir(slug)
    docs = []
    for name in PROJECT_DOCS:
        f = _project_doc(proj, name)
        if f:
            docs.append((name, f.read_text(encoding="utf-8"), False))
    auto = _auto_dir(slug, create=False)
    for name, fname, limit in (("assessment", "ASSESSMENT.md", 40000),
                               ("memory", "MEMORY.md", 40000),
                               ("changelog", "CHANGELOG.md", 80000),
                               ("final-report", "FINAL-REPORT.md", 120000)):
        f = auto / fname
        if f.is_file():
            text = f.read_text(encoding="utf-8")
            if name == "final-report":
                # its file links are relative to where it sits in the
                # project; in the vault they must point back there
                text = re.sub(
                    r"\]\((\.\.?)/([^)\s]+)\)",
                    lambda m: "](" + (proj.resolve() if m.group(1) == ".."
                                      else auto.resolve()).as_uri()
                    + "/" + m.group(2) + ")", text)
            docs.append((name, text[-limit:] if name == "changelog"
                         else text[:limit], True))
    state = {}
    if (auto / "state.json").is_file():
        try:
            state = json.loads((auto / "state.json").read_text("utf-8"))
        except ValueError:
            state = {}
    status = ""
    if state.get("status") and state["status"] != "idle":
        status = (f"autopilot {state['status']} — iteration "
                  f"{state.get('iteration', 0)}, "
                  f"{(state.get('stats') or {}).get('done', 0)} task(s) done")
    try:
        source = str(proj.relative_to(ROOT)) + "/"
    except ValueError:
        source = str(proj)
    return aria_brain.sync_project(vault, slug, docs, pitch=plan_pitch(proj),
                                   status=status, source=source)


def export_obsidian(slug):
    """Publish a project into the Obsidian vault (the board's → Obsidian
    button): one folder of prefixed notes plus a hub note with wikilinks
    and tags, so it joins the second-brain graph."""
    index = brain_sync_project(slug)
    plan = _project_doc(_project_dir(slug), "proposed-plan")
    if plan:
        update_frontmatter(plan, exported=datetime.date.today().isoformat())
    return index


# ---------------------------------------------------------------- board

def _note_cards(folder):
    cards = []
    for p in sorted(folder.glob("*.md")):
        if p.name == "README.md":
            continue
        fm, _, body = parse_note(p)
        cards.append({
            "file": p.name,
            "path": str(p.relative_to(ROOT)),
            "type": fm.get("type", "?"),
            "status": fm.get("status", "?"),
            "captured": (fm.get("captured") or "")[:10],
            "excerpt": excerpt(body),
        })
    return cards


def board():
    potential = []
    if POTENTIAL.is_dir():
        for d in sorted(POTENTIAL.iterdir()):
            if not d.is_dir():
                continue
            plan = d / "proposed-plan.md"
            # a folder holding only versioned drafts is mid-refinement —
            # the idea itself is showing in Processing right now
            if not plan.exists() and not (d / "idea.md").exists() \
                    and not (d / "requirements.md").exists():
                continue
            fm = parse_note(plan)[0] if plan.exists() else {}
            pitch = ""
            if plan.exists():
                m = re.search(r"^\s*>\s*\*\*Pitch:\*\*\s*(.+)$",
                              plan.read_text(encoding="utf-8"), re.M)
                pitch = m.group(1).strip() if m else ""
            potential.append({
                "slug": d.name,
                "path": str(plan.relative_to(ROOT)) if plan.exists() else "",
                "approved": (d / "requirements.md").exists(),
                "status": fm.get("status", "proposed"),
                "pitch": pitch,
                "rounds": len(list(d.glob("proposed-plan.v*.md"))) + 1,
                "exported": bool(fm.get("exported")),
            })

    knowledge = {}
    for sub in ("notes", "lists", "content-ideas", "someday"):
        d = KNOWLEDGE / sub
        items = []
        if d.is_dir():
            for p in sorted(d.glob("*.md")):
                if p.name == "README.md":
                    continue
                items.append({"file": p.name, "path": str(p.relative_to(ROOT))})
        knowledge[sub] = items
    open_tasks = 0
    lists_dir = KNOWLEDGE / "lists"
    if lists_dir.is_dir():
        for p in lists_dir.glob("*.md"):
            open_tasks += p.read_text(encoding="utf-8").count("- [ ]")

    target = promotion_dir()
    active = []
    if target.is_dir():
        for d in sorted(target.iterdir()):
            status_file = d / "STATUS.md"
            if not d.is_dir():
                continue
            if status_file.exists():
                text = status_file.read_text(encoding="utf-8")
                phase = re.search(r"\*\*Phase:\*\*\s*(.+)", text)
                active.append({"slug": d.name, "phase": phase.group(1).strip()
                               if phase else "?"})
            elif (d / ".aria").is_dir():           # an adopted codebase
                active.append({"slug": d.name, "phase": "adopted codebase"})

    return {
        "inbox": _note_cards(INBOX),
        "processing": _note_cards(PROCESSING),
        "potential": potential,
        "knowledge": knowledge,
        "open_tasks": open_tasks,
        "active": active,
    }


# ---------------------------------------------------------------- chat

CHAT_SYSTEM = """You are A.R.I.A. (Autonomous Reasoning & Intelligence \
Assistant), the voice of a personal idea-to-project system that runs on the \
user's own computer. Your replies are spoken aloud, so answer in one to \
three short conversational sentences — no markdown, no lists, no emoji. Be \
warm, capable, lightly witty — and strictly honest.

What is happening right now: {summary}
{detail}
Hard facts about yourself — never contradict them:
{web_fact}
- The app executes ONLY these phrases when the user says them: {commands}. \
Never invent other commands and never tell the user to say a phrase that is \
not on this list.
- Never claim an action happened or a thing exists unless the state above \
shows it. If you are unsure, say you are unsure."""

WEB_FACT_OFF = (
    "- You have NO web access. You cannot search the internet, browse, or "
    "fetch live data like job postings, companies, prices or news, and you "
    'can never "display a list" you did not actually produce. When asked '
    'for such things, say so plainly and suggest: "capture <the idea>" — '
    "the pipeline will draft a plan with research leads clearly marked "
    "unverified."
)

WEB_FACT_ON = (
    "- You can use the live web, but only through commands: \"search for "
    "<topic>\" runs a real search and answers from what it finds, and "
    "\"read <url>\" fetches a page. In ordinary conversation you are NOT "
    "browsing: never present recalled knowledge as something you just "
    "looked up, and when asked for live data (news, prices, jobs, weather, "
    "what exists today) tell the user to say \"search for …\"."
)


def board_summary_text():
    b = board()
    unclear = [n["file"] for n in b["inbox"] if n["type"] == "unclear"]
    proposed = [p["slug"] for p in b["potential"] if not p["approved"]]
    approved = [p["slug"] for p in b["potential"] if p["approved"]]
    parts = [f"{len(b['inbox'])} note(s) in inbox"
             + (f" ({len(unclear)} unclear, waiting on the user)" if unclear else ""),
             f"{len(b['processing'])} awaiting develop",
             f"{len(proposed)} proposed plan(s) awaiting the user's approval"
             + (f": {', '.join(proposed)}" if proposed else "")]
    if approved:
        parts.append("approved, not started: " + ", ".join(approved))
    parts.append(f"{b['open_tasks']} open task(s)")
    if b["active"]:
        parts.append("active projects: " + ", ".join(
            f"{p['slug']} ({p['phase']})" for p in b["active"]))
    missions = mission_list()
    if missions:
        running = [m for m in missions if m["status"] == "running"]
        queued = [m for m in missions if m["status"] == "queued"]
        done = [m for m in missions if m["status"] in ("done", "failed")]
        mp = []
        if running:
            mp.append("running: " + ", ".join(m["id"] for m in running))
        if queued:
            mp.append(f"{len(queued)} queued")
        if done:
            mp.append("latest finished: " + done[-1]["id"]
                      + f" ({done[-1]['status']})")
        parts.append("background missions — " + "; ".join(mp))
    projects = list_projects()
    running_projects = [p for p in projects if p["autopilot"] == "running"]
    if running_projects:
        def road(p):
            try:
                pr = _autopilot().progress(p["slug"])
            except Exception:
                return ""
            return (f", {pr['reached']} of {pr['total']} milestones reached"
                    if pr["total"] else "")
        parts.append("autopilot working on: " + ", ".join(
            f"{p['slug']} (iteration {p['iteration']}, {p['done']} of "
            f"{p['total']} tasks done" + road(p)
            + (f", waiting: {p['waiting']}" if p.get("waiting") else
               f", now: {p['current']}" if p.get("current") else "") + ")"
            for p in running_projects))
    owed = [f"{p['slug']} {g['id']} ({g['title']})"
            for p in projects for g in p["needs_you"]]
    if owed:
        parts.append("tasks waiting for the user's own decision: "
                     + "; ".join(owed[:5]))
    try:
        cfg = config()
        giants = aria_garage.configured(cfg)
        if giants:
            loaded = aria_garage.loaded_roles(cfg)
            parts.append("giant models: " + ", ".join(
                f"{role} = {info['label']}"
                + (" (loaded)" if role in loaded else "")
                for role, info in giants.items())
                + f" — used {cfg['giants_use']}")
    except Exception:
        pass
    return "; ".join(parts)


# ---- matching what was said to a project --------------------------------

_FILLER = set("the my our a an project app tool plan one thing called named "
              "please now for on of it to".split())


def project_slugs():
    return [p["slug"] for p in list_projects()]


def match_project(text, slugs=None):
    """The project a spoken phrase refers to ("the meal planner" ->
    receipt-based-meal-planner), or None when nothing — or more than one
    thing — fits."""
    slugs = project_slugs() if slugs is None else slugs
    if not slugs:
        return None
    said = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower().replace("-", " "))
    if "-".join(said.split()) in slugs:
        return "-".join(said.split())
    words = [w for w in said.split() if w not in _FILLER]
    if not words:
        return None
    scored = []
    for slug in slugs:
        parts = slug.split("-")
        hit = [w for w in words if w in parts]
        if not hit or max(len(w) for w in hit) < 3:
            continue
        covers_slug = len(set(hit)) == len(set(parts))
        covers_said = len(hit) / len(words)
        if covers_slug or covers_said >= 0.6:
            scored.append((len(set(hit)), covers_said, -len(parts), slug))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][:2] == scored[1][:2]:
        return None                      # ambiguous — don't guess
    return scored[0][3]


# ---- spoken commands -----------------------------------------------------

INTENT_CAPTURE = re.compile(
    r"^\s*(?:capture|remember|note down|add (?:an? )?idea)\b[:,]?\s*(.+)$",
    re.I | re.S)
INTENT_TRIAGE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:run|start|do)\s+)?(?:the\s+)?triage\b", re.I)
INTENT_DEVELOP = re.compile(
    r"^\s*(?:please\s+)?(?:run\s+)?develop\b|\bmake (?:the )?plans?\b", re.I)
INTENT_SKILL_ON = re.compile(
    r"^\s*(?:use|load|activate)\s+(?:the\s+)?skill\s+([\w-]+)", re.I)
INTENT_SKILL_OFF = re.compile(
    r"^\s*(?:drop|unload|clear|deactivate)\s+(?:the\s+)?skill\b", re.I)
INTENT_AGENT = re.compile(
    r"^\s*(?:ask|tell|have|get)\s+(?:the\s+)?(\w[\w-]*)\s+(?:agent\s+)?to\s+(.+)$",
    re.I | re.S)
INTENT_MISSION = re.compile(
    r"^\s*(?:mission|overnight|background|queue)\b[:,]?\s*(?:ask\s+)?"
    r"(?:the\s+)?(\w[\w-]*)\s+(?:agent\s+)?to\s+(.+)$", re.I | re.S)
INTENT_APPROVE = re.compile(
    r"^\s*(?:approve|green\s?light|sign off on)\s+(?:the\s+)?"
    r"(?:plan\s+for\s+)?(.+?)"
    r"(\s+and\s+(?:start|begin|go|work on it|get (?:to work|going)))?\s*$",
    re.I)
# the owner's answer on a task that waits for them: "approve task t5 in
# larder: call it Larder", "skip t7 in larder because there is no budget"
INTENT_TASK = re.compile(
    r"^\s*(approve|allow|green\s?light|go ahead with|skip|decline|reject|"
    r"refuse)\s+(?:the\s+)?(?:task\s+(?:number\s+)?(t?\d+)|(t\d+))\b"
    r"(?:\s+(?:in|on|of|for)\s+(?:the\s+)?(.+?))?"
    r"\s*(?:(?::|,|—|\bwith\b|\bbecause\b)\s*(.+))?$", re.I | re.S)
INTENT_AUTOPILOT = re.compile(
    r"^\s*(?:work on|start (?:work(?:ing)? on|autopilot(?: on| for)?)|"
    r"autopilot|begin(?: work(?:ing)? on)?|resume(?: work(?:ing)? on)?|"
    r"continue(?: work(?:ing)? on)?|get to work on)\s+(.+?)\s*$", re.I)
INTENT_AUTOPILOT_STOP = re.compile(
    r"^\s*(?:stop(?: the)?(?: autopilot| working| work)(?: on| for)?|"
    r"stand down(?: on)?|halt(?: work on)?)\s+(.+?)\s*$", re.I)
INTENT_STEER = re.compile(
    r"^\s*(?:steer|direct|tell|for)\s+(.+?)\s*(?:\bto\b|:|,)\s*(.+)$",
    re.I | re.S)
INTENT_STATUS = re.compile(
    r"^\s*(?:status(?: report)?(?: of| on| for)?|how(?:'s| is)|"
    r"progress(?: on| of)?|where are (?:we|you) (?:on|with))\s+(.+?)"
    r"(?:\s+(?:going|doing|coming along|looking))?\s*\??\s*$", re.I)
INTENT_STATUS_ALL = re.compile(
    r"^\s*(?:what are you (?:working on|doing|up to)|what(?:'s| is) "
    r"(?:running|happening|going on)|status(?: report)?|give me (?:a |the )?"
    r"status(?: report)?|how are (?:things|the projects)(?: going)?)\s*\??\s*$",
    re.I)
# "how far along is the meal planner", "what's left on …", "what's next
# for …", "the roadmap for …", "when will … be done" — answered from the
# roadmap and the measured pace, without asking a model
INTENT_PROGRESS = re.compile(
    r"^\s*(?:how far(?: along)?(?: is| are (?:you|we) (?:on|with))?|"
    r"what(?:'s| is) (?:left|next|remaining|still to do)"
    r"(?: (?:on|for|in|with))?|(?:show (?:me )?)?(?:the )?(?:road ?map|"
    r"milestones)(?: (?:for|of|on))?|how (?:long|much longer)"
    r"(?: (?:until|till|for|on|will))?|when will(?: you)?"
    r"(?: (?:be done with|finish))?)\s+(.+?)"
    r"(?:\s+(?:be (?:done|finished|ready)|finish|take|is (?:done|finished)))?"
    r"\s*\??\s*$", re.I)
INTENT_PROGRESS_ALL = re.compile(
    r"^\s*(?:how far(?: along)?(?: are (?:you|we))?|what(?:'s| is) "
    r"(?:left|next|remaining|still to do)(?: to do)?|(?:show (?:me )?)?"
    r"(?:the )?(?:road ?map|milestones)|how (?:long|much longer)"
    r"(?: (?:will (?:it|this|that) take|is left|to go))?|when will "
    r"(?:you|it|this|that) (?:be (?:done|finished|ready)|finish))"
    r"\s*\??\s*$", re.I)
# "what needs me", "does anything need me", "what's waiting on me"
INTENT_NEEDS = re.compile(
    r"^\s*(?:what(?:'s| is)? (?:needs?|waiting (?:on|for)) (?:me|my "
    r"(?:decision|approval|attention))|what needs (?:me|my (?:decision|"
    r"approval|attention))|(?:does|is) (?:anything|something) (?:need(?:s|ing)?"
    r"|waiting (?:on|for)) (?:me|my (?:decision|approval|attention))|"
    r"(?:is there )?anything (?:for me|i need to do|waiting (?:on|for) me)|"
    r"do you need (?:me|anything from me))\s*\??\s*$", re.I)
# "business plan for the meal planner", "write a business plan for …"
INTENT_BUSINESS = re.compile(
    r"^\s*(?:(?:write|draft|make|do|create|plan out)\s+)?(?:me\s+)?"
    r"(?:(?:a|the)\s+)?business (?:plan|case)\s+(?:for|of|on)\s+(.+?)\s*$",
    re.I)
# "what's on my list", "my list for the meal planner", "what do I need to
# do for …" — the work that takes a person
INTENT_MYLIST = re.compile(
    r"^\s*(?:what(?:'s| is) on my (?:own )?(?:list|to-?do(?: list)?)|"
    r"(?:show (?:me )?)?my (?:own )?(?:list|to-?do(?: list)?|tasks?|part)|"
    r"what do i (?:need|have) to do|what (?:are|is) my (?:tasks?|part))"
    r"(?:\s+(?:for|on|in|with)\s+(.+?))?\s*\??\s*$", re.I)
# "done y2 in the meal planner", "I've done y2 for …", "untick y2 in …"
INTENT_TICK = re.compile(
    r"^\s*(?:(?P<un>untick|not done|undo)|i(?:'ve| have)? (?:done|finished|"
    r"completed)|done|tick(?: off)?|finished|completed)\s+(?:item\s+)?"
    r"(?P<id>y\d+)\s+(?:in|for|on|of)\s+(?P<proj>.+?)\s*$", re.I)
INTENT_CHANGES = re.compile(
    r"^\s*(?:what(?:'s| has)? changed (?:in|on|with)|what did you "
    r"(?:do|change|build)(?: (?:in|on|to|for))?|(?:show (?:me )?)?(?:the )?"
    r"(?:changes|changelog|final report|report) (?:in|on|for|of))\s+(.+?)"
    r"\s*\??\s*$", re.I)
# "where is the meal planner", "where are the files for …", "where does
# … live" — the answer is a path and a link, not a guess
INTENT_WHERE = re.compile(
    r"^\s*where(?:'s|\s+is|\s+are|\s+does|\s+do|\s+did you (?:put|save))"
    r"\s+(.+?)\s*\??\s*$", re.I)
_WHERE_FILLER = re.compile(
    r"\b(?:the|my|code|files?|folder|directory|project|source|for|of|in|"
    r"from|stored|saved|kept|located|live|lives|living|at|is|are|it)\b", re.I)
# "open the folder for …", "open … in my editor", "open a terminal in …"
INTENT_DESKTOP = re.compile(
    r"^\s*(?:open|show|launch|start)\s+(?:me\s+)?(?:"
    r"(?:the\s+)?(?P<f1>folder|directory|file manager)\s+(?:for|of)\s+"
    r"(?P<p1>.+?)"
    r"|(?:a\s+|the\s+)?(?P<t1>terminal|shell|console)\s+(?:in|for|at)\s+"
    r"(?P<p2>.+?)"
    r"|(?P<p3>.+?)\s+in\s+(?:my\s+|the\s+|a\s+)?(?P<h3>file manager|"
    r"files app|editor|vs ?code|code|kate|zed|terminal|shell|console))"
    r"\s*$", re.I)
# "refine the meal planner", "review the code of …", "optimise …",
# "clean up … code" — a review-and-refine pass
INTENT_REFINE = re.compile(
    r"^\s*(?:please\s+)?(?:refine|optimi[sz]e|polish|tidy(?:\s+up)?|"
    r"clean\s+up|refactor|review(?:\s+and\s+refine)?)\s+"
    r"(?:(?:the|all|all\s+the|my)\s+)?(?:(?:whole\s+)?(?:code(?:base)?|"
    r"source)\s+(?:of|in|for)\s+)?(.+?)(?:'s)?(?:\s+(?:code(?:base)?|"
    r"source))?\s*$", re.I)
# "run the meal planner", "try out …", "launch …", "test …" — she never
# starts programs from a sentence; this opens the Run tab
INTENT_TRY = re.compile(
    r"^\s*(?:please\s+)?(?:run|launch|try(?:\s+out)?|execute|test|"
    r"run\s+the\s+(?:tests|checks)\s+(?:for|of|on))\s+(.+?)\s*$", re.I)
INTENT_NAVIGATE = re.compile(
    r"^\s*(?:open|show(?: me)?|go to|take me to|switch to|bring up)\s+"
    r"(?:the\s+)?(.+?)(?:\s+(?:page|tab|view|screen))?\s*$", re.I)
INTENT_BRAIN = re.compile(
    r"^\s*(?:(?:note|save|write|add)(?: this| that| it)? (?:to|in|into) "
    r"(?:my |the )?(?:second brain|brain|obsidian|vault)|brain note|"
    r"remember (?:this )?in (?:my |the )?(?:brain|obsidian|vault))\b"
    r"[:,]?\s*(.*)$", re.I | re.S)
INTENT_GITHUB = re.compile(
    r"^\s*(?:(?:push|publish|send)\s+(.+?)\s+to\s+github|(?:open|create|"
    r"make)\s+(?:a\s+)?(?:pr|pull\s+request)\s+(?:for|on)\s+(.+?))\s*$",
    re.I)
INTENT_TRIGGER = re.compile(
    r"^\s*(?:trigger|fire|run webhook|launch webhook)\s+([\w.-]+)"
    r"(?:\s*[:,]?\s*(.*))?$", re.I | re.S)
INTENT_FETCH = re.compile(
    r"^\s*(?:fetch|read|open|summari[sz]e|load)\s+(?:the\s+)?"
    r"(?:(?:web\s?)?page\s+|url\s+|site\s+|link\s+)?(?:at\s+)?"
    r"(https?://\S+|localhost(?::\d+)?(?:[/?#]\S*)?|"
    r"[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?)\s*$", re.I)
INTENT_SEARCH = re.compile(
    r"^\s*(?:please\s+)?(?:run\s+(?:a\s+)?)?(?:(?:web\s+)?search"
    r"(?:\s+the\s+(?:web|internet))?(?:\s+for)?|look\s+up|google|"
    r"find\s+out(?:\s+about)?)\s+(.+)$", re.I | re.S)
INTENT_NO_WEB = re.compile(
    r"\b(?:search|look\s+up|browse|google|scrape)\b"
    r"|\bfind\b.*\b(?:online|web|internet|compan(?:y|ies)|jobs?|postings?|"
    r"news|prices?)\b", re.I | re.S)
INTENT_RUNISH = re.compile(r"^\s*(?:run|execute|start)\s+\S+", re.I)

VIEWS = {"chat": "chat", "conversation": "chat", "home": "chat",
         "projects": "projects", "project": "projects", "kanban": "projects",
         "task board": "projects", "tasks": "projects", "gantt": "projects",
         "gantt chart": "projects", "timeline": "projects",
         "board": "board", "pipeline": "board", "pipeline board": "board",
         "inbox": "board", "ideas": "board",
         "agents": "agents", "agent": "agents", "missions": "agents",
         "team": "agents", "skills": "agents",
         "orb": "orb", "focus mode": "orb", "voice": "orb",
         "settings": "settings", "preferences": "settings"}
PROJECT_TABS = {"gantt": "gantt", "gantt chart": "gantt", "timeline": "gantt",
                "kanban": "kanban", "task board": "kanban", "board": "kanban",
                "files": "files", "file": "files", "code": "files",
                "source": "files", "source code": "files",
                "run": "run", "run tab": "run", "tests": "run",
                "review": "brief", "code review": "brief",
                "changes": "changes", "changelog": "changes",
                "report": "report", "final report": "report",
                "live": "live", "activity": "live",
                "assessment": "brief", "plan": "kanban"}


def command_phrases(cfg=None):
    """The phrases the app executes, given what is switched on — one list
    feeds the persona's prompt and the "that's not a command" reply."""
    cfg = cfg or config()
    out = ["capture <idea>", "run triage", "develop the plans",
           "approve <project>", "work on <project>",
           "stop working on <project>", "tell <project> to <guidance>",
           "approve task <id> in <project>: <your decision>",
           "skip task <id> in <project>",
           "what needs me", "status of <project>",
           "business plan for <project>", "what's on my list",
           "done <id> in <project>",
           "how far along is <project>",
           "what's left on <project>", "what changed in <project>",
           "where is <project>", "open the folder for <project>",
           "open <project> in my editor", "refine <project>",
           "run <project>",
           "ask the <agent> to <task>", "mission <agent> to <task>",
           "use skill <name>", "drop skill",
           "open <chat|projects|board|agents|settings>"]
    if cfg["webhooks"]:
        out.append("trigger <webhook>")
    if cfg["web_access"]:
        out += ["search for <topic>", "read <url>"]
    if cfg["obsidian_vault"]:
        out.append("note to brain: <text>")
    if cfg["github_access"]:
        out.append("push <project> to github")
    return out


def known_commands_text():
    cfg = config()
    agents = ", ".join(a["name"] for a in list_agents()) or "none defined"
    hooks = ", ".join(sorted(cfg["webhooks"])) or "none configured"
    projects = ", ".join(project_slugs()) or "none yet"
    return ("The commands I actually execute: "
            + "; ".join(command_phrases(cfg))
            + f". Agents: {agents}. Projects: {projects}. Webhooks: {hooks}. "
              "Everything else is just conversation.")


def _nav_target(phrase):
    """('view', tab, slug) for a spoken destination, or None."""
    phrase = " ".join(re.sub(r"[^a-z0-9 -]", " ", phrase.lower()).split())
    m = re.match(r"^(.+?)\s+(?:for|of|on)\s+(.+)$", phrase)
    if m and m.group(1) in PROJECT_TABS:
        slug = match_project(m.group(2))
        if slug:
            return "projects", PROJECT_TABS[m.group(1)], slug
    if phrase in VIEWS:
        return VIEWS[phrase], PROJECT_TABS.get(phrase, ""), ""
    for tab in PROJECT_TABS:                 # "<project> gantt"
        if phrase.endswith(" " + tab):
            slug = match_project(phrase[: -len(tab) - 1])
            if slug:
                return "projects", PROJECT_TABS[tab], slug
    slug = match_project(phrase)
    if slug:
        return "projects", "", slug
    return None


# "Aria, what needs me?" — being addressed by name is not part of the
# command. A speech recogniser hears the name as "area" or "arya" often
# enough to allow those too, but only where they can be nothing else: after
# a greeting, or set off by punctuation.
_GREETING = r"(?:hey|hi|hello|ok|okay)"
_NAME_END = r"(?=[\s,.:;!?—–]|$)[\s,.:;!—–-]*"
_ADDRESSED = re.compile(
    rf"^\s*(?:(?:{_GREETING}[\s,]+)?(?:a\.r\.i\.a\.?|aria){_NAME_END}"
    rf"|{_GREETING}[\s,]+(?:area|arya|ariya){_NAME_END}"
    r"|(?:area|arya|ariya)\s*[,.:;!—–]+\s*"
    rf"|{_GREETING}\s*[,.:;!—–]+\s*)", re.I)


def _unaddressed(text):
    """A sentence without the "Aria, " (or "hey Aria — ") it opens with."""
    rest = _ADDRESSED.sub("", text, count=1)
    return rest if rest.strip() else text


def detect_intent(text):
    """Deterministic spoken-command detection, checked before the LLM.

    People do not talk in command syntax, and a speech recogniser writes
    down what it hears as a sentence: "Aria, what needs me." So the name
    she is addressed by is set aside first, and a sentence that is no
    command with its full stop is tried again without it. (Tried again,
    not stripped up front: the words of a capture or a note are the
    owner's and stay exactly as given.)"""
    cfg = config()
    text = _unaddressed((text or "").strip())
    found = _detect(text, cfg)
    if found is None or found["action"] == "no-project":
        bare = text.rstrip(" \t.!…")
        if bare and bare != text:
            found = _detect(bare, cfg) or found
    return found


def _detect(text, cfg):
    m = INTENT_SKILL_ON.match(text)
    if m:
        return {"action": "use-skill", "name": m.group(1).lower()}
    if INTENT_SKILL_OFF.match(text):
        return {"action": "drop-skill"}
    m = INTENT_TRIGGER.match(text)
    if m and m.group(1).lower() in cfg["webhooks"]:
        return {"action": "webhook", "name": m.group(1).lower(),
                "message": (m.group(2) or "").strip()}
    m = INTENT_FETCH.match(text)
    if m and (m.group(1).lower().startswith(("http", "localhost"))
              or "/" in m.group(1)
              or re.search(r"\.(?:com|org|net|io|dev|ai|co|uk|gov|edu|app|"
                           r"info|me|us|de|fr|ca|au|wiki)\b", m.group(1), re.I)):
        return {"action": "fetch" if cfg["web_access"] else "no-web",
                "url": m.group(1)}
    m = INTENT_BRAIN.match(text)
    if m:
        return {"action": "brain-note", "text": m.group(1).strip()}
    m = INTENT_GITHUB.match(text)
    if m:
        slug = match_project(m.group(1) or m.group(2))
        if slug:
            return {"action": "github-push", "slug": slug}
    # commands that name a project: an unmatched name gets a straight "no
    # such project" rather than a chat model's improvisation
    m = INTENT_TASK.match(text)
    if m:
        task_id = (m.group(2) or m.group(3)).lower()
        task_id = task_id if task_id.startswith("t") else "t" + task_id
        if m.group(4):
            slug = match_project(m.group(4))
            if not slug:
                return {"action": "no-project", "said": m.group(4).strip()}
        else:                 # no project named: the one where it is waiting
            holding = [p["slug"] for p in list_projects()
                       if any(g["id"] == task_id for g in p["needs_you"])]
            slug = holding[0] if len(holding) == 1 else ""
        return {"action": "task-decision", "slug": slug, "task": task_id,
                "among": [] if slug or m.group(4) else holding,
                "approve": not re.match(r"skip|decline|reject|refuse",
                                        m.group(1), re.I),
                "note": (m.group(5) or "").strip()}
    m = INTENT_APPROVE.match(text)
    if m:
        slug = match_project(m.group(1))
        if slug:
            return {"action": "approve", "slug": slug,
                    "start": bool(m.group(2))}
        return {"action": "no-project", "said": m.group(1).strip()}
    m = INTENT_AUTOPILOT_STOP.match(text)
    if m:
        slug = match_project(m.group(1))
        if slug:
            return {"action": "autopilot-stop", "slug": slug}
        return {"action": "no-project", "said": m.group(1).strip()}
    m = INTENT_AUTOPILOT.match(text)
    if m:
        slug = match_project(m.group(1))
        if slug:
            return {"action": "autopilot-start", "slug": slug}
        return {"action": "no-project", "said": m.group(1).strip()}
    agent_names = {a["name"] for a in list_agents() + list_library_agents()}
    m = INTENT_MISSION.match(text)
    if m and m.group(1).lower() in agent_names:
        return {"action": "mission", "name": m.group(1).lower(),
                "task": m.group(2).strip()}
    m = INTENT_AGENT.match(text)
    if m and m.group(1).lower() in agent_names:
        return {"action": "agent", "name": m.group(1).lower(),
                "task": m.group(2).strip()}
    m = INTENT_STEER.match(text)
    if m and m.group(2).strip():
        slug = match_project(m.group(1))
        if slug:
            return {"action": "steer", "slug": slug,
                    "text": m.group(2).strip()}
    if INTENT_STATUS_ALL.match(text):
        return {"action": "status", "slug": ""}
    if INTENT_NEEDS.match(text):
        return {"action": "needs"}
    m = INTENT_TICK.match(text)
    if m:
        slug = match_project(m.group("proj"))
        if slug:
            return {"action": "tick", "slug": slug,
                    "id": m.group("id").lower(), "done": not m.group("un")}
        return {"action": "no-project", "said": m.group("proj").strip()}
    m = INTENT_MYLIST.match(text)
    if m:
        slug = match_project(m.group(1)) if m.group(1) else ""
        if m.group(1) and not slug:
            return {"action": "no-project", "said": m.group(1).strip()}
        return {"action": "mylist", "slug": slug or ""}
    m = INTENT_BUSINESS.match(text)
    if m:
        slug = match_project(m.group(1))
        if slug:
            return {"action": "business", "slug": slug}
        return {"action": "no-project", "said": m.group(1).strip()}
    if INTENT_PROGRESS_ALL.match(text):
        return {"action": "progress", "slug": ""}
    m = INTENT_PROGRESS.match(text)
    if m:
        slug = match_project(m.group(1))
        if slug:
            return {"action": "progress", "slug": slug}
    m = INTENT_CHANGES.match(text)
    if m:
        slug = match_project(m.group(1))
        if slug:
            return {"action": "changes", "slug": slug}
    m = INTENT_STATUS.match(text)
    if m:
        slug = match_project(m.group(1))
        if slug:
            return {"action": "status", "slug": slug}
        if re.match(r"\s*(?:status|progress)\b", text, re.I):
            return {"action": "no-project", "said": m.group(1).strip()}
    m = INTENT_REFINE.match(text)
    if m and not re.search(r"\b(?:plan|proposal|research|requirements|idea|"
                           r"report|changes)\b", m.group(1), re.I):
        slug = match_project(m.group(1))      # "review the plan for x" is
        if slug:                              # a conversation, not this
            return {"action": "refine", "slug": slug}
    m = INTENT_DESKTOP.match(text)
    if m:
        said = m.group("p1") or m.group("p2") or m.group("p3")
        slug = match_project(said)
        if slug:
            what = (m.group("h3") or "").lower()
            how = ("folder" if m.group("f1") or what in ("file manager",
                                                         "files app")
                   else "terminal" if m.group("t1") or what in (
                       "terminal", "shell", "console") else "editor")
            return {"action": "open-desktop", "slug": slug, "how": how}
    m = INTENT_WHERE.match(text)
    if m:
        slug = match_project(" ".join(_WHERE_FILLER.sub(
            " ", m.group(1)).split()))
        if slug:
            return {"action": "where", "slug": slug}
    m = INTENT_NAVIGATE.match(text)
    if m:
        target = _nav_target(m.group(1))
        if target:
            return {"action": "navigate", "view": target[0],
                    "tab": target[1], "slug": target[2]}
    m = INTENT_CAPTURE.match(text)
    if m and m.group(1).strip():
        return {"action": "capture", "text": m.group(1).strip()}
    if INTENT_TRIAGE.search(text):
        return {"action": "triage"}
    if INTENT_DEVELOP.search(text):
        return {"action": "develop"}
    if INTENT_NO_WEB.search(text):
        if cfg["web_access"]:
            m = INTENT_SEARCH.match(text)
            return {"action": "web-search",
                    "query": (m.group(1) if m else text).strip(" ?.")}
        return {"action": "no-web"}
    m = INTENT_TRY.match(text)
    if m:
        slug = match_project(m.group(1))
        if slug:
            return {"action": "navigate", "view": "projects", "tab": "run",
                    "slug": slug, "say": (
                        f"Opening the Run tab for {slug} — the command is "
                        "shown there and starts when you press Run. I never "
                        "start programs from a sentence.")}
    if INTENT_RUNISH.match(text):
        return {"action": "explain-commands"}
    return None


NO_WEB_SAY = ("Straight answer: web access is switched off, so I can't "
              "search or fetch live data — and I won't pretend to. Turn it "
              "on in Settings, or say 'capture <the idea>' and I'll draft a "
              "plan with research leads marked unverified.")


def perform(intent, source="chat"):
    """Carry out a spoken command that needs no streaming. Returns
    {"say": spoken reply, "action": name, …extras for the UI}, or None for
    intents the caller has to run itself (pipeline stages, agents, web
    answers — they stream)."""
    action = intent["action"]
    out = {"action": action}
    if action == "capture":
        path = capture_note(intent["text"], source=source)
        auto = config()["auto_pipeline"]
        out.update(file=path.name, auto=auto,
                   say=f"Captured — filed as {path.name}."
                       + (" Auto-pipeline is on, so I'm triaging it now."
                          if auto else ""))
    elif action == "use-skill":
        try:
            skill_text(intent["name"])           # validates it exists
            out.update(name=intent["name"],
                       say=f"Skill {intent['name']} loaded — it stays "
                           "active until you say 'drop skill'.")
        except ValueError as exc:
            out.update(action="drop-skill", say=str(exc))
    elif action == "drop-skill":
        out["say"] = "Skill unloaded."
    elif action == "webhook":
        try:
            status = trigger_webhook(intent["name"],
                                     intent.get("message", ""))
            out.update(name=intent["name"],
                       say=f"Triggered {intent['name']} — it answered "
                           f"{status}.")
        except Exception as exc:
            out["say"] = f"Couldn't trigger {intent['name']}: {exc}"
    elif action == "no-web":
        out["say"] = NO_WEB_SAY
    elif action == "no-project":
        names = [p["slug"].replace("-", " ") for p in list_projects()]
        out["say"] = (f"I don't have a project matching “{intent['said']}”. "
                      + ("Your projects are: " + ", ".join(names) + "."
                         if names else "There are no projects yet — "
                         "capture an idea and I'll plan one."))
    elif action == "explain-commands":
        out["say"] = "That's not a command I have. " + known_commands_text()
    elif action == "approve":
        slug = intent["slug"]
        try:
            approve_project(slug, via=source)
            say = f"Approved — {slug}'s plan is now its requirements."
            if intent.get("start"):
                autopilot_start(slug, via=source)
                say += (" Autopilot is engaged; I'll assess it, plan the "
                        "work and keep going until you tell me to stop.")
            else:
                say += f" Say 'work on {slug}' when you want me to start."
            out.update(slug=slug, say=say)
        except ValueError as exc:
            out.update(slug=slug, say=str(exc))
    elif action == "task-decision":
        slug, task_id = intent["slug"], intent["task"]
        owed = [f"{g['id']} in {p['slug']} ({g['title']})"
                for p in list_projects() for g in p["needs_you"]]
        nothing = (f"No task {task_id} is waiting on you"
                   + (f" in {slug}" if slug else "") + ". "
                   + ("Waiting on you: " + "; ".join(owed[:4]) + "."
                      if owed else "Nothing is waiting on you right now."))
        if len(intent.get("among") or []) > 1:
            out["say"] = (f"{task_id} is waiting in more than one project "
                          f"({', '.join(intent['among'])}) — say which: "
                          f"'approve task {task_id} in "
                          f"{intent['among'][0]}'.")
        elif not slug:
            out["say"] = nothing
        else:
            try:
                done = autopilot_decide(slug, task_id,
                                        approve=intent["approve"],
                                        note=intent.get("note", ""),
                                        via=source)
                out.update(slug=slug, task=task_id, say=(
                    f"Approved — “{done['title']}” can run now"
                    + (f", following your decision: {done['note']}"
                       if done["note"] else "") + "."
                    if intent["approve"] else
                    f"Skipped — “{done['title']}” won't be done, and I'll "
                    "plan around it."))
            except ValueError:
                out.update(slug=slug, say=nothing)
    elif action == "autopilot-start":
        slug = intent["slug"]
        try:
            autopilot_start(slug, via=source)
            out.update(slug=slug, say=(
                f"Autopilot engaged on {slug}. I'll take stock of what "
                "needs doing, plan it with the right agents, skills and "
                "models, then work the tasks one by one and keep iterating "
                f"until you say 'stop working on {slug}'."))
        except NeedsApproval as exc:
            out.update(action="needs-approval", slug=slug, say=str(exc))
        except ValueError as exc:
            out.update(slug=slug, say=str(exc))
    elif action == "autopilot-stop":
        autopilot_stop(intent["slug"])
        out.update(slug=intent["slug"], say=(
            f"Standing down on {intent['slug']} — I'll finish the task in "
            "flight, write the report of everything I changed and why, and "
            "leave the project as it is."))
    elif action == "steer":
        autopilot_steer(intent["slug"], intent["text"], via=source)
        out.update(slug=intent["slug"], say=(
            f"Noted for {intent['slug']} — I'll plan around that from the "
            "next task on."))
    elif action == "status":
        out.update(slug=intent.get("slug", ""),
                   say=project_status_text(intent["slug"])
                   if intent.get("slug") else workspace_status_text())
        if intent.get("slug"):
            out["where"] = str(_project_dir(intent["slug"]))
    elif action == "needs":
        out["say"] = workspace_needs_text()
    elif action == "mylist":
        out.update(slug=intent.get("slug", ""),
                   say=owner_list_text(intent.get("slug", "")))
    elif action == "tick":
        try:
            item = project_action_done(intent["slug"], intent["id"],
                                       done=intent["done"])
            left = sum(1 for t in project_actions(intent["slug"])["you"]
                       if not t.get("done"))
            out.update(slug=intent["slug"], say=(
                ("Ticked " if intent["done"] else "Unticked ")
                + f"{item['id']} — {item['title']}. "
                + (f"{_count(left, 'thing')} left on your list for "
                   f"{intent['slug'].replace('-', ' ')}." if left else
                   "That was the last thing on your list for "
                   f"{intent['slug'].replace('-', ' ')}.")))
        except ValueError as exc:
            out["say"] = str(exc)
    elif action == "business":
        try:
            business_start(intent["slug"])
            out.update(slug=intent["slug"], say=(
                f"Writing the business plan for "
                f"{intent['slug'].replace('-', ' ')} now — the plan, what "
                "a complete proposal still needs, and who does what. It "
                "takes a few minutes; you'll find it on the project's "
                "Business tab."))
        except ValueError as exc:
            out["say"] = str(exc)
    elif action == "progress":
        out.update(slug=intent.get("slug", ""),
                   say=project_progress_text(intent["slug"])
                   if intent.get("slug") else workspace_progress_text())
    elif action == "where":
        slug = intent["slug"]
        proj = _project_dir(slug)
        kind = project_kind(proj)
        out.update(slug=slug, where=str(proj), say=(
            f"{slug.replace('-', ' ')} lives in {proj}. "
            + ("That is its own workspace — everything I write for it goes "
               "there." if kind != "potential" else
               "It is still a proposal: the plan and research are there; it "
               "gets its own workspace when you approve it.")
            + " The Files tab on its Projects page shows every file, and "
              "opens the folder or your editor."))
    elif action == "open-desktop":
        slug = intent["slug"]
        if source in ("integration", "voice"):
            out.update(slug=slug, say="I can only open things on your "
                       "screen from the app itself.")
        else:
            try:
                did = open_path(slug, how=intent["how"])
                out.update(slug=slug, where=str(_project_dir(slug)),
                           say=did[0].upper() + did[1:] + ".")
            except ValueError as exc:
                out.update(slug=slug, say=str(exc))
    elif action == "changes":
        out.update(slug=intent["slug"],
                   say=project_changes_text(intent["slug"]),
                   files=sorted({f["path"] for r in project_changes(
                       intent["slug"])[-12:] for f in r.get("files", [])}),
                   where=str(_project_dir(intent["slug"])))
    elif action == "navigate":
        names = {"chat": "the chat", "projects": "Projects",
                 "board": "the pipeline board", "agents": "Agents",
                 "settings": "Settings"}
        out.update(view=intent["view"], tab=intent.get("tab", ""),
                   slug=intent.get("slug", ""),
                   say=intent.get("say") or (
                       "Opening " + names.get(intent["view"], intent["view"])
                       + (f" for {intent['slug']}" if intent.get("slug")
                          else "") + "."))
    elif action == "refine":
        slug = intent["slug"]
        try:
            autopilot_refine(slug, via=source)
            out.update(slug=slug, say=(
                f"I'll read through all of {slug}'s code, write up what is "
                "worth changing, and make the refinements that leave its "
                "behaviour exactly as it is — each one judged on its diff"
                + (" and on the project's own tests"
                   if checks_available()[0] else "")
                + ". The review lands on the Brief tab."))
        except NeedsApproval as exc:
            out.update(action="needs-approval", slug=slug, say=str(exc))
        except ValueError as exc:
            out.update(slug=slug, say=str(exc))
    elif action == "brain-note":
        if not intent.get("text"):
            out["say"] = ("Tell me what to write — for example: note to "
                          "brain: the feeder needs a solar panel.")
        else:
            try:
                path = brain_note(intent["text"], source=source)
                out.update(path=str(path),
                           say=f"Written to your brain as “{path.stem}”.")
            except BrainError as exc:
                out["say"] = str(exc)
    elif action == "github-push":
        try:
            info = project_push(intent["slug"])
            out.update(slug=intent["slug"], url=info.get("pr", ""),
                       say=info["say"])
        except ValueError as exc:
            out.update(slug=intent["slug"], say=str(exc))
        except aria_git.GitError as exc:
            out.update(slug=intent["slug"], say=f"GitHub push failed: {exc}")
    elif action == "mission":
        try:
            path = mission_create(intent["name"], intent["task"])
            out.update(name=intent["name"], id=path.stem, say=(
                f"Mission queued for the {intent['name']} — it runs in the "
                "background even if you close this tab."))
        except ValueError as exc:
            out["say"] = str(exc)
    else:
        return None
    return out


def _busy_reply(waited, target):
    """What to say when the chat model has not produced a word in time:
    the truth about why, where things stand, and what still works."""
    busy = [p for p in list_projects()
            if p["autopilot"] == "running" and not p["waiting"]]
    if busy:
        p = busy[0]
        doing = f"“{p['current']}”" if p["current"] else {
            "reviewing": "checking a task", "planning": "planning the next "
            "batch", "assessing": "taking stock", "reflecting": "updating "
            "my notes", "auditing": "auditing the work"}.get(
                p["phase"], "a step")
        why = (f"My models are tied up — I'm in the middle of {doing} on "
               f"{p['slug'].replace('-', ' ')} — so I can't think that one "
               "over just now.")
    else:
        why = (f"The model that answers chat ({target.get('model', '?')}) "
               f"has not said a word in {int(waited)} seconds — it may "
               "still be loading, or the model server is stuck.")
    return (why + " Where things stand: " + workspace_status_text()
            + " Commands are answered without a model, so these still "
              "work: “status”, “how far along is <project>”, “what changed "
              "in <project>”, “stop working on <project>”.")


def _patience():
    """How long a chat may wait for its first word. Short while autopilot
    holds the models (the wait would be for its whole step), longer when
    the only reason can be a cold model loading."""
    try:
        forced = float(os.environ.get("ARIA_CHAT_PATIENCE") or 0)
    except ValueError:
        forced = 0
    if forced:
        return forced
    return 25 if autopilot_busy() else 90


def converse(messages, on_token=None, skill=None, patience=None,
             on_status=None):
    """Persona chat (fast tier). With `skill`, that skill's instructions are
    loaded into the system prompt so any model — not just Claude — can
    follow them.

    A reply always comes back in bounded time: if the model has not begun
    to answer within `patience` seconds (default: see _patience) — it is
    busy with autopilot's own work, or still loading — she says so, and
    where things stand, instead of leaving the chat hanging."""
    cfg = config()
    last = next((m.get("content", "") for m in reversed(messages)
                 if m.get("role") == "user"), "")
    detail = ""
    slug = match_project(last) if isinstance(last, str) and last else None
    if slug:
        try:
            detail = (f"Detail on the project the user just mentioned — "
                      f"{project_status_text(slug)}\n")
        except Exception:
            detail = ""
    system = CHAT_SYSTEM.format(
        summary=board_summary_text(), detail=detail,
        web_fact=WEB_FACT_ON if cfg["web_access"] else WEB_FACT_OFF,
        commands=", ".join(f'"{c}"' for c in command_phrases(cfg)))
    if skill:
        system += (f"\n\nACTIVE SKILL: {skill}. Follow these skill "
                   "instructions when they apply (you cannot run scripts "
                   "or code they mention — say so if asked):\n\n"
                   + skill_text(skill)[:6000])
    trimmed = [m for m in messages if m.get("role") in ("user", "assistant")
               and isinstance(m.get("content"), str)][-16:]
    target = chat_target(cfg)
    patience = float(patience or _patience())
    box = {"parts": [], "error": None, "gave_up": False}
    first, done = threading.Event(), threading.Event()

    def token(text):
        if box["gave_up"]:
            return                       # nobody is listening any more
        box["parts"].append(text)
        first.set()
        if on_token:
            on_token(text)

    def thought(_text):
        # a model that reasons first is not a model that hangs
        if not first.is_set() and not box.get("thinking"):
            box["thinking"] = True
            if on_status and not box["gave_up"]:
                on_status("thinking it over…")
        box["thought_at"] = time.time()

    def work():
        try:
            # chat is conversation: with thinking on "auto" she answers
            # straight away, whichever model happens to serve the chat
            box["reply"] = chat_messages(
                target, [{"role": "system", "content": system}] + trimmed,
                on_token=token, on_thought=thought,
                think=None if cfg.get("thinking") == "always" else False)
        except Exception as exc:
            box["error"] = exc
        first.set()
        done.set()

    threading.Thread(target=work, daemon=True).start()
    t0 = time.time()
    told = False
    while not first.wait(2):
        waited = time.time() - t0
        if box.get("thinking") and time.time() - box.get("thought_at", 0) < 20:
            continue                     # reasoning as we wait: let her
        if waited >= patience:
            box["gave_up"] = True        # the call may end later, unheard
            reply = _busy_reply(waited, target)
            if on_token:
                on_token(reply)
            return reply
        if on_status and not told and waited >= 6:
            told = True
            on_status("the model is busy — waiting for it to answer…"
                      if autopilot_busy() else
                      "waiting for the model to wake up…")
    done.wait()
    if box["error"] is not None:
        raise box["error"]
    return strip_think(box.get("reply") or "".join(box["parts"]))


# ---------------------------------------------------------------- voice
#
# Speaking and listening happen on this machine (bin/aria_voice.py): the
# app's own window has no browser speech service to borrow.

AUDIO_SUFFIXES = {"audio/wav": ".wav", "audio/x-wav": ".wav",
                  "audio/wave": ".wav", "audio/webm": ".webm",
                  "audio/ogg": ".ogg", "audio/mp4": ".m4a",
                  "audio/mpeg": ".mp3"}


VOICE_TOKEN_FILE = ROOT / "system" / "voice-token"


def voice_token():
    """The shared secret of the voice gateway (the door for Alexa and other
    integrations): ARIA_VOICE_TOKEN, else system/voice-token — created on
    first use, readable only by the owner, ignored by git."""
    value = os.environ.get("ARIA_VOICE_TOKEN", "").strip()
    if value:
        return value
    if not VOICE_TOKEN_FILE.is_file():
        import secrets
        VOICE_TOKEN_FILE.write_text(secrets.token_urlsafe(32) + "\n",
                                    encoding="utf-8")
        os.chmod(VOICE_TOKEN_FILE, 0o600)
    return VOICE_TOKEN_FILE.read_text(encoding="utf-8").strip()


def voice_status():
    return aria_voice.status(config())


def voice_speak(text):
    """`text` spoken, as a WAV file (bytes). RuntimeError says what is
    missing when she has no voice here."""
    return aria_voice.speak(text, config())


def transcribe_audio(data, suffix=".wav", language=""):
    """Local speech-to-text. RuntimeError (with the command that fixes it)
    when no recogniser is installed."""
    return aria_voice.transcribe(data, config(), suffix=suffix,
                                 language=language)


def voice_install(say=print, voice=""):
    return aria_voice.install(config(), say=say, voice=voice)


# ---------------------------------------------------------------- skills

SKILL_DIRS = (ROOT / "skills",              # converted universal pack
              ROOT / ".claude" / "skills",    # shared with Claude Code
              ROOT / "skills-library")        # engine-only sources (ECC…)


def skill_text(name):
    """Instructions for a named skill: prefers the converted universal pack
    (skills/<name>/PROMPT.md), falls back to the raw skill as installed."""
    if not re.fullmatch(r"[\w.-]+", name) or ".." in name:
        raise ValueError(f"invalid skill name: {name}")
    for f in (SKILL_DIRS[0] / name / "PROMPT.md",
              SKILL_DIRS[1] / name / "SKILL.md",
              SKILL_DIRS[2] / name / "SKILL.md"):
        if f.is_file():
            text = f.read_text(encoding="utf-8")
            return parse_note(f)[2].strip() if f.name == "SKILL.md" else text
    raise ValueError(f"skill not found: {name} — run: aria install-skills "
                     "then aria skills convert")


def list_skill_names():
    names = set()
    for base in SKILL_DIRS:
        if base.is_dir():
            for d in base.iterdir():
                if (d / "PROMPT.md").is_file() or (d / "SKILL.md").is_file():
                    names.add(d.name)
    return sorted(names)


_SKILL_CACHE = {"sig": None, "rows": []}


def _unquote(value):
    value = (value or "").strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def skill_index():
    """[{name, description, execution}] for every installed skill."""
    idx = SKILL_DIRS[0] / "index.json"
    names = list_skill_names()
    sig = (idx.stat().st_mtime if idx.is_file() else 0,
           tuple(d.stat().st_mtime if d.is_dir() else 0 for d in SKILL_DIRS),
           len(names))
    if _SKILL_CACHE["sig"] == sig:
        return _SKILL_CACHE["rows"]
    rows = {}
    if idx.is_file():
        try:
            for r in json.loads(idx.read_text(encoding="utf-8")):
                rows[r["name"]] = {"name": r["name"],
                                   "description": _unquote(
                                       r.get("description", "")),
                                   "execution": r.get("execution", "")}
        except (ValueError, KeyError, TypeError):
            rows = {}
    for name in names:
        if name in rows:
            continue
        desc = ""
        for base in SKILL_DIRS[1:]:
            f = base / name / "SKILL.md"
            if f.is_file():
                desc = _unquote(parse_note(f)[0].get("description", ""))
                break
        rows[name] = {"name": name, "description": desc,
                      "execution": "as-installed"}
    _SKILL_CACHE.update(sig=sig, rows=sorted(rows.values(),
                                             key=lambda r: r["name"]))
    return _SKILL_CACHE["rows"]


def _stem(word):
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    for suffix in ("ing", "ers", "er", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            if len(word) > 3 and word[-1] == word[-2] \
                    and word[-1] not in "aeiouls":
                word = word[:-1]                 # planning -> plann -> plan
            break
    return word


# words that say nothing about WHICH specialist or skill a goal needs
_GENERIC = {_w for _w in """app application tool project system program
software simple small basic new good best better user command line file
data plan read write make build create run use work need want thing way
help base idea feature support add get set""".split()}


def _terms(text):
    out = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) > 2 and w not in _STOPWORDS:
            w = _stem(w)
            if w not in _GENERIC:
                out.append(w)
    return out


def rank_by_relevance(query, items, k=6, min_score=5.0, rel=0.4):
    """Rank [{name, description}] against free text: rarer shared words
    count for more, and a hit in the name counts triple. Precision over
    recall — a hit must clear `min_score` AND come within `rel` of the
    best hit, so a strong match is not diluted by word-coincidences. No
    embeddings: this has to run instantly on every plan."""
    import math
    q = set(_terms(query))
    if not q or not items:
        return []
    docs = [(it, set(_terms(it["name"].replace("-", " "))),
             set(_terms(it.get("description", "")))) for it in items]
    df = {}
    for _it, name_t, desc_t in docs:
        for t in name_t | desc_t:
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    scored = []
    for it, name_t, desc_t in docs:
        # the floor keeps scores meaningful on a small shelf, where every
        # word is "common" and plain idf would round everything to nothing
        score = sum(max(1.0, math.log(1 + n / df[t]))
                    * (3 if t in name_t else 1)
                    for t in q if t in name_t or t in desc_t)
        if score >= min_score:
            scored.append((score, it))
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    floor = scored[0][0] * rel
    return [dict(it, score=round(score, 2))
            for score, it in scored[:k] if score >= floor]


def relevant_skills(text, k=6, min_score=7.5, rel=0.4):
    """The installed skills most relevant to a piece of work."""
    return rank_by_relevance(text, skill_index(), k=k, min_score=min_score,
                             rel=rel)


# ---------------------------------------------------------------- agents
#
# Two shelves. Core agents are the markdown files in system/agents/ — her
# standing team, always on the roster. Library agents come from the repos
# `aria install-skills` cloned into vendor/ (e.g. ECC's agents/): dozens of
# specialists that are offered to the planner only when a project's goal
# makes them relevant.

AGENTS_DIR = ROOT / "system" / "agents"
VENDOR_DIR = ROOT / "vendor"

AGENT_HEADER = """You are {name}, a specialist agent inside A.R.I.A., a
personal idea-to-project system. Work autonomously on the task you are
given and reply with your finished work product in markdown — no preamble,
no questions back unless genuinely blocked. You cannot run code or use
tools yourself: reason from the task, your role, your skills and any
material provided with the task. {web_note} Flag anything that still needs
verification."""

WEB_NOTE_ON = ("Live web research is attached to the task — ground your "
               "work in it and cite it as [n].")
WEB_NOTE_OFF = ("You have no live web access for this task, so label "
                "recalled facts as unverified.")

_MODEL_TIERS = {"opus": "deep", "sonnet": "smart", "haiku": "fast",
                "inherit": "smart"}
_LIBRARY_CACHE = {"sig": None, "rows": []}


def _agent_from(path, library=False, source=""):
    fm, _, body = parse_note(path)
    skills = [s.strip() for s in
              re.split(r"[,\s]+", fm.get("skills", "").strip("[] "))
              if s.strip()]
    tools = [t.strip(" \"'").lower() for t in
             re.split(r"[,\s]+", fm.get("tools", "").strip("[] "))
             if t.strip(" \"'")]
    model = (fm.get("model") or "smart").split("#")[0].strip()
    body = body.strip()
    if library:
        model = _MODEL_TIERS.get(model.lower(), "smart")
        # boilerplate that tells the agent not to output code or links
        # would stop it producing the files a task asks for
        body = re.sub(r"## Prompt Defense Baseline\s*\n(?:\s*- .*\n)+", "",
                      body)
        body = re.sub(r"Claude Code", "the AI coding agent", body)
        body = re.sub(r"\bClaude\b", "the assistant", body).strip()
    return {
        "name": _unquote(fm.get("name", path.stem)).lower(),
        "role": _unquote(fm.get("role") or fm.get("description", "")),
        "skills": skills,
        "model": model or "smart",
        "web": fm.get("web", "").strip().lower() in ("true", "yes", "on")
               or "web" in tools or "websearch" in tools,
        "body": body,
        "path": str(path.relative_to(ROOT)),
        "library": library,
        "source": source,
    }


def list_agents():
    """Core agent definitions from system/agents/*.md (frontmatter: name,
    role, skills, model tier, web) — editable markdown, CrewAI-shaped on
    purpose."""
    if not AGENTS_DIR.is_dir():
        return []
    return [_agent_from(p) for p in sorted(AGENTS_DIR.glob("*.md"))]


def list_library_agents():
    """Specialist agents shipped by installed skill sources
    (vendor/<repo>/agents/*.md). Core agents win on a name clash."""
    if not VENDOR_DIR.is_dir():
        return []
    files = sorted(p for p in VENDOR_DIR.glob("*/agents/*.md")
                   if p.name.lower() != "readme.md")
    sig = (len(files), max((p.stat().st_mtime for p in files), default=0))
    if _LIBRARY_CACHE["sig"] == sig:
        return _LIBRARY_CACHE["rows"]
    core = {a["name"] for a in list_agents()}
    rows, seen = [], set()
    for p in files:
        try:
            agent = _agent_from(p, library=True, source=p.parts[-3])
        except Exception:
            continue
        if agent["name"] in core or agent["name"] in seen \
                or not agent["body"]:
            continue
        seen.add(agent["name"])
        rows.append(agent)
    _LIBRARY_CACHE.update(sig=sig, rows=rows)
    return rows


def get_agent(name):
    name = (name or "").lower().strip()
    return next((a for a in list_agents() + list_library_agents()
                 if a["name"] == name), None)


def relevant_agents(text, k=8, min_score=5.0, rel=0.45):
    """Library specialists whose name/description fits a piece of work."""
    return rank_by_relevance(
        text, [{"name": a["name"], "description": a["role"]}
               for a in list_library_agents()], k=k, min_score=min_score,
        rel=rel)


def agent_system(agent, skills=(), researched=False, budget=7000):
    """System prompt for an agent run: header, role, instructions and the
    text of its skills — the agent's own plus any the task assigned —
    trimmed to a character budget. Returns (text, names of skills loaded,
    names that were not installed)."""
    system = AGENT_HEADER.format(
        name=agent["name"],
        web_note=WEB_NOTE_ON if researched else WEB_NOTE_OFF)
    if agent["role"]:
        system += f"\n\nYour role: {agent['role']}"
    if agent["body"]:
        system += "\n\n" + clip(agent["body"], int(budget * 0.45))
    wanted = []
    for sk in list(agent["skills"]) + list(skills or ()):
        if sk and sk not in wanted:
            wanted.append(sk)
    loaded, missing = [], []
    texts = []
    for sk in wanted[:5]:
        try:
            texts.append((sk, skill_text(sk)))
        except ValueError:
            missing.append(sk)
    if texts:
        each = max(800, (budget - len(system)) // len(texts))
        for sk, text in texts:
            system += f"\n\n--- SKILL: {sk} ---\n" + clip(text, min(each, 4000))
            loaded.append(sk)
    return system, loaded, missing


def run_agent(name, task, emit, timeout=3600, model="", skills=None,
              web=None, max_seconds=0, info=None):
    """Run one agent on one task: role + skills + model.

    `model` overrides the agent's own `model:` — a tier (fast/smart/deep)
    or a direct backend/model spec like `ollama/gpt-oss:120b`. `skills`
    adds to the agent's own. `web` attaches live web research to the task
    (default: the agent's `web:` setting). Streams thinking via emit and
    returns the reply; `info`, when given, is filled with what was actually
    used: model, skills, sources."""
    agent = get_agent(name)
    if agent is None:
        raise ValueError(f"no such agent: {name} (defined in system/agents/)")
    cfg = config()
    mspec = (model or agent["model"] or "smart").strip()
    target = resolve_model(mspec, cfg)
    if target is None:
        emit({"event": "error",
              "log": f"model '{mspec}' has no configured backend — falling "
                     "back to smart"})
        target = resolve_tier("smart", cfg)
    elif target.get("fallback"):
        emit({"event": "error",
              "log": f"{target['fallback']} — using smart instead"})
    sources = []
    if (agent["web"] if web is None else web) and web_ready(cfg):
        found = research(task, emit=emit)
        if found["block"]:
            task += ("\n\nRESEARCH MATERIAL\n" + found["block"])
            sources = found["sources"]
    system, loaded, missing = agent_system(
        agent, skills, researched=bool(sources),
        budget=int(prompt_budget(cfg, mspec) * 0.5))
    for sk in missing:
        emit({"event": "error",
              "log": f"skill '{sk}' not installed — continuing without it"})
    if info is not None:
        info.update(model=target.get("spec", target["model"]),
                    skills=loaded, sources=sources)
    emit({"event": "start", "stage": "agent", "agent": agent["name"],
          "model": target.get("spec", target["model"]),
          "log": f"agent {agent['name']} ({target['model']}"
                 + (f", skills: {', '.join(loaded)}" if loaded else "")
                 + (", with web research" if sources else "")
                 + f") working on: {' '.join(task.split())[:120]}"})
    emit({"event": "thinking", "label": f"{agent['name']} thinking"})
    reply = chat_stream(target, task, system, timeout=timeout,
                        max_seconds=max_seconds,
                        on_token=lambda t: emit({"event": "token", "text": t}))
    said = last_call()
    if info is not None and said.get("cut_off"):
        info["cut_off"] = (
            f"the reply was cut off: {said.get('model') or 'the model'} ran "
            "out of room in its window"
            + (f" of {said['window']} tokens" if said.get("window") else "")
            + " before it had finished")
    emit({"event": "stage_done", "stage": "agent", "agent": agent["name"],
          "log": f"agent {agent['name']} finished"
                 + (" — but its reply was cut off (the model's window was "
                    "full)" if said.get("cut_off") else "")})
    return strip_think(reply)


def installed_models(cfg=None):
    """Every model the configured backends report having, as
    [{"spec": "backend/model", "size_gb": float|None}] — what the planner
    may assign beyond the three tiers."""
    cfg = cfg or config()
    now = time.time()
    if _MODELS_CACHE["at"] and now - _MODELS_CACHE["at"] < 120:
        return _MODELS_CACHE["rows"]
    rows = []
    for backend, url in cfg["backend_urls"].items():
        try:
            if backend == "ollama":
                with urllib.request.urlopen(url + "/api/tags", timeout=4) as r:
                    for m in json.load(r).get("models", []):
                        size = m.get("size")
                        rows.append({"spec": f"ollama/{m.get('name', '')}",
                                     "size_gb": round(size / 1e9, 1)
                                     if size else None})
            else:
                req = urllib.request.Request(
                    url + "/v1/models", headers={
                        "Authorization": "Bearer " + (
                            os.environ.get(f"ARIA_KEY_{backend.upper()}")
                            or cfg["backend_keys"].get(backend) or "local")})
                with urllib.request.urlopen(req, timeout=4) as r:
                    for m in json.load(r).get("data", []):
                        rows.append({"spec": f"{backend}/{m.get('id', '')}",
                                     "size_gb": None})
        except Exception:
            continue
    _MODELS_CACHE.update(at=now, rows=rows)
    return rows


_MODELS_CACHE = {"at": 0, "rows": []}


# ---------------------------------------------------------------- missions

MISSIONS = ROOT / "pipeline" / "missions"


def mission_create(agent_name, task):
    """Queue a background job for an agent. Missions are plain markdown
    files; a worker (in the UI server, or `aria missions run`) executes
    them one at a time and appends the output — so a giant model can think
    for hours with no browser tab open."""
    agent_name = agent_name.lower().strip()
    if get_agent(agent_name) is None:
        raise ValueError(f"no such agent: {agent_name}")
    task = task.strip()
    if not task:
        raise ValueError("empty task")
    MISSIONS.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    path = unique_path(MISSIONS / f"{now:%Y%m%d-%H%M}-{agent_name}.md")
    path.write_text(
        f"---\nid: {path.stem}\nagent: {agent_name}\nstatus: queued\n"
        f"created: {now:%Y-%m-%dT%H:%M}\nstarted:\nfinished:\n---\n\n"
        f"## Task\n\n{task}\n", encoding="utf-8")
    return path


def mission_list():
    out = []
    if MISSIONS.is_dir():
        for p in sorted(MISSIONS.glob("*.md")):
            fm, _, body = parse_note(p)
            m = re.search(r"## Task\s*\n+(.*?)(?=\n## |\Z)", body, re.S)
            out.append({"id": p.stem, "path": str(p.relative_to(ROOT)),
                        "agent": fm.get("agent", "?"),
                        "status": fm.get("status", "?"),
                        "created": fm.get("created", ""),
                        "finished": fm.get("finished", ""),
                        "task": " ".join((m.group(1) if m else "").split())[:160]})
    return out


def mission_run_next(emit):
    """Execute the oldest queued mission (blocking). Returns its id, or
    None when the queue is empty. Output is appended to the mission file;
    failures are recorded, never lost. If a webhook named 'mission-done'
    is configured, it is triggered on completion."""
    queued = [m for m in mission_list() if m["status"] == "queued"]
    if not queued:
        return None
    m = queued[0]
    path = ROOT / m["path"]
    fm, _, body = parse_note(path)
    task_m = re.search(r"## Task\s*\n+(.*?)(?=\n## |\Z)", body, re.S)
    task = (task_m.group(1) if task_m else "").strip()
    update_frontmatter(path, status="running",
                       started=f"{datetime.datetime.now():%Y-%m-%dT%H:%M}")
    emit({"event": "start", "stage": "mission", "mission": m["id"],
          "log": f"mission {m['id']} started ({m['agent']})"})
    try:
        reply = run_agent(m["agent"], task, emit, timeout=7 * 24 * 3600)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## Output\n\n{reply.strip()}\n")
        update_frontmatter(path, status="done",
                           finished=f"{datetime.datetime.now():%Y-%m-%dT%H:%M}")
        emit({"event": "stage_done", "stage": "mission", "mission": m["id"],
              "log": f"mission {m['id']} done"})
    except Exception as exc:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## Output\n\nFAILED: {exc}\n")
        update_frontmatter(path, status="failed",
                           finished=f"{datetime.datetime.now():%Y-%m-%dT%H:%M}")
        emit({"event": "error", "log": f"mission {m['id']} failed: {exc}"})
    try:
        if "mission-done" in config()["webhooks"]:
            trigger_webhook("mission-done",
                            f"mission {m['id']} ({m['agent']}) finished")
    except Exception:
        pass  # notification is best-effort, never fails a mission
    return m["id"]


def mission_requeue_stale():
    """A mission left 'running' by a crashed/restarted server restarts
    from scratch (generations can't be checkpointed)."""
    fixed = []
    for m in mission_list():
        if m["status"] == "running":
            update_frontmatter(ROOT / m["path"], status="queued", started="")
            fixed.append(m["id"])
    return fixed


# ---------------------------------------------------------------- projects
#
# A project is a folder: a potential one under pipeline/02-potential-
# projects/<slug>/ until its plan is approved, an active one under the
# promotion target afterwards. This section is the registry (find, list,
# describe) and the human gate (approve -> promote). The autonomous loop
# that works a project lives in aria_autopilot.

class NeedsApproval(ValueError):
    """Autopilot was asked to start on a plan no human has approved."""


def promotion_dir(cfg=None):
    target = Path(os.path.expanduser((cfg or config())["promotion_target"]))
    return target if target.is_absolute() else (ROOT / target).resolve()


def _project_dir(slug):
    slug = (slug or "").strip()
    if not re.fullmatch(r"[\w.-]+", slug) or ".." in slug:
        raise ValueError(f"no project found for slug: {slug}")
    for base in (promotion_dir() / slug, POTENTIAL / slug):
        if base.is_dir():
            return base
    raise ValueError(f"no project found for slug: {slug}")


def project_kind(proj):
    return "potential" if proj.parent == POTENTIAL else "active"


def _auto_dir(slug, create=True):
    """Where autopilot keeps a project's state. Adopted codebases use a
    hidden .aria/ so her bookkeeping stays out of their tree."""
    proj = _project_dir(slug)
    d = proj / ".aria" if (proj / ".aria").is_dir() else proj / "autopilot"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _auto_state(slug):
    f = _auto_dir(slug, create=False) / "state.json"
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"status": "idle", "iteration": 0}


def project_requirements(proj):
    return _project_doc(proj, "requirements")


def project_approved(slug):
    """Has a human approved this project's plan? (requirements.md with
    approved: true — or a goal the owner stated when adopting a codebase)"""
    proj = _project_dir(slug)
    if (proj / ".aria" / "GOAL.md").is_file():
        return True
    req = project_requirements(proj)
    return bool(req) and parse_note(req)[0].get("approved", "") \
        .strip().lower() == "true"


def _project_goal(slug, limit=2500):
    proj = _project_dir(slug)
    for f in (proj / ".aria" / "GOAL.md",
              _project_doc(proj, "requirements"),
              _project_doc(proj, "proposed-plan"),
              _project_doc(proj, "idea")):
        if f and f.is_file():
            return clip(parse_note(f)[2].strip() or
                        f.read_text(encoding="utf-8"), limit)
    return f"(no requirements found — infer the goal from the slug: {slug})"


def _section(text, heading):
    """Body of a `## heading` section (case-insensitive prefix match)."""
    m = re.search(rf"^##\s*{re.escape(heading)}[^\n]*\n(.*?)(?=^##\s|\Z)",
                  text, re.S | re.M | re.I)
    return m.group(1).strip() if m else ""


def _project_title(slug, plan_text=""):
    m = re.search(r"^#\s*(?:Proposed plan|Requirements):\s*(.+)$",
                  plan_text, re.M)
    return m.group(1).strip() if m else slug.replace("-", " ").title()


def approve_project(slug, via="ui"):
    """THE HUMAN GATE. Records the owner's approval by turning the proposed
    plan — exactly as it stands — into requirements.md (approved: true).
    Only ever called from an explicit human action: the Approve button, the
    spoken/typed "approve <project>", or `aria approve`. Returns the path."""
    proj = _project_dir(slug)
    existing = project_requirements(proj)
    if existing and project_approved(slug):
        return existing
    plan = _project_doc(proj, "proposed-plan")
    if not plan:
        raise ValueError(f"{slug} has no proposed plan to approve yet — "
                         "run develop first")
    fm, _, body = parse_note(plan)
    today = datetime.date.today().isoformat()
    scope = _section(body, "Scope")
    scope_in = re.search(r"\*\*In:?\*\*:?\s*(.*?)(?=\*\*Out|\Z)", scope, re.S)
    scope_out = re.search(r"\*\*Out[^*]*\*\*:?\s*(.*)", scope, re.S)
    pitch = plan_pitch(proj)
    parts = [
        f"# Requirements: {_project_title(slug, body)}", "",
        f"> Approved by the owner on {today} (via {via}). This is the "
        "proposed plan exactly as it stood at approval — the contract "
        "that autopilot and any project manager execute.", ""]

    def add(title, text):
        if text and text.strip():
            parts.extend([f"## {title}", "", text.strip(), ""])

    add("Goal", "\n\n".join(x for x in (
        pitch, _section(body, "What we're building")) if x))
    add("Non-goals", scope_out.group(1) if scope_out else "")
    add("Deliverables", scope_in.group(1) if scope_in
        else (scope if not scope_out else ""))
    add("Milestones & acceptance criteria", _section(body, "Milestones"))
    add("Approach & constraints", _section(body, "Recommended approach"))
    add("Recommended team", _section(body, "Recommended team"))
    decisions = _section(body, "Decisions needed")
    add("Open decisions", (decisions + "\n\n*(Approved with these still "
                           "open — settle them with a steering note or in "
                           "review.)*") if decisions else "")
    add("Risks", _section(body, "Risks"))
    if len(parts) <= 4:                  # the plan had no usable sections
        add("Goal", body.strip())
    actions = project_actions(slug)
    if actions and actions["aria"]:
        # what she listed for herself beside the build is approved with it
        add("Business groundwork", (
            "Beside the build, these are produced as documents under "
            "`business/` — each one complete enough to act on:\n\n"
            + "\n".join(f"- **{a['title']}** — {a['detail']}"
                        for a in actions["aria"])))
    dest = plan.parent / "requirements.md"
    dest.write_text(
        f"---\nslug: {slug}\nstatus: approved\napproved: true\n"
        f"approved_on: {today}\napproved_via: {via}\n---\n\n"
        + "\n".join(parts).rstrip() + "\n", encoding="utf-8")
    update_frontmatter(plan, status="approved")
    idea = _project_doc(proj, "idea")
    captured = (parse_note(idea)[0].get("captured") or "?")[:10] \
        if idea else "?"
    try:
        location = str(proj.relative_to(ROOT)) + "/"
    except ValueError:
        location = str(proj)
    ledger_update(slug, "project", "approved", location, captured)
    brain_journal(f"you approved the plan for [[{slug}]]")
    return dest


def _milestones(text):
    """Milestone names from a `## Milestones` markdown table."""
    rows = []
    for line in _section(text, "Milestones").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and not set(cells[0]) <= set("-: ") \
                and cells[0].lower() not in ("#", "no", "no."):
            rows.append(cells[1])
    return rows


def _team(text):
    """[(name, mandate)] from a `## Recommended team` bullet list."""
    team = []
    for line in _section(text, "Recommended team").splitlines():
        m = re.match(r"\s*[-*]\s*\**(.+?)\**\s+[—–-]\s+(.+)", line)
        if m:
            name = re.sub(r"[^\w]+", "-", m.group(1).strip().lower()).strip("-")
            if name:
                team.append((name, m.group(2).strip()))
    return team


def promote_project(slug):
    """Move an APPROVED project out of the pipeline into the promotion
    target and scaffold its workspace (docs/, STATUS.md, a project-manager
    CLAUDE.md and its team stubs). Refuses without approval; never
    overwrites an existing folder. Returns the project's new home."""
    proj = _project_dir(slug)
    if project_kind(proj) == "active":
        return proj
    if not project_approved(slug):
        raise NeedsApproval(f"{slug} is not approved — approve its plan "
                            "before promoting it")
    dest = promotion_dir() / slug
    if dest.exists():
        raise ValueError(f"{dest} already exists — not overwriting it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(proj), str(dest))
    docs = dest / "docs"
    docs.mkdir(exist_ok=True)
    for f in sorted(dest.glob("*.md")):
        if re.match(r"(" + "|".join(PROJECT_DOCS) + r")(\.v\d+)?\.md$",
                    f.name):
            f.rename(docs / f.name)
    if (dest / "actions.json").is_file():
        (dest / "actions.json").rename(docs / "actions.json")
    req_text = (docs / "requirements.md").read_text(encoding="utf-8")
    title = _project_title(slug, req_text)
    today = datetime.date.today().isoformat()
    pitch = plan_pitch(dest)
    templates = ROOT / "system" / "templates"

    status = (templates / "project-status.md").read_text(encoding="utf-8")
    milestones = _milestones(req_text) or ["Deliver the approved requirements"]
    status = status.replace("{{PROJECT_NAME}}", title).replace("{{DATE}}", today)
    status = re.sub(r"- \[ \] \{\{Milestone 1\}\}\n- \[ \] \{\{Milestone 2\}\}",
                    "\n".join(f"- [ ] {m}" for m in milestones), status)
    status = re.sub(r"\{\{What the PM[^}]*\}\}", "Not started.", status)
    status = re.sub(r"\{\{Anything waiting[^}]*\}\}", "None.", status)
    (dest / "STATUS.md").write_text(status, encoding="utf-8")

    team = _team(req_text)
    if not (dest / "CLAUDE.md").exists():
        pm = (templates / "pm-claude.md").read_text(encoding="utf-8")
        pm = pm.replace("{{PROJECT_NAME}}", title) \
               .replace("{{ONE_LINER}}", pitch or title) \
               .replace("{{PROMOTED_DATE}}", today)
        roster = "\n".join(f"- `{n}` — {m}" for n, m in team) or \
            "- (none yet — create specialists as the work calls for them)"
        pm = re.sub(r"\{\{TEAM_ROSTER[^}]*\}\}", lambda _m: roster, pm)
        (dest / "CLAUDE.md").write_text(pm, encoding="utf-8")
    agents_dir = dest / ".claude" / "agents"
    for name, mandate in team:
        f = agents_dir / f"{name}.md"
        if f.exists():
            continue
        agents_dir.mkdir(parents=True, exist_ok=True)
        f.write_text(
            f"---\nname: {name}\ndescription: {mandate}\n"
            "tools: Read, Write, Edit, Glob, Grep, Bash\n---\n\n"
            f"You are the **{name.replace('-', ' ')}** for {title}.\n\n"
            f"## Mandate\n\n{mandate}\n\n## Context\n\n"
            "- Requirements: `docs/requirements.md` — your work must serve "
            "its deliverables and acceptance criteria.\n\n"
            "## Report back\n\nEnd every task with: what you did, how you "
            "verified it, what you'd flag for the PM.\n", encoding="utf-8")
    if aria_git.enclosing_repo(dest) is None and not aria_git.is_repo_root(dest):
        try:
            aria_git.init(dest, f"Promote {slug} from the A.R.I.A. pipeline")
        except aria_git.GitError:
            pass                           # git is optional
    idea = docs / "idea.md"
    captured = (parse_note(idea)[0].get("captured") or "?")[:10] \
        if idea.is_file() else "?"
    try:
        location = str(dest.relative_to(ROOT)) + "/"
    except ValueError:
        location = str(dest)
    ledger_update(slug, "project", "promoted", location, captured)
    brain_journal(f"promoted [[{slug}]] to an active project")
    return dest


def adopt_project(source, goal, slug=""):
    """Bring an existing codebase under autopilot. `source` is a git URL,
    a GitHub `owner/repo`, or a folder on this machine; it becomes an
    active project whose contract is the goal the owner states here —
    stating it is the approval. Her bookkeeping goes in a hidden .aria/
    folder that git is told to ignore, so the codebase's own tree and
    history only ever receive the verified work. Returns the project
    folder."""
    goal = goal.strip()
    if not goal:
        raise ValueError('say what you want done: aria adopt <repo> "goal"')
    source = source.strip()
    local = Path(os.path.expanduser(source))
    is_url = bool(re.match(r"^(https?://|git@|ssh://|file://)", source)) or (
        bool(re.fullmatch(r"[\w.-]+/[\w.-]+", source)) and not local.exists())
    stem = re.sub(r"\.git$", "", source.rstrip("/").split("/")[-1])
    name = slugify(slug or stem.replace("_", "-").replace(".", "-"), limit=5)
    if name in project_slugs() or name == "untitled":
        raise ValueError(f"a project called {name} already exists — pass "
                         "--slug to name this one differently")
    dest = promotion_dir() / name
    if dest.exists():
        raise ValueError(f"{dest} already exists — not overwriting it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if is_url:
        url = source if re.match(r"^(\w+://|git@)", source) \
            else f"https://github.com/{source}"
        aria_git.clone(url, dest)
    else:
        local = local.resolve()
        if not local.is_dir():
            raise ValueError(f"not a folder: {local}")
        dest.symlink_to(local, target_is_directory=True)
    state = dest / ".aria"
    state.mkdir(exist_ok=True)
    today = datetime.date.today().isoformat()
    (state / "GOAL.md").write_text(
        f"# Goal for {name}\n\n> Stated by the owner on {today} when this "
        "codebase was adopted. It is the contract autopilot works to; "
        "change direction with a steering note.\n\n" + goal + "\n",
        encoding="utf-8")
    exclude = dest / ".git" / "info" / "exclude"
    if (dest / ".git").is_dir():
        exclude.parent.mkdir(exist_ok=True)
        have = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".aria/" not in have.splitlines():
            exclude.write_text(have.rstrip("\n") + "\n.aria/\n",
                               encoding="utf-8")
    ledger_update(name, "project", "adopted",
                  str(dest if is_url else local), today)
    brain_journal(f"adopted [[{name}]] — goal: {excerpt(goal, 100)}")
    return dest


def list_projects():
    """Every project, active first: slug, kind, approval, and what
    autopilot is doing with it right now."""
    seen, out = set(), []
    for base, kind in ((promotion_dir(), "active"), (POTENTIAL, "potential")):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name in seen or d.name == "crew" \
                    or d.name.startswith("."):
                continue
            if not any((d / n).exists() for n in
                       ("requirements.md", "docs", "proposed-plan.md",
                        "idea.md", "STATUS.md", ".aria")):
                continue
            seen.add(d.name)
            auto = d / ".aria" if (d / ".aria").is_dir() else d / "autopilot"
            state = {"status": "idle", "iteration": 0}
            if (auto / "state.json").is_file():
                try:
                    state = json.loads(
                        (auto / "state.json").read_text(encoding="utf-8"))
                except ValueError:
                    pass
            done = total = 0
            gated = []
            if (auto / "plan.json").is_file():
                try:
                    tasks = json.loads((auto / "plan.json").read_text(
                        encoding="utf-8")).get("tasks", [])
                    total = len([t for t in tasks
                                 if t.get("status") != "skipped"])
                    done = len([t for t in tasks
                                if t.get("status") == "done"])
                    # waiting on the owner — minus what they have already
                    # answered (an answer is a file until she folds it in)
                    gated = [{"id": str(t.get("id")),
                              "title": str(t.get("title", ""))}
                             for t in tasks if t.get("status") == "todo"
                             and t.get("needs_human") and not
                             (auto / f"decision-{t.get('id')}.json").is_file()]
                except (ValueError, AttributeError):
                    pass
            try:
                approved = project_approved(d.name)
            except ValueError:
                approved = False
            # open items on the owner's own list
            listed = _load_actions(d, d.name)
            mine = sum(1 for t in (listed or {}).get("you", [])
                       if not t.get("done"))
            out.append({"slug": d.name, "kind": kind, "approved": approved,
                        "your_list": mine,
                        "has_business": _project_doc(d, "business-plan")
                        is not None,
                        "autopilot": state.get("status", "idle"),
                        "iteration": state.get("iteration", 0),
                        "phase": state.get("phase", ""),
                        "current": (state.get("current") or {}).get("title", ""),
                        "waiting": state.get("waiting", ""),
                        "failed": (state.get("stats") or {}).get("failed", 0),
                        "done": done, "total": total, "needs_you": gated,
                        "has_plan": _project_doc(d, "proposed-plan") is not None})
    return out


# ---- what a project is doing, in words ----------------------------------

def _count(n, noun):
    """'1 task', '3 tasks' — these sentences are also read aloud, where
    "task(s)" is not a word."""
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _is(n):
    return "is" if n == 1 else "are"


def project_status_text(slug):
    """One or two speakable sentences on where a project stands."""
    p = next((x for x in list_projects() if x["slug"] == slug), None)
    if p is None:
        return f"I don't have a project called {slug}."
    name = slug.replace("-", " ")
    gated = p["needs_you"]
    needs = ""
    if gated:
        needs = (f" {_count(len(gated), 'task')} "
                 f"{'needs' if len(gated) == 1 else 'need'} your decision: "
                 + "; ".join(f"{g['title']} ({g['id']})" for g in gated[:3])
                 + f". Say 'approve task {gated[0]['id']} in {slug}' — add "
                   "your decision after a colon — or 'skip task "
                   f"{gated[0]['id']} in {slug}'.")
    if p["autopilot"] == "running":
        say = (f"{name} is running, iteration {p['iteration']}: "
               f"{p['done']} of {_count(p['total'], 'task')} done.")
        if p["phase"] == "waiting" and gated:
            say += " Everything that doesn't need you is done."
        elif p["waiting"]:
            say += f" I'm waiting — {p['waiting']}."
        elif p["current"]:
            say += f" Right now I'm on: {p['current']}."
        elif p["phase"]:
            say += f" Currently {p['phase']}."
        return say + needs
    if p["autopilot"] in ("stopped", "complete", "stalled", "budget"):
        ending = {"stopped": "stopped at your request",
                  "complete": "finished — the goal is met",
                  "stalled": "paused itself because it stopped making "
                             "progress",
                  "budget": "paused at the budget you set"}[p["autopilot"]]
        return (f"{name} {ending} after "
                f"{_count(p['iteration'], 'iteration')} with "
                f"{_count(p['done'], 'task')} done"
                + (f" and {p['failed']} it could not complete"
                   if p["failed"] else "")
                + ". The final report is on the Projects page." + needs)
    if not p["approved"]:
        return (f"{name} has a proposed plan waiting for your approval — "
                f"say 'approve {slug}' when you're happy with it."
                if p["has_plan"] else
                f"{name} is still an idea — run develop to get a plan.")
    return f"{name} is approved and idle — say 'work on {slug}' to start."


def _spoken_time(seconds):
    """A duration the way it is said aloud: 'about 40 minutes'."""
    seconds = float(seconds or 0)
    if seconds < 90:
        return "about a minute"
    if seconds < 3300:
        return f"about {int(round(seconds / 60 / 5) * 5) or 5} minutes"
    hours = seconds / 3600
    if hours < 20:
        half = round(hours * 2) / 2
        return (f"about {half:g} hour" + ("" if half == 1 else "s"))
    return f"about {round(hours / 24, 1):g} days"


def project_progress_text(slug):
    """Where a project stands on its road, speakable: milestones reached,
    what is in progress, what comes next, and how much work is left."""
    name = slug.replace("-", " ")
    pr = _autopilot().progress(slug)
    if not pr["total"]:
        return (project_status_text(slug) + " It has no roadmap yet — I "
                "lay one out when I first plan a project.")
    say = (f"{name}: {pr['reached']} of "
           f"{_count(pr['total'], 'milestone')} reached.")
    if pr["current"]:
        say += " In progress: " + "; ".join(
            f"{m['title']} ({m['done']} of about "
            f"{_count(max(m['est_tasks'], m['done'] + m['open']), 'task')})"
            for m in pr["current"][:2]) + "."
    if pr["next"]:
        say += " Still ahead: " + ", then ".join(
            m["title"] for m in pr["next"][:3]) \
            + (f", and {len(pr['next']) - 3} more" if len(pr["next"]) > 3
               else "") + "."
    if pr["reached"] == pr["total"]:
        say += " The whole road is behind me."
    elif pr["eta"] and pr["remaining_tasks"]:
        # measured time, with the slow case said too: an estimate that is
        # only ever the best case is the one that gets missed
        slow = _spoken_time(pr.get("eta_slow") or 0)
        usual = _spoken_time(pr["eta"])
        say += (f" That is roughly {_count(pr['remaining_tasks'], 'task')} "
                f"— {usual} of work"
                + (f", {slow.replace('about ', 'up to ')} if it goes slowly"
                   if slow != usual else "")
                + (" — a first guess; nothing has been measured on this "
                   "machine yet." if pr.get("basis") == "guess" else
                   f", going by {pr['rests_on']}."
                   if pr.get("rests_on") else "."))
    if pr["status"] == "running" and pr["now"]:
        say += f" Right now I'm on: {pr['now']['title']}."
    elif pr["status"] != "running":
        say += (" Autopilot is not running on it."
                if pr["status"] in ("idle", "stopped") else
                f" It is {pr['status']}.")
    if pr["needs_you"]:
        owed = len(pr["needs_you"])
        say += f" {_count(owed, 'task')} {_is(owed)} waiting for your decision."
    return say


def workspace_needs_text():
    """Everything that is waiting on the owner, speakable."""
    projects = list_projects()
    parts = []
    plans = [p for p in projects if not p["approved"] and p["has_plan"]]
    if plans:
        parts.append(f"{_count(len(plans), 'plan')} {_is(len(plans))} "
                     "waiting for your approval: "
                     + ", ".join(p["slug"].replace("-", " ") for p in plans)
                     + ".")
    for p in projects:
        if p["needs_you"]:
            parts.append(
                f"{p['slug'].replace('-', ' ')} has "
                f"{_count(len(p['needs_you']), 'task')} waiting for your "
                "decision: "
                + "; ".join(g["title"] for g in p["needs_you"][:3]) + ".")
        if p["autopilot"] in ("stalled", "budget"):
            parts.append(f"{p['slug'].replace('-', ' ')} has paused itself — "
                         + ("it stopped making progress" if p["autopilot"]
                            == "stalled" else "it reached the budget you set")
                         + "; it resumes when you say so.")
    unclear = [n for n in board()["inbox"] if n["type"] == "unclear"]
    if unclear:
        parts.append(f"{_count(len(unclear), 'note')} in the inbox "
                     f"{'has' if len(unclear) == 1 else 'have'} questions "
                     "for you.")
    mine = sum(p["your_list"] for p in projects)
    if mine:        # not waiting on a decision — but only they can do it
        listed = [p["slug"].replace("-", " ") for p in projects
                  if p["your_list"]]
        parts.append(f"On your own {'list' if len(listed) == 1 else 'lists'} "
                     f"({', '.join(listed[:3])}): {_count(mine, 'thing')} "
                     "only you can do — say 'what's on my list'.")
    if not parts:
        return "Nothing needs you right now."
    return " ".join(parts) + (" Approvals and decisions are yours to make "
                              "in the app.")


def workspace_progress_text():
    running = [p for p in list_projects() if p["autopilot"] == "running"]
    if not running:
        started = [p for p in list_projects() if p["iteration"]]
        if not started:
            return "Nothing has been started yet, so there is no road to report on."
        running = started[:2]
    return " ".join(project_progress_text(p["slug"]) for p in running[:3])


def workspace_status_text():
    projects = list_projects()
    running = [p for p in projects if p["autopilot"] == "running"]
    waiting = [p for p in projects if not p["approved"] and p["has_plan"]]
    parts = []
    if running:
        parts.append(" ".join(project_status_text(p["slug"]) for p in running))
    else:
        parts.append("Nothing is running on autopilot right now.")
    if waiting:
        parts.append(f"{_count(len(waiting), 'plan')} {_is(len(waiting))} "
                     "waiting for your approval: "
                     + ", ".join(p["slug"].replace("-", " ") for p in waiting)
                     + ".")
    for p in projects:              # decisions owed on projects not running
        if p["needs_you"] and p["autopilot"] != "running":
            parts.append(f"{p['slug'].replace('-', ' ')} has "
                         f"{_count(len(p['needs_you']), 'task')} waiting for "
                         "your decision.")
    b = board()
    if b["inbox"]:
        parts.append(f"{_count(len(b['inbox']), 'note')} "
                     f"{_is(len(b['inbox']))} in the inbox.")
    return " ".join(parts)


def project_changes(slug, limit=0):
    """Structured change records autopilot has written for a project,
    oldest first (see aria_autopilot for the schema)."""
    f = _auto_dir(slug, create=False) / "changes.jsonl"
    rows = []
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows[-limit:] if limit else rows


def project_changes_text(slug, limit=4):
    rows = [r for r in project_changes(slug) if r.get("status") == "done"]
    name = slug.replace("-", " ")
    if not rows:
        return f"I haven't changed anything in {name} yet."
    recent = rows[-limit:]
    bits = []
    for r in recent:
        files = [f["path"] for f in r.get("files", [])]
        bits.append(r["title"] + (f" — {', '.join(files[:3])}"
                                  if files else ""))
    return (f"{_count(len(rows), 'completed change')} in {name}. Most "
            "recent: "
            + "; ".join(bits) + ". The full list with reasoning is on the "
            "Projects page under Changes.")


# ---- git and GitHub, per project ----------------------------------------

def project_git(slug):
    """What the UI shows about a project's repository."""
    info = aria_git.summary(_project_dir(slug))
    info["github_access"] = config()["github_access"]
    info["gh"] = bool(aria_git.gh())
    return info


def project_git_init(slug, force=False):
    """Give a project its own git repository (explicit owner action)."""
    proj = _project_dir(slug)
    parent = aria_git.enclosing_repo(proj)
    if parent and not aria_git.is_repo_root(proj) and not force:
        raise ValueError(
            f"{slug} lives inside another repository ({parent}). Point "
            "promotion_target at a folder outside it to give projects "
            "their own repos — or pass --force to nest one here.")
    aria_git.init(proj, f"Initial commit of {slug} by A.R.I.A.")
    return project_git(slug)


def _need_github(slug):
    cfg = config()
    if not cfg["github_access"]:
        raise ValueError("GitHub access is off — turn it on in Settings "
                         "(or: aria settings set github_access true)")
    proj = _project_dir(slug)
    if not aria_git.is_repo_root(proj):
        raise ValueError(f"{slug} isn't its own git repository yet — run: "
                         f"aria github init {slug}")
    return cfg, proj


def project_publish(slug, private=True):
    """Create a GitHub repository for a project and push it (explicit
    owner action; private unless told otherwise). Returns the remote URL."""
    _cfg, proj = _need_github(slug)
    url = aria_git.publish(proj, slug, private=private)
    brain_journal(f"published [[{slug}]] to GitHub")
    return url


def project_push(slug, body=""):
    """Push a project's autopilot branch and make sure a pull request is
    open for it. Returns {"say", "pr", "pushed"}; the base branch is never
    pushed to."""
    cfg, proj = _need_github(slug)
    if not aria_git.remote_url(proj):
        raise ValueError(f"{slug} has no GitHub remote yet — run: "
                         f"aria github publish {slug}")
    if not aria_git.branch_exists(proj, aria_git.WORK_BRANCH):
        raise ValueError(f"{slug} has no autopilot work to push yet")
    aria_git.push(proj)
    base = _auto_state(slug).get("git_base") or cfg["github_base_branch"]
    pr = ""
    try:
        pr = aria_git.pr_open(
            proj, base, f"A.R.I.A. autopilot: {slug.replace('-', ' ')}",
            body or f"Autonomous work on `{slug}` by A.R.I.A. — every "
                    "commit is one verified task; see "
                    "`autopilot/FINAL-REPORT.md` for the reasoning.")
    except aria_git.GitError as exc:
        return {"pushed": True, "pr": "",
                "say": f"Pushed {slug}'s autopilot branch, but couldn't "
                       f"open a pull request: {exc}"}
    return {"pushed": True, "pr": pr,
            "say": f"Pushed {slug}'s autopilot branch. Pull request: {pr}"}


# ---- the owner's desktop: file manager, editor, terminal ------------------
#
# Only ever on a click in the app or a command in a terminal, and only for
# a project's own folder. Files are handed to an editor, never to
# "whatever opens this type" — a model-written .desktop or script must not
# be launched by looking at it.

EDITORS = ("code", "codium", "zed", "zeditor", "kate", "subl", "pulsar",
           "gnome-text-editor", "gedit", "kwrite", "mousepad", "xed",
           "pluma", "geany")
FOLDER_EDITORS = {"code", "codium", "zed", "zeditor", "kate", "subl",
                  "pulsar", "geany"}
TERMINALS = (("konsole", ["--workdir", "{dir}"]),
             ("gnome-terminal", ["--working-directory={dir}"]),
             ("kgx", ["--working-directory={dir}"]),
             ("ptyxis", ["--new-window", "-d", "{dir}"]),
             ("xfce4-terminal", ["--working-directory={dir}"]),
             ("kitty", ["--directory", "{dir}"]),
             ("alacritty", ["--working-directory", "{dir}"]),
             ("wezterm", ["start", "--cwd", "{dir}"]),
             ("foot", ["--working-directory={dir}"]),
             ("tilix", ["-w", "{dir}"]),
             ("terminator", ["--working-directory={dir}"]),
             ("xterm", []))


def _which(name):
    import shutil
    return shutil.which(name)


def desktop_notify(title, body=""):
    """A notification on the owner's desktop (notify-send), for the moments
    that want them: a task waits on their decision, a run has ended. Sent
    by the always-on server, so it arrives with no window open. Returns
    whether one was sent."""
    if not config()["desktop_notifications"]:
        return False
    tool = _which("notify-send")
    if not tool or not (os.environ.get("DBUS_SESSION_BUS_ADDRESS")
                        or os.environ.get("XDG_RUNTIME_DIR")):
        return False
    try:
        subprocess.Popen(
            [tool, "-a", "A.R.I.A.", "-i", str(ROOT / "docs" / "aria-icon.svg"),
             " ".join(str(title).split())[:120],
             " ".join(str(body).split())[:400]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def desktop_tools(cfg=None):
    """What can be opened from the app on this machine."""
    cfg = cfg or config()
    editor = cfg["editor"].split()[0] if cfg["editor"] else next(
        (e for e in EDITORS if _which(e)), "")
    terminal = cfg["terminal"].split()[0] if cfg["terminal"] else next(
        (name for name, _ in TERMINALS if _which(name)), "")
    return {"folder": bool(_which("xdg-open")), "editor": editor,
            "terminal": terminal}


def _spawn(argv, cwd):
    subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def open_path(slug, path="", how="folder"):
    """Open a project's folder in the file manager, the project (or one
    of its files) in an editor, or a terminal in its folder. Returns what
    was opened, in words."""
    import shlex
    cfg = config()
    proj = _project_dir(slug).resolve()
    target = (proj / path).resolve() if path else proj
    if target != proj and proj not in target.parents:
        raise ValueError("that path is outside the project")
    if not target.exists():
        raise ValueError(f"{path} is not in the project")
    tools = desktop_tools(cfg)
    if how == "folder":
        folder = target if target.is_dir() else target.parent
        if not tools["folder"]:
            raise ValueError("no way to open a file manager here (xdg-open "
                             f"is missing). The folder is: {folder}")
        _spawn(["xdg-open", str(folder)], folder)
        return f"opened {folder} in your file manager"
    if how == "editor":
        if not tools["editor"]:
            raise ValueError("no editor found — set one in Settings "
                             "(e.g. code, kate, zed). The path is: "
                             f"{target}")
        argv = shlex.split(cfg["editor"]) if cfg["editor"] \
            else [tools["editor"]]
        if target.is_dir() and argv[0] not in FOLDER_EDITORS \
                and not cfg["editor"]:
            raise ValueError(f"{argv[0]} opens files, not folders — pick a "
                             "file, or set an editor that opens folders in "
                             f"Settings. The folder is: {target}")
        _spawn(argv + [str(target)], proj)
        return f"opened {target.name} in {argv[0]}"
    if how == "terminal":
        folder = target if target.is_dir() else target.parent
        if cfg["terminal"]:
            # the owner's own command: "{dir}" is filled in; a trailing
            # option ("konsole --workdir") gets the folder as its value
            parts = shlex.split(cfg["terminal"])
            argv = [a.replace("{dir}", str(folder)) for a in parts]
            if argv == parts and argv[-1].startswith("-"):
                argv.append(str(folder))
        else:
            hit = next(((name, args) for name, args in TERMINALS
                        if _which(name)), None)
            if hit is None:
                raise ValueError("no terminal emulator found — set one in "
                                 f"Settings. The folder is: {folder}")
            argv = [hit[0]] + [a.format(dir=str(folder)) for a in hit[1]]
        _spawn(argv, folder)
        return f"opened a terminal in {folder}"
    raise ValueError("how must be folder, editor or terminal")


# ---- running a project's software (aria_run) ------------------------------
#
# Two things with one hard line between them. `project_run_start` starts
# a program on the owner's machine and is reached ONLY from a click in the
# app or their own terminal — never from autopilot, chat or voice.
# `project_check` runs the project's own tests in the sandbox; that one
# autopilot may use.

def checks_available(cfg=None):
    """(can checks run here?, why not)."""
    cfg = cfg or config()
    if cfg["autopilot_checks"] == "off":
        return False, "running checks is switched off"
    if cfg["autopilot_checks"] == "direct":
        return True, ""
    tool, why = aria_run.sandbox()
    return bool(tool), why


def project_run_profile(slug):
    proj = _project_dir(slug).resolve()
    cfg = config()
    tool, why = aria_run.sandbox()
    return {"slug": slug, "path": str(proj),
            **aria_run.profile(proj, _auto_dir(slug)),
            "sandbox": {"available": bool(tool), "why": why},
            "checks": cfg["autopilot_checks"],
            "status": aria_run.status(slug),
            "tools": desktop_tools(cfg)}


def project_run_start(slug, cmd, sandboxed=True):
    proj = _project_dir(slug).resolve()
    info = aria_run.start(slug, proj, _auto_dir(slug), cmd,
                          sandboxed=sandboxed)
    brain_journal(f"you ran [[{slug}]]: `{' '.join(str(cmd).split())[:80]}`")
    return info


def project_run_stop(slug):
    return aria_run.stop(slug)


def project_run_output(slug, since=0):
    _project_dir(slug)
    return aria_run.output(slug, since)


def project_run_input(slug, text):
    return aria_run.send(slug, text)


def project_run_save(slug, kind, name, cmd):
    return aria_run.save_command(_auto_dir(slug), kind, name, cmd)


def project_check(slug):
    """Run the project's own tests the way autopilot does: sandboxed, in
    a scratch copy. Returns aria_run.check's result."""
    cfg = config()
    return aria_run.check(_project_dir(slug).resolve(), _auto_dir(slug),
                          mode=cfg["autopilot_checks"],
                          timeout=cfg["autopilot_check_seconds"])


# ---- the giants (the garage lives in aria_garage) ------------------------

def garage_status(probe=True):
    return aria_garage.status(config(), probe=probe)


def garage_scan(folder=""):
    folders = [folder] if folder else aria_garage.default_folders()
    return aria_garage.scan(folders)


def garage_load(role, notify=None, retry=True):
    if role not in aria_garage.ROLES:
        raise ValueError(f"no such role: {role} (general, research, coder)")
    return aria_garage.load(role, config(), notify=notify, retry=retry)


def garage_test(role, notify=None):
    """Prove a giant end to end — the answer to "can she really load it?":
    load it, ask it one short question, time both. The outcome (also a
    failure) is kept for the Settings page. Returns
    {ok, label, load_seconds, seconds, words, reply | error}."""
    if role not in aria_garage.ROLES:
        raise ValueError(f"no such role: {role} (general, research, coder)")
    cfg = config()
    label = (aria_garage.configured(cfg).get(role) or {}).get("label", "")
    result = {"ok": False, "label": label}
    try:
        t0 = time.time()
        aria_garage.load(role, cfg, notify=notify, retry=True)
        result["load_seconds"] = round(time.time() - t0, 1)
        target = _giant_target(role, cfg)
        if target.get("fallback"):
            raise aria_garage.GarageError(target["fallback"])
        if notify:
            notify(f"the {role} giant is loaded — asking it a question")
        t1 = time.time()
        with aria_garage.serving(role, cfg, notify=notify):
            reply = _chat_call(target, [{"role": "user", "content":
                               "In one short sentence: which model are you, "
                               "and are you ready to work?"}],
                               False, None, 7 * 24 * 3600, 0)
        reply = " ".join(strip_think(reply).split())
        if not reply:
            raise aria_garage.GarageError("it loaded but answered nothing")
        took = time.time() - t1
        result.update(ok=True, seconds=round(took, 1),
                      words=len(reply.split()), reply=clip(reply, 240))
    except Exception as exc:
        result["error"] = " ".join(str(exc).split())[:400]
    aria_garage.note_test(role, cfg, result)
    return result


def garage_unload(role=""):
    if role and role not in aria_garage.ROLES:
        raise ValueError(f"no such role: {role} (general, research, coder)")
    return [role] if role and aria_garage.stop(role) else (
        [] if role else aria_garage.stop_all())


def garage_reap():
    return aria_garage.reap(config())


# ---- autopilot (the loop lives in aria_autopilot) ------------------------

def _autopilot():
    import aria_autopilot
    return aria_autopilot


def autopilot_start(slug, via="cli"):
    return _autopilot().start(slug, via=via)


def autopilot_stop(slug):
    return _autopilot().stop(slug)


def autopilot_steer(slug, text, via="chat"):
    return _autopilot().steer(slug, text, via=via)


def autopilot_refine(slug, via="app"):
    """Ask for a review-and-refine pass over a project's code."""
    return _autopilot().refine(slug, via=via)


def autopilot_decide(slug, task_id, approve=True, note="", via="app"):
    """The owner's answer on a task that waits for them."""
    return _autopilot().decide(slug, task_id, approve=approve, note=note,
                               via=via)


def autopilot_tick(slug, emit):
    return _autopilot().tick(slug, emit)


def autopilot_plan(slug, emit):
    return _autopilot().plan(slug, emit)


def project_view(slug):
    return _autopilot().view(slug)


def project_outputs(slug):
    """What she wrote before: the finished live streams, newest first."""
    _project_dir(slug)
    return _autopilot().outputs(slug)


def project_output(slug, n):
    _project_dir(slug)
    return _autopilot().output(slug, n)


def file_history(slug, path):
    """Every verified change to one of a project's files, newest first."""
    _project_dir(slug)
    return _autopilot().file_history(slug, path)


def file_diff(slug, path, task):
    """One change to one file as rows of (previous line, modified line)."""
    _project_dir(slug)
    return _autopilot().file_diff(slug, path, task)


# ---------------------------------------------------------------- version
#
# Code changes on disk (an update, a pull) reach the always-on server only
# when it restarts. So the server records what it started with, the app
# shows it, and whoever updates can tell — and restart it — when the two
# have drifted apart.

SERVER_FILE = ROOT / "system" / "server.json"


def code_version():
    """A fingerprint of the server's own code as it is on disk now."""
    digest = hashlib.sha1()
    for f in sorted((ROOT / "bin").iterdir()):
        if f.is_file() and (f.name == "aria-ui" or
                            (f.name.startswith("aria_") and f.suffix == ".py")):
            digest.update(f.name.encode())
            digest.update(f.read_bytes())
    return digest.hexdigest()[:12]


def version():
    """Which version this copy is: {branch, commit, date, subject} — empty
    strings when it is not a git checkout."""
    def git(*argv):
        try:
            r = subprocess.run(["git", "-C", str(ROOT), *argv],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""
    commit = git("rev-parse", "--short", "HEAD")
    if not commit:
        return {"branch": "", "commit": "", "date": "", "subject": ""}
    return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "commit": commit, "date": git("log", "-1", "--format=%cs"),
            "subject": git("log", "-1", "--format=%s")}


SERVICE = "aria.service"


def service_state():
    """What systemd says about the always-on unit: its main process (0 when
    it is not running, or where there is no systemd) and whether the unit
    serves THIS copy of A.R.I.A."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", SERVICE, "-p", "MainPID",
             "-p", "ExecStart"], capture_output=True, text=True,
            timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"pid": 0, "ours": False}
    found = re.search(r"^MainPID=(\d+)", out, re.M)
    return {"pid": int(found.group(1)) if found else 0,
            "ours": str(ROOT / "bin" / "aria-ui") in out}


def _is_the_service(pid):
    """Is this process the one systemd runs as the always-on unit?"""
    try:
        return bool(pid) and service_state()["pid"] == int(pid)
    except (TypeError, ValueError):
        return False


def server_mark(port):
    """The server, at start: put on record who is serving this copy."""
    info = {"pid": os.getpid(), "port": int(port), "code": code_version(),
            "version": version(),
            "started": datetime.datetime.now().isoformat(timespec="seconds"),
            # "Run by systemd" means being the unit's own main process.
            # Having been started under SOME unit is not it: systemd's
            # INVOCATION_ID is handed down to everything a unit starts —
            # a terminal the desktop launched as one, and with it every
            # server started by hand there (and every self-test).
            "managed": bool(os.environ.get("INVOCATION_ID"))
            and _is_the_service(os.getpid())}
    tmp = SERVER_FILE.with_name(SERVER_FILE.name + ".tmp")
    tmp.write_text(json.dumps(info, indent=2), encoding="utf-8")
    os.replace(tmp, SERVER_FILE)
    return info


def server_unmark():
    """The server, on its way out: take its own record away."""
    try:
        if json.loads(SERVER_FILE.read_text("utf-8")).get("pid") == os.getpid():
            SERVER_FILE.unlink()
    except (OSError, ValueError):
        pass


def server_running():
    """The server of this copy, if one is up: its record plus `stale`
    (newer code is on disk than it is running). The record is believed
    only if the process answers for it — a left-over file may name a pid
    that is somebody else's by now."""
    try:
        info = json.loads(SERVER_FILE.read_text("utf-8"))
        with urllib.request.urlopen(
                f"http://127.0.0.1:{int(info['port'])}/api/version",
                timeout=3) as r:
            live = json.load(r)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if live.get("pid") != info.get("pid"):
        return None
    info["stale"] = info.get("code") != code_version()
    # a record is a claim; whether systemd runs this process is asked now
    info["managed"] = bool(info.get("managed")) \
        and _is_the_service(info.get("pid"))
    return info


def server_restart(info):
    """Restart the server `info` describes onto the code on disk. Returns
    how it was done: "systemd" or "respawned"."""
    if info.get("managed") and _is_the_service(info.get("pid")):
        subprocess.run(["systemctl", "--user", "restart", SERVICE],
                       check=False)
        return "systemd"
    pid, port = int(info["pid"]), int(info["port"])
    os.kill(pid, signal.SIGTERM)
    # Gone is "no longer listening", not "no such pid": a server the app
    # window started lingers as that window's defunct child.
    for _ in range(150):                        # it stops her programs first
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        except OSError:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"the server (pid {pid}) did not stop — stop it "
                           "by hand, then open the app again")
    state = Path(os.environ.get("XDG_STATE_HOME")
                 or Path.home() / ".local" / "state")
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "aria-ui.log", "ab") as log:
        subprocess.Popen([sys.executable, str(ROOT / "bin" / "aria-ui"),
                          str(info["port"])], stdout=log,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    return "respawned"


def autopilot_busy():
    """Projects autopilot is in the middle of a step on (a run that only
    waits for the owner is not: nothing would be lost)."""
    return [p["slug"] for p in list_projects()
            if p["autopilot"] == "running" and not p["waiting"]]


# ---------------------------------------------------------------- config edit

_BOOL_KEYS = {"auto_pipeline", "business_plans", "web_access",
              "github_access", "ollama_autostart", "models_warm",
              "brain_journal", "autopilot_git", "autopilot_refine",
              "desktop_notifications", "giants_free_memory"}
_CHOICE_KEYS = {
    "web_search": {"auto", "duckduckgo", "brave", "searxng", "wikipedia",
                   "none"},
    "autopilot_after_goal": {"improve", "stop"},
    "giants_use": {"auto", "always", "never"},
    "thinking": {"auto", "always", "never"},
    "autopilot_checks": {"sandbox", "direct", "off"},
    "engine": {"ollama", "claude"},
    "voice_engine": {"auto", "local", "browser"},
    "voice_input": {"auto", "local", "browser"},
}
_NUMBER_KEYS = {"context_tokens", "autopilot_max_tasks",
                "autopilot_max_iterations", "autopilot_max_hours",
                "autopilot_stall_limit", "autopilot_idle_minutes",
                "giants_loaded", "giants_idle_minutes", "giants_ctx",
                "giants_port", "autopilot_check_seconds",
                "triage_confidence_threshold", "voice_speed"}
_TEXT_KEYS = {"obsidian_vault", "fast", "smart", "deep", "stt_model",
              "web_search_url", "web_allowlist", "github_base_branch",
              "promotion_target", "giant_general", "giant_research",
              "giant_coder", "giants_flags", "editor", "terminal",
              "heartbeat_url", "voice_name", "alexa_url",
              "alexa_skill_id", "based_in", "ollama_keep_alive",
              "ollama_command"}
SETTABLE = _BOOL_KEYS | set(_CHOICE_KEYS) | _NUMBER_KEYS | _TEXT_KEYS


def _config_clean(key, value):
    """`value` as it is stored for `key` — or ValueError saying what is
    wrong with it."""
    if key not in SETTABLE:
        raise ValueError(f"config key not settable: {key}")
    value = " ".join(str(value).split())          # one line, always
    if key in _BOOL_KEYS:
        value = "true" if value.lower() in ("true", "yes", "on", "1") \
            else "false"
    elif key in _CHOICE_KEYS:
        value = value.lower()
        if value not in _CHOICE_KEYS[key]:
            raise ValueError(f"{key} must be one of: "
                             + ", ".join(sorted(_CHOICE_KEYS[key])))
    elif key in _NUMBER_KEYS:
        try:
            number = float(value or 0)
        except ValueError:
            raise ValueError(f"{key} must be a number")
        if number < 0:
            raise ValueError(f"{key} cannot be negative")
        if key == "autopilot_stall_limit" and number < 1:
            raise ValueError("autopilot_stall_limit must be at least 1 — "
                             "one dry batch is the soonest she can notice")
        if key == "giants_loaded" and number < 1:
            raise ValueError("giants_loaded must be at least 1")
        if key == "triage_confidence_threshold" and number > 1:
            raise ValueError("triage_confidence_threshold is a share: "
                             "0.7 means 70%")
        if key == "giants_port" and not 1024 <= number <= 65000:
            raise ValueError("giants_port must be between 1024 and 65000")
        if key == "voice_speed" and not 0.5 <= number <= 2:
            raise ValueError("voice_speed is between 0.5 (slow) and 2 (fast)")
        if key in ("autopilot_max_tasks", "autopilot_max_iterations",
                   "autopilot_stall_limit", "giants_loaded", "giants_ctx",
                   "giants_port"):
            number = int(number)                  # whole tasks, whole batches
        value = str(int(number)) if number == int(number) else str(number)
    elif "#" in value:
        raise ValueError(f"{key} cannot contain '#'")
    elif key == "ollama_keep_alive" and value and not re.fullmatch(
            r"-1|\d+[smh]?", value.lower()):
        raise ValueError("ollama_keep_alive is a time like 30m, 2h or 90s "
                         "(-1 = never unload; empty = leave it to Ollama)")
    elif key == "alexa_url" and value and not re.fullmatch(
            r"https://[\w.-]+(?::\d+)?/?", value):
        raise ValueError("alexa_url is the tunnel's own address, like "
                         "https://my-machine.my-tailnet.ts.net — https, "
                         "and nothing after the host")
    elif key == "alexa_skill_id" and value and not value.startswith(
            "amzn1.ask.skill."):
        raise ValueError("a skill id looks like amzn1.ask.skill.… — it is "
                         "on the skill's page in the Alexa developer "
                         "console")
    elif key.startswith("giant_") and value:
        path = Path(os.path.expanduser(value))
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file() or path.suffix.lower() != ".gguf":
            raise ValueError(f"{value} is not a .gguf file — point at the "
                             "first part of the model (…-00001-of-0000N.gguf)")
    return value


def config_set(key, value):
    """Set one allowed key — in the owner's own file (config.local.yml),
    never in the shipped config.yml."""
    _local_set(key, _config_clean(key, value))


def config_update(pairs):
    """Save a whole form of settings (the Settings page posts every field):
    all of it is checked first, then only what differs from the value in
    force is written — so the owner's file holds what they changed, not a
    copy of every default. Returns the keys that changed."""
    mine = _scalars(_local_text())
    shipped = _scalars(CONFIG_FILE.read_text(encoding="utf-8"))
    changed = {}
    for key, value in pairs.items():
        if key not in SETTABLE:
            raise ValueError(f"config key not settable: {key}")
        raw = " ".join(str(value).split())
        current = mine[key] if key in mine else shipped.get(key, "")
        if raw == current:
            continue                    # untouched — nothing to check
        value = _config_clean(key, raw)
        try:
            same = value == _config_clean(key, current)
        except ValueError:
            same = False                # what is there now is not valid
        if not same:
            changed[key] = value
    for key, value in changed.items():
        _local_set(key, value)
    return sorted(changed)


def _local_write(text):
    tmp = LOCAL_FILE.with_name(LOCAL_FILE.name + ".tmp")
    tmp.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
    os.replace(tmp, LOCAL_FILE)


def _local_pad(text):
    """How far the settings in the owner's file are indented (two spaces
    under `aria:`, none in a file written flat)."""
    return "  " if re.search(r"^aria:[ \t]*(?:#.*)?$", text, re.M) else ""


def _local_set(key, value):
    """Write one setting into the owner's own file (created on first use)."""
    text = _local_text() or LOCAL_HEADER
    line = f"{_local_pad(text)}{key}: {value}".rstrip()
    pattern = re.compile(rf"^[ \t]*{re.escape(key)}:.*$", re.M)
    if pattern.search(text):
        text = pattern.sub(lambda m: line, text, count=1)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    _local_write(text)


def _local_drop(text, key):
    """`text` without the setting `key` — a block goes with its children."""
    out, skip = [], None
    for ln in text.splitlines():
        indent = len(ln) - len(ln.lstrip())
        if skip is not None:
            if not ln.strip() or indent > skip:
                continue
            skip = None
        if re.match(rf"^[ \t]*{re.escape(key)}:", ln):
            skip = indent
            continue
        out.append(ln)
    return "\n".join(out)


def config_unset(key):
    """Take one setting out of the owner's own file — back to the default.
    True when it was there."""
    text = _local_text()
    if not re.search(rf"^[ \t]*{re.escape(key)}:", text, re.M):
        return False
    _local_write(_local_drop(text, key))
    return True


def _local_block(key, pairs):
    """Write a whole block (backends, webhooks…) into the owner's file."""
    text = _local_drop(_local_text() or LOCAL_HEADER, key)
    pad = _local_pad(text)
    _local_write("\n".join(
        [text.rstrip("\n"), f"{pad}{key}:"]
        + [f"{pad}  {k}: {v}" for k, v in pairs.items()]))


def config_set_entry(block, name, value):
    """Set — or with an empty value remove — one entry of a list setting
    (a webhook, a backend, a backend key) in the owner's own file. The
    owner's list starts as a copy of the shipped one."""
    if block not in BLOCKS:
        raise ValueError(f"{block} is not a list setting — those are: "
                         + ", ".join(BLOCKS))
    name = str(name).strip()
    if not re.fullmatch(r"[\w.-]{1,60}", name):
        raise ValueError(f"'{name}' is not a usable name (letters, digits, "
                         "- _ . only)")
    value = " ".join(str(value).split())
    if "#" in value:
        raise ValueError(f"{block}.{name} cannot contain '#'")
    if value and block != "backend_keys" \
            and not re.match(r"https?://\S+$", value):
        raise ValueError(f"{block}.{name} must be an http(s) address")
    own = _block(_local_text(), block)
    shipped = _block(CONFIG_FILE.read_text("utf-8"), block) or {}
    pairs = dict(own if own is not None else shipped)
    if value:
        pairs[name] = value
    elif pairs.pop(name, None) is None:
        return False
    if pairs == shipped:
        config_unset(block)             # nothing of the owner's left in it
    else:
        _local_block(block, pairs)
    return True


def config_mine():
    """The owner's own settings: ({key: value}, {block: {key: value}})."""
    text = _local_text()
    blocks = {k: _block(text, k) for k in BLOCKS}
    return ({k: v for k, v in _scalars(text).items() if k not in BLOCKS},
            {k: v for k, v in blocks.items() if v is not None})


def config_import(path, base=None):
    """Carry the settings of an edited config.yml over into the owner's own
    file: everything in it that differs from `base`, the shipped config.yml
    it was edited from (default: this version's). A setting the owner's
    file already has is left as it is. Returns (kept keys, [(key, why it
    was left out)])."""
    old = Path(os.path.expanduser(str(path))).read_text(encoding="utf-8")
    shipped = CONFIG_FILE.read_text(encoding="utf-8")
    base = shipped if base is None else base
    was, ref, now = _scalars(old), _scalars(base), _scalars(shipped)
    mine, my_blocks = config_mine()
    kept, left = [], []
    for key in sorted(was):
        value = " ".join(was[key].split())
        if key in BLOCKS or value == ref.get(key, ""):
            continue
        if key in mine:
            if mine[key] != value:
                left.append((key, f"your own settings already say "
                                  f"'{mine[key]}' (the old file: '{value}')"))
            continue
        if key not in now and key not in SETTABLE:
            left.append((key, "this version has no such setting"))
            continue
        try:
            if key in SETTABLE:
                config_set(key, value)          # validated, like Settings
            else:
                _local_set(key, value)
            kept.append(key)
        except ValueError as exc:
            left.append((key, str(exc)))
    for key in BLOCKS:
        block = _block(old, key)
        if not block or block == (_block(base, key) or {}):
            continue
        if key in my_blocks:
            if my_blocks[key] != block:
                left.append((key, "your own settings already have this "
                                  "block"))
            continue
        _local_block(key, block)
        kept.append(key)
    # a block the engine does not read itself (the slash commands do)
    for key in ("categories", "lists"):
        if (_block(old, key) or {}) != (_block(base, key) or {}):
            left.append((key, "only read from system/config.yml — copy it "
                              "over by hand if you still want your version"))
    return kept, left


def _probe_backend(target):
    """Is this tier's backend up, and does it have the model (ollama only)?
    A giant counts as up when its files are complete: it loads on demand."""
    if target.get("giant"):
        return {"server": True, "ok": True, "models": [], "on_demand": True}
    try:
        if target["kind"] == "ollama":
            with urllib.request.urlopen(target["url"] + "/api/tags",
                                        timeout=5) as resp:
                models = [m.get("name", "")
                          for m in json.load(resp).get("models", [])]
            ok = any(m == target["model"]
                     or m.startswith(target["model"] + ":") for m in models)
            return {"server": True, "ok": ok, "models": models}
        with urllib.request.urlopen(target["url"] + "/v1/models",
                                    timeout=5) as resp:
            resp.read()
        return {"server": True, "ok": True, "models": []}
    except Exception as exc:
        return {"server": False, "ok": False, "error": str(exc)}


def health():
    cfg = config()
    tiers = {}
    probes = {}
    for tier in ("fast", "smart", "deep"):
        target = resolve_tier(tier, cfg)
        key = (target["kind"], target["url"], target["model"])
        if key not in probes:
            probes[key] = _probe_backend(target)
        tiers[tier] = {"backend": target["kind"], "model": target["model"],
                       "url": target["url"], **{k: v for k, v in
                       probes[key].items() if k != "models"}}
    fast = resolve_tier("fast", cfg)
    fast_probe = probes[(fast["kind"], fast["url"], fast["model"])]
    # is Ollama answering? (None: no tier runs on it)
    ollama = [p["server"] for (kind, _url, _model), p in probes.items()
              if kind == "ollama"]
    return {"ok": fast_probe["ok"], "server": fast_probe["server"],
            "model": fast["model"], "url": fast["url"],
            "models": fast_probe.get("models", []),
            "error": fast_probe.get("error", ""),
            "ollama": any(ollama) if ollama else None,
            "wake": wake_state(),
            "tiers": tiers,
            "auto_pipeline": cfg["auto_pipeline"],
            "obsidian": bool(cfg["obsidian_vault"]),
            "brain": brain_ready(cfg),
            "web": cfg["web_access"],
            "web_search": aria_web.provider_chain(cfg)
            if cfg["web_access"] else [],
            "github": cfg["github_access"],
            "gh": bool(aria_git.gh()),
            "context_tokens": cfg["context_tokens"],
            "skills": len(list_skill_names()),
            "agents": [a["name"] for a in list_agents()],
            "library_agents": len(list_library_agents())}
