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

import datetime
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import aria_brain
import aria_garage
import aria_git
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

def config():
    text = (ROOT / "system" / "config.yml").read_text(encoding="utf-8")

    def find(key, default):
        # [ \t]* (not \s*) so an empty value can't swallow the newline and
        # match the next line as the value
        m = re.search(rf"^[ \t]*{re.escape(key)}:[ \t]*(\S.*?)[ \t]*$", text, re.M)
        return m.group(1) if m else default

    def block(key):
        """key: value pairs nested under a `<key>:` heading."""
        lines = text.splitlines()
        out = {}
        for i, ln in enumerate(lines):
            m = re.match(rf"^([ \t]*){re.escape(key)}:[ \t]*(?:#.*)?$", ln)
            if not m:
                continue
            indent = len(m.group(1))
            for sub in lines[i + 1:]:
                if not sub.strip() or sub.lstrip().startswith("#"):
                    continue
                sm = re.match(r"^([ \t]*)([\w.-]+):[ \t]*(\S.*?)[ \t]*$", sub)
                if not sm or len(sm.group(1)) <= indent:
                    break
                out[sm.group(2)] = sm.group(3).split(" #")[0].strip()
            break
        return out

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
        "obsidian_vault": os.environ.get(
            "ARIA_OBSIDIAN_VAULT", find("obsidian_vault", "")),
        "brain_journal": flag("brain_journal", "true"),
        "stt_model": find("stt_model", "base"),
        "tiers": {"fast": find("fast", ""), "smart": find("smart", ""),
                  "deep": find("deep", "")},
        "backend_urls": {k: v.rstrip("/") for k, v in backends.items()},
        "backend_keys": block("backend_keys"),
        "context_tokens": int(number("context_tokens", 8192)),
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
        # the giants: one role each, started and swapped by aria_garage
        "giants": {r: find(f"giant_{r}", "").split(" #")[0].strip()
                   for r in aria_garage.ROLES},
        "giants_use": find("giants_use", "auto").split(" #")[0].strip()
            .lower(),
        "giants_loaded": max(1, int(number("giants_loaded", 1))),
        "giants_idle_minutes": number("giants_idle_minutes", 0),
        "giants_port": int(number("giants_port", 8090)),
        "giants_ctx": int(number("giants_ctx", 8192)),
        "giants_flags": find("giants_flags", "").split(" #")[0].strip(),
    }


def _parse_spec(spec, cfg):
    """Parse a 'backend/model' spec against configured backends; None if
    the backend name is unknown."""
    backend, _, model = spec.partition("/")
    backend = backend.strip().lower()
    model = model.strip()
    if not model:  # bare model name means ollama
        backend, model = "ollama", backend
    url = cfg["backend_urls"].get(backend)
    if url is None:
        return None
    kind = "ollama" if backend == "ollama" else "openai"
    key = (os.environ.get(f"ARIA_KEY_{backend.upper()}")
           or cfg["backend_keys"].get(backend) or "local")
    return {"kind": kind, "url": url, "model": model,
            "backend": backend, "key": key, "spec": f"{backend}/{model}",
            "num_ctx": cfg.get("context_tokens", 0)}


def _legacy_target(cfg, tier):
    return {"kind": "ollama", "url": cfg["url"], "model": cfg["model"],
            "backend": "ollama", "spec": f"ollama/{cfg['model']}",
            "num_ctx": cfg.get("context_tokens", 0), "tier": tier}


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
                timeout=3600, max_seconds=0):
    """Single-prompt convenience wrapper around chat_messages."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat_messages(target, messages, force_json=force_json,
                         on_token=on_token, timeout=timeout,
                         max_seconds=max_seconds)


def chat_messages(target, messages, force_json=False, on_token=None,
                  timeout=3600, max_seconds=0):
    """Stream a chat completion from the target backend; returns the full
    reply text. `target` comes from resolve_tier() — a legacy config dict
    (url+model, no kind) is treated as Ollama. `timeout` is how long the
    server may stay silent; `max_seconds` (0 = no limit) caps the whole
    generation."""
    deadline = time.time() + max_seconds if max_seconds else 0
    if target.get("giant"):
        # a giant: started (and others unloaded) on demand, and held so it
        # cannot be swapped out mid-answer
        cfg = config()
        try:
            with aria_garage.serving(target["giant"], cfg,
                                     notify=_garage_note):
                return _chat_call(target, messages, force_json, on_token,
                                  timeout, deadline)
        except aria_garage.GarageBusy as exc:
            raise BackendDown(str(exc))
        except aria_garage.GarageError as exc:
            _garage_note(f"{exc} — using the smart tier instead")
            target = resolve_tier("smart", cfg)
    return _chat_call(target, messages, force_json, on_token, timeout,
                      deadline)


GARAGE_LISTENERS = []           # callables that want giant load notices


def _garage_note(text):
    for fn in list(GARAGE_LISTENERS):
        try:
            fn(text)
        except Exception:
            pass


def _chat_call(target, messages, force_json, on_token, timeout, deadline):
    try:
        if target.get("kind", "ollama") == "openai":
            return _openai_stream(target, messages, force_json, on_token,
                                  timeout, deadline)
        return _ollama_stream(target, messages, force_json, on_token,
                              timeout, deadline)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if exc.code >= 500 or (exc.code == 503 and target.get("giant")):
            raise BackendDown(f"{target['model']}: HTTP {exc.code} {detail}")
        raise RuntimeError(f"{target['model']}: HTTP {exc.code} {detail}")
    except (urllib.error.URLError, ConnectionError, TimeoutError,
            json.JSONDecodeError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            raise BackendDown(f"{target['model']}: garbled stream ({exc})")
        raise BackendDown(f"{target['model']} unreachable at "
                          f"{target['url']}: {getattr(exc, 'reason', exc)}")


def _ollama_stream(target, messages, force_json, on_token, timeout, deadline):
    body = {"model": target["model"], "stream": True, "messages": messages}
    if force_json:
        body["format"] = "json"
    if target.get("num_ctx"):
        # without this Ollama silently truncates long prompts to its small
        # default window — fatal for a planner reading project state
        body["options"] = {"num_ctx": int(target["num_ctx"])}
    req = urllib.request.Request(
        target["url"] + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    parts = []
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
            tok = (data.get("message") or {}).get("content", "")
            if tok:
                parts.append(tok)
                if on_token:
                    on_token(tok)
            if data.get("done"):
                break
            if deadline and time.time() > deadline:
                raise GenerationTimeout(
                    f"{target['model']} ran past its time limit")
    return "".join(parts)


def _openai_stream(target, messages, force_json=False, on_token=None,
                   timeout=3600, deadline=0):
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
    parts = []
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
            tok = (choices[0].get("delta") or {}).get("content") or ""
            if tok:
                parts.append(tok)
                if on_token:
                    on_token(tok)
            if deadline and time.time() > deadline:
                raise GenerationTimeout(
                    f"{target['model']} ran past its time limit")
    return "".join(parts)


def ask_json(target, prompt, system, on_token=None, timeout=3600, tries=2):
    """Ask for JSON and get a parsed value back, re-asking once if the
    model answers with something that is not JSON."""
    last = ""
    for attempt in range(tries):
        last = chat_stream(
            target, prompt if attempt == 0 else
            prompt + "\n\nYour previous reply was not valid JSON. Reply "
                     "with ONLY the JSON object, nothing else.",
            system, force_json=True, on_token=on_token, timeout=timeout)
        data = parse_json_reply(last)
        if data is not None:
            return data
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
    for name in ("proposed-plan", "research"):
        f = proj / f"{name}.md"
        if f.exists():
            f.rename(proj / f"{name}.v{version}.md")

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
    for name in ("idea", "research", "proposed-plan", "requirements"):
        f = _project_doc(proj, name)
        if f:
            docs.append((name, f.read_text(encoding="utf-8"), False))
    auto = proj / "autopilot"
    for name, fname, limit in (("assessment", "ASSESSMENT.md", 40000),
                               ("memory", "MEMORY.md", 40000),
                               ("changelog", "CHANGELOG.md", 80000),
                               ("final-report", "FINAL-REPORT.md", 120000)):
        f = auto / fname
        if f.is_file():
            text = f.read_text(encoding="utf-8")
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
    running_projects = [p for p in list_projects()
                        if p["autopilot"] == "running"]
    if running_projects:
        parts.append("autopilot working on: " + ", ".join(
            f"{p['slug']} (iteration {p['iteration']}"
            + (f", now: {p['current']}" if p.get("current") else "") + ")"
            for p in running_projects))
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
INTENT_CHANGES = re.compile(
    r"^\s*(?:what(?:'s| has)? changed (?:in|on|with)|what did you "
    r"(?:do|change|build)(?: (?:in|on|to|for))?|(?:show (?:me )?)?(?:the )?"
    r"(?:changes|changelog|final report|report) (?:in|on|for|of))\s+(.+?)"
    r"\s*\??\s*$", re.I)
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
           "status of <project>", "what changed in <project>",
           "ask the <agent> to <task>", "mission <agent> to <task>",
           "use skill <name>", "drop skill",
           "open <chat|projects|board|agents|orb|settings>"]
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


def detect_intent(text):
    """Deterministic spoken-command detection, checked before the LLM."""
    cfg = config()
    text = (text or "").strip()
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
    elif action == "changes":
        out.update(slug=intent["slug"],
                   say=project_changes_text(intent["slug"]))
    elif action == "navigate":
        names = {"chat": "the chat", "projects": "Projects",
                 "board": "the pipeline board", "agents": "Agents",
                 "orb": "the Orb", "settings": "Settings"}
        out.update(view=intent["view"], tab=intent.get("tab", ""),
                   slug=intent.get("slug", ""),
                   say="Opening " + names.get(intent["view"], intent["view"])
                       + (f" for {intent['slug']}" if intent.get("slug")
                          else "") + ".")
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


def converse(messages, on_token=None, skill=None):
    """Persona chat (fast tier). With `skill`, that skill's instructions are
    loaded into the system prompt so any model — not just Claude — can
    follow them."""
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
    return strip_think(chat_messages(
        resolve_tier("fast", cfg),
        [{"role": "system", "content": system}] + trimmed,
        on_token=on_token))


# ---------------------------------------------------------------- local STT

_WHISPER = None


def transcribe_audio(data, suffix=".webm"):
    """Local speech-to-text via faster-whisper (optional dependency) —
    the voice-input path for browsers without Web Speech API (Firefox)."""
    global _WHISPER
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "local speech-to-text needs the faster-whisper package — "
            "install it with: pip install faster-whisper")
    if _WHISPER is None:
        _WHISPER = WhisperModel(config()["stt_model"],
                                device="cpu", compute_type="int8")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        segments, _info = _WHISPER.transcribe(tmp)
        return " ".join(s.text.strip() for s in segments).strip()
    finally:
        os.unlink(tmp)


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
    emit({"event": "stage_done", "stage": "agent", "agent": agent["name"],
          "log": f"agent {agent['name']} finished"})
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
        if re.match(r"(idea|research|proposed-plan|requirements)"
                    r"(\.v\d+)?\.md$", f.name):
            f.rename(docs / f.name)
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
            out.append({"slug": d.name, "kind": kind, "approved": approved,
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

def project_status_text(slug):
    """One or two speakable sentences on where a project stands."""
    p = next((x for x in list_projects() if x["slug"] == slug), None)
    if p is None:
        return f"I don't have a project called {slug}."
    name = slug.replace("-", " ")
    gated = p["needs_you"]
    needs = ""
    if gated:
        needs = (f" {len(gated)} task(s) need your decision: "
                 + "; ".join(f"{g['title']} ({g['id']})" for g in gated[:3])
                 + f". Say 'approve task {gated[0]['id']} in {slug}' — add "
                   "your decision after a colon — or 'skip task "
                   f"{gated[0]['id']} in {slug}'.")
    if p["autopilot"] == "running":
        say = (f"{name} is running, iteration {p['iteration']}: "
               f"{p['done']} of {p['total']} tasks done.")
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
        return (f"{name} {ending} after {p['iteration']} iteration(s) with "
                f"{p['done']} tasks done"
                + (f" and {p['failed']} it could not complete"
                   if p["failed"] else "")
                + ". The final report is on the Projects page." + needs)
    if not p["approved"]:
        return (f"{name} has a proposed plan waiting for your approval — "
                f"say 'approve {slug}' when you're happy with it."
                if p["has_plan"] else
                f"{name} is still an idea — run develop to get a plan.")
    return f"{name} is approved and idle — say 'work on {slug}' to start."


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
        parts.append(f"{len(waiting)} plan(s) are waiting for your approval: "
                     + ", ".join(p["slug"].replace("-", " ") for p in waiting)
                     + ".")
    for p in projects:              # decisions owed on projects not running
        if p["needs_you"] and p["autopilot"] != "running":
            parts.append(f"{p['slug'].replace('-', ' ')} has "
                         f"{len(p['needs_you'])} task(s) waiting for your "
                         "decision.")
    b = board()
    if b["inbox"]:
        parts.append(f"{len(b['inbox'])} note(s) are in the inbox.")
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
    return (f"{len(rows)} completed change(s) in {name}. Most recent: "
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
                         "(aria.github_access in system/config.yml)")
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


# ---------------------------------------------------------------- config edit

_BOOL_KEYS = {"auto_pipeline", "web_access", "github_access",
              "brain_journal", "autopilot_git"}
_CHOICE_KEYS = {
    "web_search": {"auto", "duckduckgo", "brave", "searxng", "wikipedia",
                   "none"},
    "autopilot_after_goal": {"improve", "stop"},
    "giants_use": {"auto", "always", "never"},
}
_NUMBER_KEYS = {"context_tokens", "autopilot_max_tasks",
                "autopilot_max_iterations", "autopilot_max_hours",
                "autopilot_stall_limit", "autopilot_idle_minutes",
                "giants_loaded", "giants_idle_minutes", "giants_ctx"}
_TEXT_KEYS = {"obsidian_vault", "fast", "smart", "deep", "stt_model",
              "web_search_url", "web_allowlist", "github_base_branch",
              "promotion_target", "giant_general", "giant_research",
              "giant_coder", "giants_flags"}
SETTABLE = _BOOL_KEYS | set(_CHOICE_KEYS) | _NUMBER_KEYS | _TEXT_KEYS


def config_set(key, value):
    """Update a single allowed key in system/config.yml in place."""
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
        if key in ("autopilot_max_tasks", "autopilot_max_iterations",
                   "autopilot_stall_limit", "giants_loaded", "giants_ctx"):
            number = int(number)                  # whole tasks, whole batches
        value = str(int(number)) if number == int(number) else str(number)
    elif "#" in value:
        raise ValueError(f"{key} cannot contain '#'")
    elif key.startswith("giant_") and value:
        path = Path(os.path.expanduser(value))
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file() or path.suffix.lower() != ".gguf":
            raise ValueError(f"{value} is not a .gguf file — point at the "
                             "first part of the model (…-00001-of-0000N.gguf)")
    path = ROOT / "system" / "config.yml"
    text = path.read_text(encoding="utf-8")
    line = f"{key}: {value}".rstrip()
    pattern = re.compile(rf"^([ \t]*){re.escape(key)}:.*$", re.M)
    if pattern.search(text):
        text = pattern.sub(lambda m: m.group(1) + line, text, count=1)
    else:
        text = text.rstrip("\n") + f"\n  {line}\n"
    path.write_text(text, encoding="utf-8")


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
    return {"ok": fast_probe["ok"], "server": fast_probe["server"],
            "model": fast["model"], "url": fast["url"],
            "models": fast_probe.get("models", []),
            "error": fast_probe.get("error", ""),
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
