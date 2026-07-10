"""A.R.I.A. engine library — the local (Ollama) pipeline stages as importable
functions that report progress through an `emit(event)` callback.

Used by bin/aria-ollama (CLI, prints event logs) and bin/aria-ui (web UI,
streams events to the browser so you can watch the model think).

Events are dicts with an "event" key; most carry a human-readable "log":
  start / note / prompt / thinking / token / decision / question /
  moved / file_written / error / stage_done
"""

import datetime
import json
import os
import re
import urllib.request
from pathlib import Path

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
        m = re.search(rf"^\s*{re.escape(key)}:\s*(\S.*?)\s*$", text, re.M)
        return m.group(1) if m else default

    return {
        "url": os.environ.get(
            "ARIA_OLLAMA_URL", find("base_url", "http://localhost:11434")
        ).rstrip("/"),
        "model": os.environ.get("ARIA_OLLAMA_MODEL", find("model", "llama3.1:8b")),
        "threshold": float(find("triage_confidence_threshold", "0.7")),
        "promotion_target": find("promotion_target", "./active-projects"),
        "auto_pipeline": find("auto_pipeline", "true").lower()
            in ("true", "yes", "on", "1"),
        "obsidian_vault": os.environ.get(
            "ARIA_OBSIDIAN_VAULT", find("obsidian_vault", "")),
    }


def chat_stream(cfg, prompt, system=None, force_json=False, on_token=None):
    """Call Ollama /api/chat with streaming; returns the full reply text."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": cfg["model"], "stream": True, "messages": messages}
    if force_json:
        body["format"] = "json"
    req = urllib.request.Request(
        cfg["url"] + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    parts = []
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            raw = raw.strip()
            if not raw:
                continue
            data = json.loads(raw)
            tok = (data.get("message") or {}).get("content", "")
            if tok:
                parts.append(tok)
                if on_token:
                    on_token(tok)
            if data.get("done"):
                break
    return "".join(parts)


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


def classify(cfg, body, on_token=None):
    reply = chat_stream(cfg, "Classify this note:\n\n" + body.strip(),
                        TRIAGE_SYSTEM, force_json=True, on_token=on_token)
    data = json.loads(reply)
    if data.get("type") not in CATEGORIES:
        raise ValueError(f"model returned unknown type {data.get('type')!r}")
    data["confidence"] = float(data.get("confidence", 0))
    return data


def triage_run(emit, files=None):
    cfg = config()
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
          "log": f"triaging {len(files)} inbox note(s) with {cfg['model']}"})
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
            result = classify(cfg, body,
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
    project — the 'idea goes in, proposed plan comes out' path."""
    result = triage_run(emit, files=files)
    if result["projects"]:
        develop_run(emit, slugs=result["projects"])
    return result


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


def develop_run(emit, slugs=None):
    cfg = config()
    files = sorted(p for p in PROCESSING.glob("*.md") if p.name != "README.md")
    if slugs:
        wanted = set(slugs)
        files = [f for f in files if f.stem in wanted]
    if not files:
        emit({"event": "stage_done", "stage": "develop",
              "log": "nothing in 01-processing to develop"})
        return 0

    emit({"event": "start", "stage": "develop", "count": len(files),
          "log": f"developing {len(files)} idea(s) with {cfg['model']}"})
    today = datetime.date.today().isoformat()
    for f in files:
        fm, _, body = parse_note(f)
        slug = fm.get("slug") or f.stem
        captured = (fm.get("captured") or "?")[:10]
        emit({"event": "note", "file": f.name, "excerpt": excerpt(body),
              "log": f"developing {slug}"})
        try:
            emit({"event": "prompt", "label": "plan prompt",
                  "text": PLANNER_SYSTEM + "\n\n---\n\nWrite the proposed plan for this idea:\n\n"
                          + body.strip()})
            emit({"event": "thinking", "label": "drafting proposed plan"})
            plan_body = chat_stream(
                cfg, "Write the proposed plan for this idea:\n\n" + body.strip(),
                PLANNER_SYSTEM,
                on_token=lambda t: emit({"event": "token", "text": t}))
            emit({"event": "prompt", "label": "research prompt",
                  "text": RESEARCH_PROMPT + body.strip()})
            emit({"event": "thinking", "label": "recalling prior art (unverified — no web access)"})
            research_body = chat_stream(
                cfg, RESEARCH_PROMPT + body.strip(),
                on_token=lambda t: emit({"event": "token", "text": t}))
        except Exception as exc:
            emit({"event": "error", "file": f.name,
                  "log": f"ERROR  {f.name}: {exc} — left in 01-processing untouched"})
            continue

        proj = POTENTIAL / slug
        proj.mkdir(parents=True, exist_ok=True)
        f.rename(proj / "idea.md")
        (proj / "research.md").write_text(
            f"---\nslug: {slug}\ntype: research\ncreated: {today}\n"
            f"engine: ollama:{cfg['model']}\n---\n\n{RESEARCH_DISCLAIMER}\n"
            + research_body.strip() + "\n",
            encoding="utf-8",
        )
        (proj / "proposed-plan.md").write_text(
            f"---\nslug: {slug}\nstatus: proposed\ncreated: {today}\n"
            f"updated: {today}\nengine: ollama:{cfg['model']}\n---\n\n"
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

    emit({"event": "stage_done", "stage": "develop",
          "log": "drafts ready — review with /review <slug> in Claude Code"})
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


# ---------------------------------------------------------------- obsidian

def export_obsidian(slug):
    """Publish a potential project into the Obsidian vault configured at
    aria.obsidian_vault: one folder of prefixed notes plus an index note
    with wikilinks and tags, so it joins the second-brain graph."""
    cfg = config()
    if not cfg["obsidian_vault"]:
        raise ValueError("obsidian_vault is not set in system/config.yml — "
                         "point it at your vault to enable export")
    vault = Path(os.path.expanduser(cfg["obsidian_vault"]))
    if not vault.is_dir():
        raise ValueError(f"obsidian vault not found: {vault}")
    proj = POTENTIAL / slug
    if not proj.is_dir():
        raise ValueError(f"no such potential project: {slug}")

    today = datetime.date.today().isoformat()
    dest = vault / "ARIA" / slug
    dest.mkdir(parents=True, exist_ok=True)
    links = []
    for name in ("idea", "research", "proposed-plan", "requirements"):
        src = proj / f"{name}.md"
        if src.exists():
            (dest / f"{slug}-{name}.md").write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8")
            links.append(f"- [[{slug}-{name}|{name.replace('-', ' ').title()}]]")

    plan = proj / "proposed-plan.md"
    pitch = ""
    if plan.exists():
        m = re.search(r"^\s*>\s*\*\*Pitch:\*\*\s*(.+)$",
                      plan.read_text(encoding="utf-8"), re.M)
        pitch = m.group(1).strip() if m else ""

    index = vault / "ARIA" / f"{slug}.md"
    index.write_text(
        f"---\ntags: [aria, project]\naria-slug: {slug}\nexported: {today}\n"
        f"---\n\n# {slug.replace('-', ' ').title()}\n\n"
        + (f"> {pitch}\n\n" if pitch else "")
        + "\n".join(links)
        + f"\n\nSource: A.R.I.A. pipeline — `pipeline/02-potential-projects/{slug}/`\n",
        encoding="utf-8",
    )
    if plan.exists():
        update_frontmatter(plan, exported=today)
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

    cfg = config()
    target = Path(cfg["promotion_target"])
    if not target.is_absolute():
        target = (ROOT / target).resolve()
    active = []
    if target.is_dir():
        for d in sorted(target.iterdir()):
            status_file = d / "STATUS.md"
            if not d.is_dir() or not status_file.exists():
                continue
            text = status_file.read_text(encoding="utf-8")
            phase = re.search(r"\*\*Phase:\*\*\s*(.+)", text)
            active.append({"slug": d.name,
                           "phase": phase.group(1).strip() if phase else "?"})

    return {
        "inbox": _note_cards(INBOX),
        "processing": _note_cards(PROCESSING),
        "potential": potential,
        "knowledge": knowledge,
        "open_tasks": open_tasks,
        "active": active,
    }


def health():
    cfg = config()
    extras = {"auto_pipeline": cfg["auto_pipeline"],
              "obsidian": bool(cfg["obsidian_vault"])}
    try:
        with urllib.request.urlopen(cfg["url"] + "/api/tags", timeout=5) as resp:
            models = [m.get("name", "") for m in json.load(resp).get("models", [])]
        model_ok = any(m == cfg["model"] or m.startswith(cfg["model"] + ":")
                       for m in models)
        return {"ok": model_ok, "server": True, "model": cfg["model"],
                "url": cfg["url"], "models": models, **extras}
    except Exception as exc:
        return {"ok": False, "server": False, "model": cfg["model"],
                "url": cfg["url"], "error": str(exc), **extras}
