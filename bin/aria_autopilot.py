"""A.R.I.A. autopilot — autonomous, recursive project execution.

    assess -> plan -> [ execute -> verify -> record ]* -> reflect -> plan …

…until the owner says stop (the task in flight finishes, the report is
written, the project is left exactly as it is), the goal is met and
audited, or the loop notices it has stopped making progress.

How one project is worked
  1. ASSESS   What needs doing, what the agents/skills/models/tools on hand
              can do about it, and what they cannot.  -> ASSESSMENT.md
  2. PLAN     A batch of tasks; each names its agent, skills, model, the
              files it must read, what it depends on, why it exists and how
              to tell it is done.                      -> plan.json
  3. EXECUTE  One task at a time, dependency order. The agent sees the goal,
              the project memory, its dependencies' output and the files it
              asked for; it answers with whole files in `file:` blocks.
  4. VERIFY   Mechanical checks first (does the code even parse?), then a
              judge against the task's own done_when. A failed attempt is
              rolled back and retried with the critique; three strikes and
              the task is marked failed for the planner to route around.
              So: every change left in the project passed verification.
  5. RECORD   changes.jsonl (structured, complete), CHANGELOG.md (readable),
              a commit per task when the project is its own git repository.
  6. REFLECT  At the end of a batch the memory is rewritten — state,
              decisions, lessons — so the next plan starts from what was
              learned instead of an ever-growing transcript.
  7. REPEAT   Next batch. When the planner says the goal is met an audit
              checks that against the requirements; gaps become the next
              batch, otherwise she moves on to improvements (or stops, per
              aria.autopilot_after_goal).

Everything lives in <project>/autopilot/. The loop is driven by tick(): one
call = one step, so a crash or restart loses at most the step in flight.
"""

import contextlib
import datetime
import difflib
import fcntl
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import aria_engine as eng
import aria_git

MAX_ATTEMPTS = 3            # generations a task gets before it is failed
MAX_FILE_BYTES = 400_000    # per file an agent may write
MAX_FILES_PER_TASK = 25
KEEP_FINISHED = 40          # finished tasks kept on the board; older archive
LONG_TIMEOUT = 7 * 24 * 3600

SKIP_DIRS = {"autopilot", ".aria", ".git", "node_modules", "__pycache__",
             "crew", ".venv", "venv", "dist", "build", ".claude", ".idea",
             ".pytest_cache", "target"}
PROTECTED = ("autopilot/", ".aria/", ".git/", ".claude/", "docs/requirements.md",
             "docs/idea.md", "docs/research.md", "docs/proposed-plan.md",
             "requirements.md", "idea.md", "research.md", "proposed-plan.md",
             "CLAUDE.md")
TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".html", ".htm",
    ".css", ".scss", ".sh", ".bash", ".sql", ".rs", ".go", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".lua",
    ".r", ".csv", ".xml", ".svg", ".env.example", ".gitignore", ".vue",
    ".svelte", ".tf", ".dockerfile", ".gd", ".dart", ".ex", ".exs", ""}

LIVE = {}                   # slug -> what is being generated right now
_LIVE_LOCK = threading.Lock()


class Busy(RuntimeError):
    """Another worker holds this project."""


# ------------------------------------------------------------ small tools

def _stamp():
    return f"{datetime.datetime.now():%Y-%m-%dT%H:%M:%S}"


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)             # readers never see a half-written file


def _auto(slug):
    return eng._auto_dir(slug)


def _proj(slug):
    return eng._project_dir(slug).resolve()


@contextlib.contextmanager
def _locked(slug):
    """One worker per project — across threads and processes."""
    fh = open(_auto(slug) / "LOCK", "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy(f"{slug} is being worked on right now")
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _state(slug):
    state = eng._auto_state(slug)
    for key, default in (("status", "idle"), ("iteration", 0), ("run", 0),
                         ("next_id", 1), ("mode", "build"), ("dry", 0),
                         ("errors", 0), ("audits", 0), ("phase", ""),
                         ("waiting", ""), ("backoff_until", 0)):
        state.setdefault(key, default)
    state.setdefault("stats", {})
    for key in ("done", "failed", "skipped", "files", "calls"):
        state["stats"].setdefault(key, 0)
    return state


def _save_state(slug, state):
    state["updated"] = _stamp()
    _write_json(_auto(slug) / "state.json", state)


def _plan(slug):
    plan = _read_json(_auto(slug) / "plan.json", {"tasks": []})
    plan.setdefault("tasks", [])
    for t in plan["tasks"]:               # tolerate plans from older versions
        if t.get("status") == "blocked":
            t["status"] = "todo"
        t.setdefault("attempts", 0)
        t.setdefault("iteration", 0)
        t.setdefault("depends_on", [])
        t.setdefault("skills", [])
        t.setdefault("reads", [])
    return plan


def _save_plan(slug, plan):
    _write_json(_auto(slug) / "plan.json", plan)


# ------------------------------------------------------------ activity

def _activity(slug, kind, text, **extra):
    """Append to the project's activity feed (what the Live tab shows)."""
    path = _auto(slug) / "activity.jsonl"
    row = {"t": _stamp(), "kind": kind, "text": " ".join(str(text).split()),
           **extra}
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        if path.stat().st_size > 600_000:      # keep the feed bounded
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join(lines[len(lines) // 2:]) + "\n",
                            encoding="utf-8")
    except OSError:
        pass


def activity(slug, limit=150):
    path = _auto(slug) / "activity.jsonl"
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def live(slug):
    with _LIVE_LOCK:
        return dict(LIVE.get(slug) or {})


def _emitter(slug, emit):
    """Wrap the caller's emit: log lines go to the activity feed, tokens to
    the live buffer."""
    def wrapped(ev):
        kind = ev.get("event")
        if kind == "token":
            with _LIVE_LOCK:
                cur = LIVE.setdefault(slug, {"label": "", "text": ""})
                cur["text"] = (cur["text"] + ev.get("text", ""))[-6000:]
        elif kind == "thinking":
            with _LIVE_LOCK:
                LIVE[slug] = {"label": ev.get("label", ""), "text": "",
                              "at": time.time()}
        if ev.get("log") and kind != "token" and ev.get("stage") != "agent":
            _activity(slug, "error" if kind == "error" else
                      ("file" if kind == "file_written" else "info"),
                      ev["log"])
        try:
            emit(ev)
        except Exception:
            pass
    return wrapped


# ------------------------------------------------------------ project files

def _is_text(path):
    name = path.name.lower()
    if path.suffix.lower() in TEXT_SUFFIXES or name in (
            "makefile", "dockerfile", "license", "readme"):
        try:
            return b"\x00" not in path.open("rb").read(2048)
        except OSError:
            return False
    return False


def inventory(proj, limit=200):
    """The project's own files (not her bookkeeping): path, bytes, lines."""
    rows = []
    for root, dirs, files in os.walk(proj):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            f = Path(root) / name
            rel = f.relative_to(proj).as_posix()
            try:
                size = f.stat().st_size
            except OSError:
                continue
            lines = 0
            if size < 2_000_000 and _is_text(f):
                try:
                    lines = f.read_text(encoding="utf-8",
                                        errors="replace").count("\n") + 1
                except OSError:
                    pass
            rows.append({"path": rel, "bytes": size, "lines": lines})
            if len(rows) >= limit:
                return rows
    return rows


def _inventory_text(rows, budget=1500):
    if not rows:
        return "(no project files yet)"
    lines = [f"{r['path']} ({r['lines']} lines)" if r["lines"]
             else f"{r['path']} ({r['bytes']} bytes)" for r in rows]
    text = "\n".join(lines)
    if len(text) > budget:
        kept = []
        for ln in lines:
            if sum(len(k) + 1 for k in kept) + len(ln) > budget - 40:
                break
            kept.append(ln)
        text = "\n".join(kept) + f"\n… and {len(lines) - len(kept)} more"
    return text


def _protected(rel):
    low = rel.lower()
    return any(low == p.lower() or low.startswith(p.lower())
               for p in PROTECTED if p.endswith("/")) or \
        any(low == p.lower() for p in PROTECTED if not p.endswith("/"))


_FENCE = re.compile(r"^(`{3,}|~{3,})\s*(?:[\w+-]+\s+)?file:\s*(.+?)\s*$")
_ANY_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")


def parse_file_blocks(output):
    """[(path, content, closed)] for every fenced block whose opening line
    is ```file:relative/path. Understands longer fences (````), and nested
    code blocks that carry an info string — so a README with ```bash
    examples survives being wrapped in a three-backtick file block."""
    lines = output.split("\n")
    blocks, i = [], 0
    while i < len(lines):
        m = _FENCE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        fence, path = m.group(1), m.group(2).strip().strip("`'\"*")
        body, depth, closed = [], 0, False
        i += 1
        while i < len(lines):
            st = lines[i].strip()
            fm = _ANY_FENCE.match(st)
            if fm and fm.group(1)[0] == fence[0] \
                    and len(fm.group(1)) >= len(fence):
                info = fm.group(2).strip()
                if info:
                    depth += 1                 # a nested block opens
                elif depth:
                    depth -= 1                 # …and closes
                else:
                    closed = True
                    break
            body.append(lines[i])
            i += 1
        blocks.append((path, "\n".join(body) + "\n", closed))
        i += 1
    return blocks


def write_files(slug, blocks, emit):
    """Write an agent's file blocks into the project. Confined to the
    project folder; her own records, the approved requirements and the
    pipeline documents are off limits. Returns one record per file,
    carrying what was there before so a failed attempt can be undone."""
    proj = _proj(slug)
    records = []
    for rel, content, closed in blocks[:MAX_FILES_PER_TASK]:
        rel = rel.replace("\\", "/").strip()
        while rel.startswith("./"):
            rel = rel[2:]
        dest = (proj / rel).resolve() if rel else proj
        if (not rel or rel.startswith("/") or rel.endswith("/")
                or proj not in dest.parents):
            emit({"event": "error",
                  "log": f"refused unsafe file path from agent: {rel!r}"})
            continue
        rel = dest.relative_to(proj).as_posix()
        if _protected(rel):
            emit({"event": "error",
                  "log": f"refused to overwrite protected file: {rel}"})
            continue
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            emit({"event": "error",
                  "log": f"refused oversized file ({rel})"})
            continue
        old = None
        if dest.is_file():
            try:
                old = dest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                old = ""
        elif dest.exists():
            emit({"event": "error",
                  "log": f"refused: {rel} is a directory"})
            continue
        new_lines = content.count("\n")
        if old == content:
            records.append({"path": rel, "action": "unchanged",
                            "lines": new_lines, "added": 0, "removed": 0,
                            "closed": closed, "old": old})
            continue
        if old is None:
            added, removed = new_lines, 0
        else:
            a, b = old.splitlines(), content.splitlines()
            if len(a) + len(b) > 8000:
                added, removed = max(0, len(b) - len(a)), max(0, len(a) - len(b))
            else:
                added = removed = 0
                for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                        None, a, b, autojunk=False).get_opcodes():
                    if tag in ("replace", "delete"):
                        removed += i2 - i1
                    if tag in ("replace", "insert"):
                        added += j2 - j1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        records.append({"path": rel,
                        "action": "created" if old is None else "modified",
                        "lines": new_lines, "added": added,
                        "removed": removed, "closed": closed, "old": old})
        emit({"event": "file_written", "file": rel,
              "log": f"wrote {rel} ({'new' if old is None else 'updated'}, "
                     f"{new_lines} lines)"})
    return records


def _revert(slug, records):
    """Undo a failed attempt's writes."""
    proj = _proj(slug)
    for r in records:
        if r["action"] == "unchanged":
            continue
        dest = proj / r["path"]
        try:
            if r["old"] is None:
                dest.unlink(missing_ok=True)
                parent = dest.parent      # tidy folders the attempt created
                while parent != proj and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
            else:
                dest.write_text(r["old"], encoding="utf-8")
        except OSError:
            pass


def static_checks(slug, records):
    """Mechanical problems in what was written — things no judge needs to
    be asked about. Parsing only; nothing is executed."""
    proj = _proj(slug)
    problems = []
    for r in records:
        if not r["closed"]:
            problems.append(f"{r['path']}: the file block was never closed "
                            "— the output was cut off mid-file")
        if r["action"] == "unchanged":
            continue
        path = proj / r["path"]
        suffix = path.suffix.lower()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not text.strip():
            problems.append(f"{r['path']}: the file is empty")
        elif suffix == ".py":
            try:
                compile(text, r["path"], "exec")
            except SyntaxError as exc:
                problems.append(f"{r['path']}: Python syntax error on line "
                                f"{exc.lineno}: {exc.msg}")
            except ValueError as exc:
                problems.append(f"{r['path']}: {exc}")
        elif suffix == ".json":
            try:
                json.loads(text)
            except ValueError as exc:
                problems.append(f"{r['path']}: invalid JSON — {exc}")
        elif suffix in (".sh", ".bash") and shutil.which("bash"):
            res = subprocess.run(["bash", "-n", str(path)], text=True,
                                 capture_output=True, timeout=15)
            if res.returncode:
                problems.append(f"{r['path']}: shell syntax error — "
                                + res.stderr.strip().splitlines()[-1][:160])
        elif suffix in (".js", ".mjs", ".cjs") and shutil.which("node"):
            res = subprocess.run(["node", "--check", str(path)], text=True,
                                 capture_output=True, timeout=20)
            if res.returncode:
                err = [ln for ln in res.stderr.splitlines()
                       if "Error" in ln]
                problems.append(f"{r['path']}: JavaScript syntax error — "
                                + (err[0] if err else "does not parse")[:160])
    return problems


def _read_for_task(slug, paths, budget):
    """Contents of the files a task needs to see, within a budget."""
    proj = _proj(slug)
    wanted, seen = [], set()
    for p in paths:
        p = str(p).replace("\\", "/").lstrip("./")
        f = (proj / p).resolve()
        if p and p not in seen and proj in f.parents and f.is_file() \
                and _is_text(f):
            seen.add(p)
            wanted.append((p, f))
    if not wanted or budget < 300:
        return ""
    each = max(400, budget // len(wanted))
    out = []
    for p, f in wanted:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.append(f"--- {p} ---\n{eng.clip(text, each)}")
        if sum(len(x) for x in out) > budget:
            break
    return "\n\n".join(out)


def _mentioned_files(text, rows):
    """Existing project files a task's wording refers to by name."""
    hits = []
    for r in rows:
        name = r["path"]
        if name in text or (len(Path(name).name) > 5
                            and Path(name).name in text):
            hits.append(name)
    return hits[:6]


# ------------------------------------------------------------ capabilities

def capabilities(goal_text, cfg=None):
    """What she has to work with, sized to the project in front of her:
    the standing team, library specialists and skills relevant to the
    goal, the models on hand, and the tools that are switched on."""
    cfg = cfg or eng.config()
    agents = [{"name": a["name"], "role": a["role"], "model": a["model"],
               "library": False} for a in eng.list_agents()]
    for hit in eng.relevant_agents(goal_text, k=8):
        a = eng.get_agent(hit["name"])
        if a:
            agents.append({"name": a["name"], "role": a["role"],
                           "model": a["model"], "library": True})
    skills = [{"name": s["name"], "description": s.get("description", "")}
              for s in eng.relevant_skills(goal_text, k=14)]
    tiers = {t: eng.resolve_tier(t, cfg).get("spec", "")
             for t in ("fast", "smart", "deep")}
    extra = [m for m in eng.installed_models(cfg)
             if m["spec"] not in tiers.values()][:12]
    tools = ["write and rewrite project files (whole files, verified "
             "before they are kept)",
             "check code for syntax errors (Python, JSON, shell, "
             "JavaScript) — parsing only"]
    limits = ["cannot run or test code, install packages, or use a shell",
              "cannot see images, audio or binary files"]
    if eng.web_ready(cfg):
        tools.append("live web research (search + read pages) for tasks "
                     "marked research")
    else:
        limits.append("no web access — facts are recalled, not looked up")
    if cfg["autopilot_git"]:
        tools.append("a git commit per verified task when the project is "
                     "its own repository"
                     + (", pushed to GitHub as a pull request"
                        if cfg["github_access"] else ""))
    if eng.brain_ready(cfg):
        tools.append("notes mirrored into the owner's Obsidian vault")
    return {"agents": agents, "skills": skills, "tiers": tiers,
            "models": extra, "tools": tools, "limits": limits}


def capabilities_text(cap, budget=3200):
    lines = ["AGENTS (name — what it is for — default model):"]
    lines += [f"- {a['name']} — {eng.clip(a['role'], 110)} — {a['model']}"
              + (" [library specialist]" if a["library"] else "")
              for a in cap["agents"]]
    lines.append("\nSKILLS (optional know-how a task can load):")
    lines += [f"- {s['name']} — {eng.clip(s['description'], 110)}"
              for s in cap["skills"]] or ["- (none installed)"]
    lines.append("\nMODELS:")
    lines += [f"- {tier} = {spec}" + {
        "fast": "  (quick drafting, summaries, simple edits)",
        "smart": "  (default: planning, code, analysis)",
        "deep": "  (slowest and strongest: only for the hardest reasoning)",
    }[tier] for tier, spec in cap["tiers"].items()]
    lines += [f"- {m['spec']}"
              + (f"  ({m['size_gb']} GB)" if m.get("size_gb") else "")
              for m in cap["models"]]
    lines.append("\nTOOLS:")
    lines += [f"- {t}" for t in cap["tools"]]
    lines.append("\nLIMITS:")
    lines += [f"- {t}" for t in cap["limits"]]
    return eng.clip("\n".join(lines), budget)


# ------------------------------------------------------------ prompts

ASSESS_SYSTEM = """You are A.R.I.A.'s lead planner, about to run a project
autonomously. Before planning anything, take stock. Write a short assessment
in markdown with exactly these four headings:

## What needs to be done
The deliverables and the real work behind them — 4 to 10 concrete bullets.

## What I can do with what I have
Which of the available agents, skills, models and tools cover which part.
Name them.

## Gaps and risks
What the available tools CANNOT do for this project — be honest — and how
each gap will be worked around or left for the owner.

## Approach
The order of attack, 3 to 6 steps.

Be specific to THIS project. No preamble, no closing remarks."""

PLAN_SYSTEM = """You are the project planner inside A.R.I.A. Plan the next
batch of work as tasks that an AI agent completes by writing documents,
designs or code files. Respond with ONLY a JSON object:

{"tasks": [{"id": "a", "title": "short imperative title",
  "detail": "exactly what to produce, naming the file paths",
  "why": "why this task matters now, and why this agent and model",
  "agent": "<name from AGENTS>", "skills": ["<name from SKILLS>"],
  "model": "", "phase": "<short phase name>", "est_hours": 2,
  "depends_on": ["<id of an earlier task in this list>"],
  "reads": ["<existing project file this task must see>"],
  "research": false,
  "done_when": "<observable criterion a reviewer can check>"}],
 "project_complete": false}

Rules:
- 3 to 8 tasks. Each is concrete, self-contained, and produces one thing.
- "agent": pick the specialist that fits the task from AGENTS.
- "skills": up to 3 names from SKILLS that genuinely help; otherwise [].
- "model": "" uses the agent's default. Set "fast", "smart" or "deep" (or a
  listed backend/model) only when the task clearly needs less or more than
  the default — fast for simple drafting, deep for the hardest reasoning.
- "depends_on": ids from THIS list whose output the task builds on.
- "reads": files from FILES that the task must see to do its work.
- "research": true only for tasks that need facts from the live web%s
- Never repeat finished work; build on the files that exist.
- If the goal is completely met and nothing worthwhile remains, answer
  {"tasks": [], "project_complete": true}."""

TASK_TEMPLATE = """You are working autonomously inside the project "{slug}".

PROJECT GOAL (summary)
{goal}

YOUR TASK: {title}
Detail: {detail}
Done when: {done_when}
{context}
HOW TO DELIVER
- To create or change a project file, output its COMPLETE new contents in a
  fenced block whose opening line is exactly ```file:relative/path.ext —
  one block per file. If the file itself contains ``` fences, open and
  close its block with four backticks instead.
- Finish with a section headed "## Notes": what you did, why you did it
  that way, and what you relied on (files, sources).
Deliver the finished work now — no questions, no placeholders, nothing left
"to do later"."""

VERIFY_SYSTEM = """You are the verification judge for an autonomous project
run. Decide whether the work satisfies the task's "done when" criterion.
Respond with ONLY JSON: {"pass": true or false, "feedback": "<one or two
sentences: if it fails, exactly what is missing or wrong>"}.
Judge substance, not style. A genuine, complete attempt that meets the
criterion passes; placeholders, stubs, missing files or work that ignores
the task fail."""

AUDIT_SYSTEM = """You audit whether a project's requirements are met by the
work that exists. Respond with ONLY JSON:
{"met": true or false, "gaps": ["<specific thing that is missing or weak>"]}
Judge against the requirements' deliverables and acceptance criteria. A gap
names what is missing — at most 6, most important first. If everything
required exists and is complete, answer {"met": true, "gaps": []}."""

MEMORY_SYSTEM = """You keep the working memory of an autonomous project
run. Rewrite it so a planner can pick the project up cold. Markdown, at
most 350 words, with exactly these headings:

## State of the project
## Decisions made
## Lessons
## Still to do

Keep what is still true from the old memory, add what this batch changed,
drop what is obsolete. Under Lessons record what failed or was rejected and
what to do differently. Facts only — never invent files or results."""

SUMMARY_SYSTEM = """You write the opening summary of a report on an
autonomous project run, for the project's owner. At most 170 words of plain
prose, no headings or lists: what was accomplished, what state the project
is in now, and anything the owner should check or worry about. Only use
what you are given."""


# ------------------------------------------------------------ assess

def assess(slug, emit):
    """Step 1: take stock. Writes ASSESSMENT.md and returns its text."""
    cfg = eng.config()
    goal = eng._project_goal(slug, 3500)
    cap = capabilities(goal, cfg)
    rows = inventory(_proj(slug))
    prompt = (f"PROJECT: {slug}\n\nGOAL / REQUIREMENTS\n{goal}\n\n"
              f"WHAT I HAVE TO WORK WITH\n{capabilities_text(cap)}\n\n"
              f"FILES THAT ALREADY EXIST\n{_inventory_text(rows, 1200)}"
              + _steering_text(slug))
    emit({"event": "thinking", "label": f"assessing {slug}"})
    text = eng.strip_think(eng.chat_stream(
        eng.resolve_tier("smart", cfg), prompt, ASSESS_SYSTEM,
        on_token=lambda t: emit({"event": "token", "text": t}),
        timeout=_idle_timeout(cfg, "smart")))
    if len(text.strip()) < 80:
        raise ValueError("the assessment came back empty")
    (_auto(slug) / "ASSESSMENT.md").write_text(
        f"# Assessment — {slug}\n\n*Written {_stamp()[:16].replace('T', ' ')}"
        f" before planning.*\n\n{text.strip()}\n", encoding="utf-8")
    emit({"event": "stage_done", "stage": "assess",
          "log": f"{slug}: assessment written (what needs doing, what I can "
                 "do, the gaps)"})
    return text


def _steering_text(slug, limit=8):
    f = _auto(slug) / "STEERING.md"
    if not f.is_file():
        return ""
    notes = [ln for ln in f.read_text(encoding="utf-8").splitlines()
             if ln.startswith("- ")][-limit:]
    if not notes:
        return ""
    return ("\n\nGUIDANCE FROM THE OWNER (binding, newest last)\n"
            + "\n".join(notes))


# ------------------------------------------------------------ plan

def _idle_timeout(cfg, model_spec):
    """How long a backend may stay silent before the attempt is abandoned.
    The deep tier and hand-picked models keep the old week-long patience:
    a giant model can chew on a prompt for an hour before its first token."""
    if model_spec in ("fast", "smart", ""):
        return max(60, int(cfg["autopilot_idle_minutes"] * 60))
    return LONG_TIMEOUT


def _guess_agent(text, names):
    text = text.lower()
    table = (("coder", r"\b(code|implement|script|function|class|api|bug|"
                       r"test|build|program|parser|endpoint|refactor|cli)\b"),
             ("researcher", r"\b(research|investigat|compar|market|survey|"
                            r"prior art|competitor|find out|look up)\w*"),
             ("critic", r"\b(review|critique|audit|red.?team|verify|"
                        r"validate|check)\w*"),
             ("scribe", r"\b(document|readme|summar|write.?up|notes|report|"
                        r"changelog|guide)\w*"),
             ("strategist", r"\b(strategy|architect|trade.?off|decide|"
                            r"business model)\w*"))
    for name, pattern in table:
        if name in names and re.search(pattern, text):
            return name
    return "planner" if "planner" in names else sorted(names)[0]


def _valid_model(spec, cfg, installed):
    spec = (spec or "").strip()
    if spec in ("", "fast", "smart", "deep"):
        return spec
    target = eng._parse_spec(spec, cfg)
    if target is None:
        return ""
    if target["kind"] == "ollama":
        have = [m["spec"].split("/", 1)[1] for m in installed
                if m["spec"].startswith("ollama/")]
        if have and not any(h == target["model"]
                            or h.startswith(target["model"] + ":")
                            for h in have):
            return ""
    return spec


def _norm_title(title):
    return " ".join(re.findall(r"[a-z0-9]+", title.lower()))


def _break_cycles(tasks):
    """Drop dependency edges that would deadlock the batch."""
    by = {t["id"]: t for t in tasks}
    state = {}

    def visit(tid):
        state[tid] = 1
        for dep in list(by[tid]["depends_on"]):
            if dep not in by:
                continue
            if state.get(dep) == 1:
                by[tid]["depends_on"].remove(dep)
            elif dep not in state:
                visit(dep)
        state[tid] = 2

    for t in tasks:
        if t["id"] not in state:
            visit(t["id"])


def _plan_next(slug, state, emit, cfg, replace_unstarted=False):
    """Ask the planner for the next batch and merge it into plan.json.
    Returns (number of new tasks, planner says the project is complete)."""
    plan = _plan(slug)
    goal = eng._project_goal(slug, 3000)
    cap = capabilities(goal, cfg)
    rows = inventory(_proj(slug))
    auto = _auto(slug)
    budget = eng.prompt_budget(cfg)
    web_rule = "." if eng.web_ready(cfg) else \
        " — web access is OFF, so always false."
    system = PLAN_SYSTEM % web_rule

    sections = [f"PROJECT: {slug}",
                f"GOAL / REQUIREMENTS\n{goal}"]
    memory = (auto / "MEMORY.md").read_text(encoding="utf-8") \
        if (auto / "MEMORY.md").is_file() else ""
    if memory:
        sections.append("PROJECT MEMORY (what has happened so far)\n"
                        + eng.clip(memory, 2600))
    elif (auto / "ASSESSMENT.md").is_file():
        sections.append("YOUR ASSESSMENT\n" + eng.clip(
            (auto / "ASSESSMENT.md").read_text(encoding="utf-8"), 2600))
    sections.append("FILES (what exists now)\n" + _inventory_text(rows, 1500))
    finished = [t for t in plan["tasks"] if t["status"] in ("done", "failed")]
    if finished:
        recent = finished[-14:]
        sections.append("RECENT RESULTS\n" + "\n".join(
            f"- {t['title']} — {t['status']}"
            + (f" ({eng.clip(t.get('feedback', ''), 140)})"
               if t["status"] == "failed" else "") for t in recent))
    unstarted = [t for t in plan["tasks"] if t["status"] == "todo"]
    if replace_unstarted and unstarted:
        sections.append(
            "CURRENTLY PLANNED, NOT STARTED (keep, change or drop these — "
            "your answer replaces them)\n"
            + "\n".join(f"- {t['title']}: {eng.clip(t['detail'], 120)}"
                        for t in unstarted))
    if state.get("audit_gaps"):
        sections.append(
            "AN AUDIT FOUND THESE GAPS AGAINST THE REQUIREMENTS — the next "
            "tasks must close them:\n"
            + "\n".join(f"- {g}" for g in state["audit_gaps"]))
    if state.get("mode") == "improve":
        sections.append(
            "MODE: the stated goal is met and audited. Plan the most "
            "valuable IMPROVEMENTS now — fix weaknesses, harden, document, "
            "extend toward the obvious next step. Only real improvements: "
            "if nothing worthwhile is left, answer project_complete.")
    steering = _steering_text(slug)
    if steering:
        sections.append(steering.strip())
    sections.append("WHAT YOU CAN ASSIGN\n" + capabilities_text(
        cap, max(1800, budget - sum(len(s) for s in sections) - 600)))

    emit({"event": "thinking", "label": f"planning {slug}"})
    data = eng.ask_json(
        eng.resolve_tier("smart", cfg), "\n\n".join(sections), system,
        on_token=lambda t: emit({"event": "token", "text": t}),
        timeout=_idle_timeout(cfg, "smart"))
    state["stats"]["calls"] += 1
    if isinstance(data, list):
        data = {"tasks": data}
    if not isinstance(data, dict):
        raise ValueError("the planner did not return a plan object")
    raw_tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]

    roster = {a["name"] for a in cap["agents"]} | \
        {a["name"] for a in eng.list_library_agents()}
    skill_names = set(eng.list_skill_names())
    installed = eng.installed_models(cfg)
    existing_files = {r["path"] for r in rows}
    done_titles = {_norm_title(t["title"]) for t in plan["tasks"]
                   if t["status"] == "done"}
    iteration = state["iteration"] + 1
    fresh, idmap = [], {}
    for raw in raw_tasks[:10]:
        title = " ".join(str(raw.get("title") or "").split())[:140]
        if not title or _norm_title(title) in done_titles \
                or _norm_title(title) in {_norm_title(t["title"])
                                          for t in fresh}:
            continue                       # empty, or a repeat of done work
        detail = str(raw.get("detail") or "").strip()[:1800]
        agent = str(raw.get("agent") or "").lower().strip()
        if agent not in roster:
            agent = _guess_agent(title + " " + detail,
                                 {a["name"] for a in cap["agents"]
                                  if not a["library"]})
        skills = [s for s in (raw.get("skills") or [])
                  if isinstance(s, str) and s in skill_names][:3]
        auto_skills = False
        if not skills and skill_names:
            skills = [s["name"] for s in eng.relevant_skills(
                title + " " + detail, k=2, min_score=9.0, rel=0.6)]
            auto_skills = bool(skills)
        try:
            est = min(80.0, max(0.25, float(raw.get("est_hours") or 1)))
        except (TypeError, ValueError):
            est = 1.0
        tid = f"t{state['next_id']}"
        state["next_id"] += 1
        idmap[str(raw.get("id", tid))] = tid
        fresh.append({
            "id": tid, "title": title, "detail": detail,
            "why": str(raw.get("why") or "").strip()[:500],
            "agent": agent, "skills": skills, "auto_skills": auto_skills,
            "model": _valid_model(str(raw.get("model") or ""), cfg, installed),
            "phase": " ".join(str(raw.get("phase") or "work").split())[:40],
            "est_hours": est,
            "depends_on": [str(d) for d in (raw.get("depends_on") or [])
                           if isinstance(d, (str, int))],
            "reads": [str(p).lstrip("./") for p in (raw.get("reads") or [])
                      if isinstance(p, str)
                      and str(p).lstrip("./") in existing_files][:6],
            "research": bool(raw.get("research")) and eng.web_ready(cfg),
            "done_when": str(raw.get("done_when") or "the work product "
                             "exists and is complete").strip()[:400],
            "status": "todo", "attempts": 0, "iteration": iteration,
        })
    for t in fresh:
        t["depends_on"] = [idmap[d] for d in t["depends_on"]
                           if d in idmap and idmap[d] != t["id"]]
    _break_cycles(fresh)

    if fresh or replace_unstarted:
        for t in unstarted if replace_unstarted else []:
            t["status"] = "skipped"
            t["feedback"] = "replanned after new guidance from the owner"
            state["stats"]["skipped"] += 1
    if fresh:
        state["iteration"] = iteration
        state.pop("audit_gaps", None)
        plan["tasks"].extend(fresh)
        closed = [t for t in plan["tasks"]
                  if t["status"] in ("done", "failed", "skipped")]
        if len(closed) > KEEP_FINISHED:        # keep the board readable
            drop = {t["id"] for t in closed[:len(closed) - KEEP_FINISHED]}
            with (auto / "plan-archive.jsonl").open("a",
                                                    encoding="utf-8") as fh:
                for t in plan["tasks"]:
                    if t["id"] in drop:
                        fh.write(json.dumps(t) + "\n")
            plan["tasks"] = [t for t in plan["tasks"] if t["id"] not in drop]
            plan["archived"] = plan.get("archived", 0) + len(drop)
    plan["iteration"] = state["iteration"]
    plan["updated"] = _stamp()
    _save_plan(slug, plan)
    complete = bool(data.get("project_complete")) and not fresh
    emit({"event": "stage_done", "stage": "plan",
          "log": f"{slug}: " + (
              f"iteration {state['iteration']} planned — {len(fresh)} "
              "task(s): " + "; ".join(
                  f"{t['title']} → {t['agent']}"
                  + (f" on {t['model']}" if t["model"] else "")
                  for t in fresh[:8])
              if fresh else "the planner has nothing more to add")})
    return len(fresh), complete


def plan(slug, emit):
    """(Re)plan on request — the Plan button and `aria plan`. Takes stock
    first if that has not happened yet. Returns the plan."""
    eng._project_dir(slug)                       # validates the slug
    with _locked(slug):
        cfg = eng.config()
        state = _state(slug)
        wrapped = _emitter(slug, emit)
        if not (_auto(slug) / "ASSESSMENT.md").is_file():
            assess(slug, wrapped)
            state["stats"]["calls"] += 1
        _plan_next(slug, state, wrapped, cfg, replace_unstarted=True)
        state["closed_iteration"] = state["iteration"] - 1
        _save_state(slug, state)
        return _plan(slug)


# ------------------------------------------------------------ execute

def _runnable(plan):
    done = {t["id"] for t in plan["tasks"] if t["status"] == "done"}
    known = {t["id"] for t in plan["tasks"]}
    ready = [t for t in plan["tasks"] if t["status"] == "todo"
             and all(d in done or d not in known for d in t["depends_on"])]
    # first attempts before retries, so one stubborn task cannot starve
    # the rest of the batch
    return sorted(ready, key=lambda t: t["attempts"] > 0)


def _task_prompt(slug, task, plan, cfg):
    auto = _auto(slug)
    rows = inventory(_proj(slug))
    budget = int(eng.prompt_budget(cfg) * 0.55)      # the rest: system prompt
    parts = []
    if task.get("feedback") and task["attempts"] > 1:
        parts.append(
            "A REVIEWER REJECTED YOUR PREVIOUS ATTEMPT (it was rolled "
            f"back): {task['feedback']}\nFix exactly that and deliver the "
            "complete corrected work.")
    deps = [t for t in plan["tasks"] if t["id"] in task["depends_on"]]
    dep_files = []
    for d in deps[:4]:
        dep_files += d.get("files", [])
    reads = list(dict.fromkeys(
        task.get("reads", []) + dep_files + _mentioned_files(
            task["title"] + " " + task["detail"] + " " + task["done_when"],
            rows)))
    files_text = _read_for_task(slug, reads, int(budget * 0.45))
    if files_text:
        parts.append("PROJECT FILES YOU NEED (current contents)\n"
                     + files_text)
    for d in deps[:3]:
        f = auto / f"task-{d['id']}.md"
        if f.is_file() and not d.get("files"):
            # a dependency that produced prose rather than files
            parts.append(f"OUTPUT OF EARLIER TASK “{d['title']}”\n"
                         + eng.clip(f.read_text(encoding="utf-8"),
                                    int(budget * 0.2)))
    memory = auto / "MEMORY.md"
    if memory.is_file():
        parts.append("PROJECT MEMORY\n" + eng.clip(
            memory.read_text(encoding="utf-8"), 1400))
    parts.append("ALL PROJECT FILES\n" + _inventory_text(rows, 900))
    steering = _steering_text(slug, limit=4)
    if steering:
        parts.append(steering.strip())
    context = eng.clip("\n\n".join(parts), budget)
    return TASK_TEMPLATE.format(
        slug=slug, goal=eng._project_goal(slug, 1400), title=task["title"],
        detail=task["detail"] or "(see title)", done_when=task["done_when"],
        context="\n" + context + "\n" if context else "")


def _notes(output):
    """The agent's own account of what it did and why (its ## Notes)."""
    m = re.search(r"^#+\s*Notes\s*$(.*)", output, re.S | re.M | re.I)
    text = m.group(1) if m else ""
    if not text.strip():                 # no Notes section: prose outside
        text = re.sub(r"(`{3,}|~{3,}).*?\1", " ", output, flags=re.S)
    return " ".join(text.split())[:700]


def _verify(slug, task, output, records, problems, cfg, emit):
    """Step 4. Mechanical problems fail outright; otherwise a judge reads
    the work against done_when."""
    if problems:
        return {"pass": False, "reviewed": True,
                "feedback": "; ".join(problems)[:500]}
    changed = [r for r in records if r["action"] != "unchanged"]
    if not changed and len(output.strip()) < 60:
        return {"pass": False, "reviewed": True,
                "feedback": "the agent produced no files and almost no "
                            "output"}
    proj = _proj(slug)
    shown = []
    budget = int(eng.prompt_budget(cfg) * 0.6)
    each = max(500, budget // max(1, len(changed)))
    for r in changed[:8]:
        try:
            text = (proj / r["path"]).read_text(encoding="utf-8",
                                                errors="replace")
        except OSError:
            text = ""
        shown.append(f"--- {r['path']} ({r['action']}, {r['lines']} lines) "
                     f"---\n{eng.clip(text, each)}")
    body = (f"TASK: {task['title']}\nDETAIL: {eng.clip(task['detail'], 600)}"
            f"\nDONE WHEN: {task['done_when']}\n\n"
            + ("FILES THE AGENT WROTE\n" + "\n\n".join(shown) if shown
               else "THE AGENT WROTE NO FILES. ITS OUTPUT:\n"
                    + eng.clip(output, budget))
            + f"\n\nTHE AGENT'S NOTES\n{_notes(output) or '(none)'}")
    emit({"event": "thinking", "label": f"verifying {task['id']}"})
    try:
        data = eng.ask_json(eng.resolve_tier("smart", cfg),
                            eng.clip(body, budget + 1500), VERIFY_SYSTEM,
                            timeout=_idle_timeout(cfg, "smart"))
        if not isinstance(data, dict) or "pass" not in data:
            raise ValueError("no verdict in the judge's reply")
        verdict = data["pass"]
        if isinstance(verdict, str):
            verdict = verdict.strip().lower() in ("true", "yes", "pass")
        return {"pass": bool(verdict), "reviewed": True,
                "feedback": " ".join(str(data.get("feedback", "")).split())[:500]}
    except Exception as exc:
        if eng.backend_trouble(exc):
            raise
        return {"pass": True, "reviewed": False,
                "feedback": f"verifier unavailable ({exc}) — accepted "
                            "unreviewed"}


def _record(slug, state, task, records, info, verdict, output, commit=""):
    """Step 5: the structured change record + the readable changelog."""
    auto = _auto(slug)
    files = [{k: r[k] for k in ("path", "action", "lines", "added", "removed")}
             for r in records if r["action"] != "unchanged"]
    row = {
        "t": _stamp(), "run": state["run"], "iteration": task["iteration"],
        "task": task["id"], "title": task["title"], "status": task["status"],
        "attempts": task["attempts"], "agent": task["agent"],
        "model": info.get("model", ""), "skills": info.get("skills", []),
        "phase": task.get("phase", ""), "why": task.get("why", ""),
        "notes": _notes(output), "files": files,
        "verified": verdict.get("reviewed", False),
        "feedback": verdict.get("feedback", ""),
        "sources": info.get("sources", []),
        "output": f"{auto.name}/task-{task['id']}.md", "commit": commit,
        "seconds": task.get("seconds", 0),
    }
    with (auto / "changes.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    line = (f"- **{task['title']}** [{task['id']}] — {task['status']} "
            f"(attempt {task['attempts']}) · {task['agent']} on "
            f"{info.get('model', '?')}"
            + (f" · skills: {', '.join(info['skills'])}"
               if info.get("skills") else ""))
    if files:
        line += "\n  - Files: " + ", ".join(
            f"`{f['path']}` ({f['action']}, +{f['added']}/−{f['removed']})"
            for f in files)
    if task.get("why"):
        line += f"\n  - Why: {task['why']}"
    if row["notes"]:
        line += f"\n  - What was done: {row['notes'][:400]}"
    if verdict.get("feedback"):
        line += f"\n  - Review: {verdict['feedback']}"
    refs = [f"`{row['output']}`"] + ([f"commit `{commit}`"] if commit else [])
    refs += [f"[{s.get('title') or s['url']}]({s['url']})"
             for s in row["sources"][:6]]
    line += "\n  - References: " + " · ".join(refs)
    with (auto / "CHANGELOG.md").open("a", encoding="utf-8") as fh:
        fh.write(f"\n### {_stamp()[:16].replace('T', ' ')} — iteration "
                 f"{task['iteration']}\n{line}\n")
    return row


def _commit(slug, state, cfg, paths, message, emit):
    """A commit on the work branch — only when the project is its own
    repository and git has not been stood down for this run."""
    if not cfg["autopilot_git"] or state.get("git_off"):
        return ""
    proj = _proj(slug)
    if not aria_git.is_repo_root(proj):
        return ""
    auto = _auto(slug)
    if auto.name == "autopilot":           # her records travel with the repo
        paths = list(paths) + [
            f"autopilot/{n}" for n in ("CHANGELOG.md", "changes.jsonl",
                                       "plan.json", "MEMORY.md",
                                       "ASSESSMENT.md", "FINAL-REPORT.md",
                                       "STEERING.md")
        ] + [p.relative_to(proj).as_posix()
             for p in list(auto.glob("task-*.md"))
             + list(auto.glob("reports/*.md"))] + ["STATUS.md", ".gitignore"]
    try:
        if aria_git.current_branch(proj) != aria_git.WORK_BRANCH:
            aria_git.start_work_branch(proj, state.get("git_base", ""))
        sha = aria_git.commit(proj, paths, message)
        return sha or ""
    except aria_git.GitError as exc:
        state["git_off"] = str(exc)
        emit({"event": "error",
              "log": f"{slug}: git commits paused for this run — {exc}"})
        return ""


def _run_task(slug, state, plan, task, cfg, emit):
    auto = _auto(slug)
    task["status"] = "inprogress"
    task["attempts"] += 1
    task["started"] = _stamp()
    _save_plan(slug, plan)
    state.update(phase="working", waiting="", current={
        "id": task["id"], "title": task["title"], "agent": task["agent"],
        "model": task["model"] or "agent default", "started": _stamp(),
        "attempt": task["attempts"]})
    _save_state(slug, state)
    emit({"event": "start", "stage": "autopilot",
          "log": f"{slug}: {task['id']} — {task['title']} → {task['agent']}"
                 + (f" on {task['model']}" if task["model"] else "")
                 + (f" (attempt {task['attempts']})"
                    if task["attempts"] > 1 else "")})
    t0 = time.time()
    info, records, output = {}, [], ""
    agent = eng.get_agent(task["agent"])
    if agent is None:                  # e.g. its library was uninstalled
        task["agent"] = _guess_agent(
            task["title"] + " " + task["detail"],
            {a["name"] for a in eng.list_agents()})
        agent = eng.get_agent(task["agent"])
    model_spec = task["model"] or (agent["model"] if agent else "smart")
    try:
        output = eng.run_agent(
            task["agent"], _task_prompt(slug, task, plan, cfg), emit,
            timeout=_idle_timeout(cfg, model_spec), model=task["model"],
            skills=task["skills"],
            web=True if task.get("research") else None, info=info)
        state["stats"]["calls"] += 1
        records = write_files(slug, parse_file_blocks(output), emit)
        task["status"] = "reviewing"
        _save_plan(slug, plan)
        state["phase"] = "reviewing"
        _save_state(slug, state)
        verdict = _verify(slug, task, output, records,
                          static_checks(slug, records), cfg, emit)
        state["stats"]["calls"] += 1
    except Exception as exc:
        _revert(slug, records)
        if eng.backend_trouble(exc):
            task["status"] = "todo"            # not the task's fault
            task["attempts"] -= 1
            _save_plan(slug, plan)
            raise
        verdict = {"pass": False, "reviewed": False,
                   "feedback": f"the attempt errored: {exc}"}
        if "HTTP 404" in str(exc) or "not found" in str(exc).lower():
            task["model"] = ""                 # the assigned model is gone
    task["seconds"] = task.get("seconds", 0) + int(time.time() - t0)
    with (auto / f"task-{task['id']}.md").open("a", encoding="utf-8") as fh:
        fh.write(f"# {task['title']} — attempt {task['attempts']}\n\n"
                 f"agent: {task['agent']} · model: "
                 f"{info.get('model', model_spec)} · {_stamp()}\n"
                 f"verdict: {'PASS' if verdict['pass'] else 'FAIL'} — "
                 f"{verdict['feedback']}\n\n{output}\n\n---\n\n")
    task["model_used"] = info.get("model", "")
    task["feedback"] = verdict["feedback"]
    state["current"] = None
    if verdict["pass"]:
        task["status"] = "done"
        task["finished"] = _stamp()
        task["files"] = [r["path"] for r in records
                         if r["action"] != "unchanged"]
        state["stats"]["done"] += 1
        state["stats"]["files"] += len(task["files"])
        state["iter_done"] = state.get("iter_done", 0) + 1
        _save_plan(slug, plan)
        sha = _commit(
            slug, state, cfg, task["files"],
            f"{task['id']}: {task['title']}\n\n"
            + (f"Why: {task['why']}\n" if task.get("why") else "")
            + f"Agent: {task['agent']} · model: {info.get('model', '?')}\n"
              f"Verified: {verdict['feedback'] or 'passed'}", emit)
        _record(slug, state, task, records, info, verdict, output, sha)
        eng.brain_journal(f"[[{slug}]] — finished “{task['title']}”"
                          + (f" ({', '.join(task['files'][:3])})"
                             if task["files"] else ""))
        emit({"event": "stage_done", "stage": "autopilot",
              "log": f"{slug}: {task['id']} done and verified"
                     + (f" — {', '.join(task['files'][:4])}"
                        if task["files"] else "")})
    else:
        _revert(slug, records)
        if task["attempts"] >= MAX_ATTEMPTS:
            task["status"] = "failed"
            task["finished"] = _stamp()
            state["stats"]["failed"] += 1
            _record(slug, state, task, [], info, verdict, output)
            emit({"event": "error",
                  "log": f"{slug}: {task['id']} failed after "
                         f"{task['attempts']} attempts — "
                         f"{verdict['feedback']}"})
        else:
            task["status"] = "todo"
            emit({"event": "error",
                  "log": f"{slug}: {task['id']} rejected (attempt "
                         f"{task['attempts']} of {MAX_ATTEMPTS}), rolled "
                         f"back — {verdict['feedback']}"})
        _save_plan(slug, plan)
    state["errors"] = 0
    _save_state(slug, state)
    _update_status_file(slug, state)
    return "task"


# ------------------------------------------------------------ reflect

def _reflect(slug, state, batch, cfg, emit):
    """Step 6: rewrite the memory from what this batch taught."""
    auto = _auto(slug)
    path = auto / "MEMORY.md"
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    results = []
    for t in batch:
        results.append(
            f"- {t['title']} — {t['status']}"
            + (f"; files: {', '.join(t.get('files', [])[:5])}"
               if t.get("files") else "")
            + (f"; review: {eng.clip(t.get('feedback', ''), 160)}"
               if t.get("feedback") else ""))
    prompt = (f"PROJECT: {slug}\n\nOLD MEMORY\n{eng.clip(old, 2400) or '(none yet)'}"
              f"\n\nTHIS BATCH (iteration {state['iteration']})\n"
              + "\n".join(results)
              + "\n\nFILES NOW\n" + _inventory_text(inventory(_proj(slug)),
                                                    1000))
    try:
        emit({"event": "thinking", "label": f"updating {slug}'s memory"})
        text = eng.strip_think(eng.chat_stream(
            eng.resolve_tier("fast", cfg), prompt, MEMORY_SYSTEM,
            timeout=_idle_timeout(cfg, "fast")))
        state["stats"]["calls"] += 1
        if len(text.strip()) < 60:
            raise ValueError("empty memory")
        body = text.strip()
    except Exception as exc:
        if eng.backend_trouble(exc):
            raise
        # never lose the batch: fall back to a plain record
        body = (eng.clip(re.sub(r"^# .*\n+(\*.*\*\n+)?", "", old), 3000)
                + f"\n\n### Iteration {state['iteration']}\n"
                + "\n".join(results)).strip()
    path.write_text(f"# Memory — {slug}\n\n*Rewritten after iteration "
                    f"{state['iteration']} ({_stamp()[:16].replace('T', ' ')})"
                    f".*\n\n{body}\n", encoding="utf-8")
    emit({"event": "stage_done", "stage": "reflect",
          "log": f"{slug}: memory updated after iteration "
                 f"{state['iteration']}"})


def _audit(slug, state, cfg, emit):
    """When the planner says "done": check that against the requirements."""
    auto = _auto(slug)
    rows = inventory(_proj(slug))
    key = [r["path"] for r in rows if r["lines"]][:8]
    prompt = (f"REQUIREMENTS\n{eng._project_goal(slug, 3500)}\n\n"
              f"FILES THAT EXIST\n{_inventory_text(rows, 1500)}\n\n"
              + ("MEMORY OF THE RUN\n" + eng.clip(
                  (auto / "MEMORY.md").read_text(encoding="utf-8"), 2000)
                 + "\n\n" if (auto / "MEMORY.md").is_file() else "")
              + "EXCERPTS\n" + _read_for_task(
                  slug, key, int(eng.prompt_budget(cfg) * 0.45)))
    emit({"event": "thinking",
          "label": f"auditing {slug} against its requirements"})
    data = eng.ask_json(eng.resolve_tier("smart", cfg), prompt, AUDIT_SYSTEM,
                        timeout=_idle_timeout(cfg, "smart"))
    state["stats"]["calls"] += 1
    if not isinstance(data, dict):
        return []
    gaps = [" ".join(str(g).split())[:240] for g in (data.get("gaps") or [])
            if str(g).strip()][:6]
    met = data.get("met")
    if isinstance(met, str):
        met = met.strip().lower() in ("true", "yes")
    return [] if met and not gaps else (gaps or ["the audit judged the "
                                                 "requirements unmet"])


def _close_iteration(slug, state, plan, cfg, emit):
    """A batch has nothing left to run: settle it and learn from it."""
    it = state["iteration"]
    for t in plan["tasks"]:
        if t["status"] == "todo":            # its dependencies never landed
            t["status"] = "skipped"
            t["feedback"] = "a task it depends on did not complete"
            state["stats"]["skipped"] += 1
    _save_plan(slug, plan)
    # everything finished since the memory was last rewritten — including
    # work from a batch that new guidance cut short
    batch = [t for t in plan["tasks"] if not t.get("reflected")
             and t["status"] in ("done", "failed", "skipped")]
    if batch:
        state["phase"] = "reflecting"
        _save_state(slug, state)
        _reflect(slug, state, batch, cfg, emit)
        for t in batch:
            t["reflected"] = True
        _save_plan(slug, plan)
        state["dry"] = 0 if state.get("iter_done") else state["dry"] + 1
    state["iter_done"] = 0
    state["closed_iteration"] = it
    _save_state(slug, state)
    _commit(slug, state, cfg, [], f"iteration {it}: memory and records", emit)
    _sync_brain(slug)
    if state["dry"] >= 2:
        _finalize(slug, emit, "stalled",
                  "two batches in a row completed nothing")
        return "finalized"
    return "reflected"


def _advance(slug, state, cfg, emit):
    """Plan the next batch — or audit, switch to improvements, or finish."""
    state["phase"] = "planning"
    _save_state(slug, state)
    added, complete = _plan_next(slug, state, emit, cfg)
    if added:
        state["empty_plans"] = 0
        state["phase"] = "working"
        _save_state(slug, state)
        return "replanned"
    if not state["stats"]["done"]:
        # nothing has ever been completed and the planner offers nothing:
        # that is a planner problem, never "the goal is met"
        state["empty_plans"] = state.get("empty_plans", 0) + 1
        _save_state(slug, state)
        if state["empty_plans"] >= 2:
            _finalize(slug, emit, "stalled",
                      "the planner returned no tasks twice — check the "
                      "smart-tier model")
            return "finalized"
        return "replanned"
    if state["mode"] == "improve":
        _save_state(slug, state)
        _finalize(slug, emit, "complete",
                  "the goal is met and no further improvement was worth "
                  "making")
        return "complete"
    if state["audits"] < 2:
        state["phase"] = "auditing"
        _save_state(slug, state)
        gaps = _audit(slug, state, cfg, emit)
        state["audits"] += 1
        if gaps:
            state["audit_gaps"] = gaps
            _save_state(slug, state)
            emit({"event": "stage_done", "stage": "audit",
                  "log": f"{slug}: audit found {len(gaps)} gap(s) against "
                         "the requirements — planning to close them: "
                         + "; ".join(gaps[:3])})
            return "audited"
        emit({"event": "stage_done", "stage": "audit",
              "log": f"{slug}: audit passed — the requirements are met"})
    state["audits"] = 0
    if cfg["autopilot_after_goal"] == "improve":
        state["mode"] = "improve"
        _save_state(slug, state)
        emit({"event": "stage_done", "stage": "autopilot",
              "log": f"{slug}: goal met — moving on to improvements until "
                     "you stop me"})
        return "improving"
    _save_state(slug, state)
    _finalize(slug, emit, "complete", "the goal is met and audited")
    return "complete"


# ------------------------------------------------------------ the loop

def tick(slug, emit):
    """Run ONE step for a project. Returns what happened: 'task',
    'assessed', 'replanned', 'reflected', 'audited', 'improving',
    'recovered', 'finalized', 'complete', 'waiting', 'busy' or 'idle'."""
    if _state(slug)["status"] != "running":
        return "idle"
    try:
        with _locked(slug):
            return _tick(slug, emit)
    except Busy:
        return "busy"


def _tick(slug, emit):
    state = _state(slug)
    if state["status"] != "running":
        return "idle"
    auto = _auto(slug)
    emit = _emitter(slug, emit)
    if (auto / "STOP").exists():
        _finalize(slug, emit, "stopped")
        return "finalized"
    if state["backoff_until"] > time.time():
        return "waiting"
    cfg = eng.config()
    try:
        result = _step(slug, state, cfg, emit)
    except Exception as exc:
        state = _state(slug)                 # what the step managed to save
        state["current"] = None
        state["errors"] += 1
        delay = min(900, 30 * 2 ** min(state["errors"] - 1, 5))
        down = eng.backend_trouble(exc)
        if not down and state["errors"] >= 5:
            _save_state(slug, state)
            _finalize(slug, emit, "stalled",
                      f"five steps in a row errored — last: {exc}")
            return "finalized"
        state["backoff_until"] = time.time() + delay
        state["waiting"] = (f"the model server is not answering — retrying "
                            f"in {delay}s" if down else
                            f"a step failed ({exc}) — retrying in {delay}s")
        _save_state(slug, state)
        emit({"event": "error", "log": f"{slug}: {state['waiting']}"
                                       + (f" [{exc}]" if down else "")})
        return "waiting"
    return result


def _step(slug, state, cfg, emit):
    auto = _auto(slug)
    if state.get("waiting") or state["backoff_until"]:
        state["waiting"], state["backoff_until"] = "", 0
        _save_state(slug, state)
    hours = (time.time() - state.get("run_started", time.time())) / 3600
    if (cfg["autopilot_max_hours"] and hours >= cfg["autopilot_max_hours"]) \
            or (cfg["autopilot_max_iterations"]
                and state["iteration"] - state.get("run_first_iteration", 0)
                >= cfg["autopilot_max_iterations"]
                and state.get("closed_iteration") == state["iteration"]):
        _finalize(slug, emit, "stopped", "the run's budget was reached "
                  "(aria.autopilot_max_hours / autopilot_max_iterations)")
        return "finalized"
    if not (auto / "ASSESSMENT.md").is_file():
        state["phase"] = "assessing"
        _save_state(slug, state)
        assess(slug, emit)
        state["stats"]["calls"] += 1
        state["errors"] = 0
        _save_state(slug, state)
        return "assessed"
    plan = _plan(slug)
    if state.pop("steer_pending", None) and any(
            t["status"] == "todo" for t in plan["tasks"]):
        state["phase"] = "planning"
        _save_state(slug, state)
        _plan_next(slug, state, emit, cfg, replace_unstarted=True)
        state["errors"] = 0
        state["phase"] = "working"
        _save_state(slug, state)
        return "replanned"
    ready = _runnable(plan)
    if ready:
        return _run_task(slug, state, plan, ready[0], cfg, emit)
    stranded = [t for t in plan["tasks"]
                if t["status"] in ("inprogress", "reviewing")]
    if stranded:                       # a crash or restart caught them mid-air
        for t in stranded:
            t["status"] = "todo"
            t["attempts"] = max(0, t["attempts"] - 1)
        _save_plan(slug, plan)
        emit({"event": "error",
              "log": f"{slug}: restarting {len(stranded)} task(s) that a "
                     "restart interrupted"})
        return "recovered"
    if plan["tasks"] and state.get("closed_iteration") != state["iteration"]:
        result = _close_iteration(slug, state, plan, cfg, emit)
        state["errors"] = 0
        return result
    result = _advance(slug, state, cfg, emit)
    if result != "complete":
        state = _state(slug)
        state["errors"] = 0
        _save_state(slug, state)
    return result


# ------------------------------------------------------------ control

def start(slug, via="cli"):
    """Engage autopilot. The human gate comes first: an unapproved plan is
    refused (NeedsApproval), and an approved project still sitting in the
    pipeline is promoted to its own workspace before work begins."""
    proj = eng._project_dir(slug)
    if not eng.project_approved(slug):
        if eng._project_doc(proj, "proposed-plan"):
            raise eng.NeedsApproval(
                f"{slug}'s plan hasn't been approved yet. Read it, then say "
                f"'approve {slug}' (or press Approve on the Projects page) "
                "and I'll start.")
        raise eng.NeedsApproval(
            f"{slug} has no plan yet — run develop first, then approve it.")
    if eng.project_kind(proj) == "potential" \
            and not (proj / "autopilot" / "state.json").is_file():
        proj = eng.promote_project(slug)
    cfg = eng.config()
    state = _state(slug)
    auto = _auto(slug)
    (auto / "STOP").unlink(missing_ok=True)
    if state["status"] == "running":
        return state
    state.update(status="running", run=state["run"] + 1, started=_stamp(),
                 run_started=time.time(), phase="starting", waiting="",
                 backoff_until=0, errors=0, dry=0, audits=0, stop_reason="",
                 run_first_iteration=state["iteration"], current=None)
    state.pop("git_off", None)
    if cfg["autopilot_git"] and aria_git.is_repo_root(proj):
        try:
            branch, base = aria_git.start_work_branch(
                proj, state.get("git_base", ""))
            state["git_branch"] = branch
            if base:
                state["git_base"] = base
        except aria_git.GitError as exc:
            state["git_off"] = str(exc)
    _save_state(slug, state)
    _activity(slug, "info", f"autopilot engaged (run {state['run']}, "
                            f"via {via})"
              + (f" — git commits off: {state['git_off']}"
                 if state.get("git_off") else ""))
    eng.brain_journal(f"started working on [[{slug}]] (run {state['run']})")
    _update_status_file(slug, state)
    return state


def stop(slug):
    """Graceful stop: the step in flight finishes, then the final report
    is written and the project is left as it is."""
    state = _state(slug)
    if state["status"] == "running":
        (_auto(slug) / "STOP").touch()
        _activity(slug, "info", "stop requested — finishing the current "
                                "step, then writing the report")
    return state


def steer(slug, text, via="chat"):
    """Guidance from the owner, mid-run. Binding on every plan from here
    on; work that has not started yet is replanned around it."""
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("empty guidance")
    eng._project_dir(slug)
    with (_auto(slug) / "STEERING.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- [{_stamp()[:16].replace('T', ' ')} via {via}] {text}\n")
    state = _state(slug)
    state["steer_pending"] = True
    _save_state(slug, state)
    _activity(slug, "info", f"guidance from you: {text}")
    return state


def run_forever(slug, emit, idle_sleep=10):
    """Drive one project in the foreground until it stops — for running
    autopilot from a terminal without the always-on server."""
    while True:
        result = tick(slug, emit)
        if result in ("idle", "finalized", "complete"):
            return result
        if result in ("waiting", "busy"):
            time.sleep(idle_sleep)


# ------------------------------------------------------------ finish

def _update_status_file(slug, state):
    """Keep an active project's STATUS.md honest about what autopilot is
    doing (phase, date, progress, current focus)."""
    path = _proj(slug) / "STATUS.md"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    plan = _plan(slug)
    open_tasks = [t for t in plan["tasks"]
                  if t["status"] in ("todo", "inprogress", "reviewing")]
    done = state["stats"]["done"]
    total = done + len(open_tasks)
    pct = int(100 * done / total) if total else 0
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    phase = {"running": f"Autopilot — iteration {state['iteration']} "
                        f"({state.get('phase') or 'working'})",
             "stopped": "Autopilot stopped — see autopilot/FINAL-REPORT.md",
             "complete": "Complete — see autopilot/FINAL-REPORT.md",
             "stalled": "Paused (stalled) — see autopilot/FINAL-REPORT.md",
             }.get(state["status"], "Not started")
    cur = state.get("current") or {}
    focus = cur.get("title") or (open_tasks[0]["title"] if open_tasks
                                 else "Nothing queued.")
    text = re.sub(r"^\*\*Phase:\*\*.*$", f"**Phase:** {phase}", text, 1, re.M)
    text = re.sub(r"^\*\*Last updated:\*\*.*$",
                  f"**Last updated:** {_stamp()[:10]}", text, 1, re.M)
    text = re.sub(r"^\*\*Overall:\*\*.*$",
                  f"**Overall:** {bar} {pct}% ({done} task(s) verified)",
                  text, 1, re.M)
    text = re.sub(r"(^## Current focus\n\n).*?(?=\n## )",
                  lambda m: m.group(1) + focus + "\n", text, 1, re.S | re.M)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _sync_brain(slug):
    try:
        if eng.brain_ready():
            eng.brain_sync_project(slug)
    except Exception:
        pass                              # the vault is never load-bearing


def build_report(slug, state, reason, detail="", summary=""):
    """The final report: a complete, deterministic list of every change in
    this run — files, reasoning, references — built from the structured
    record, never from a model's memory of it."""
    auto = _auto(slug)
    rows = eng.project_changes(slug)
    run = state["run"]
    mine = [r for r in rows if r.get("run") == run]
    done = [r for r in mine if r["status"] == "done"]
    failed = [r for r in mine if r["status"] == "failed"]
    created = sum(1 for r in done for f in r["files"]
                  if f["action"] == "created")
    modified = sum(1 for r in done for f in r["files"]
                   if f["action"] == "modified")
    ending = {"stopped": "stopped by you", "complete": "completed",
              "stalled": "paused itself (stalled)"}.get(reason, reason)
    out = [f"# Final report — {slug}", "",
           f"Run {run} {ending} at {_stamp()[:16].replace('T', ' ')}"
           + (f" — {detail}" if detail else "") + ".",
           f"Started {str(state.get('started', '?'))[:16].replace('T', ' ')}"
           f" · iterations {state.get('run_first_iteration', 0) + 1}–"
           f"{state['iteration']} · {len(done)} task(s) completed and "
           f"verified, {len(failed)} failed · {created} file(s) created, "
           f"{modified} modified.", ""]
    out += ["## Summary", "", summary.strip() or (
        f"{len(done)} task(s) were completed and verified in this run."), ""]

    look = []
    for name in ("README.md", "docs/README.md"):
        if (_proj(slug) / name).is_file():
            look.append(f"`{name}` — the project's own front page")
            break
    touched = {}
    for r in done:
        for f in r["files"]:
            touched.setdefault(f["path"], []).append(r["task"])
    for path, _tasks in sorted(touched.items(),
                               key=lambda kv: -len(kv[1]))[:5]:
        if not any(f"`{path}`" in x for x in look) and len(look) < 5:
            look.append(f"`{path}` — changed by {len(_tasks)} task(s)")
    if failed:
        look.append(f"the {len(failed)} failed task(s) below — work the "
                    "plan could not land")
    if state.get("git_branch") and not state.get("git_off"):
        look.append(f"git branch `{state['git_branch']}` — one commit per "
                    "verified task")
    if look:
        out += ["## What to look at first", ""] + [f"- {x}" for x in look] \
            + [""]

    out += ["## Every change made in this run", ""]
    if not done:
        out += ["No changes were completed in this run.", ""]
    for it in sorted({r["iteration"] for r in done}):
        out += [f"### Iteration {it}", ""]
        for n, r in enumerate([x for x in done if x["iteration"] == it], 1):
            out.append(
                f"{n}. **{r['title']}** (`{r['task']}`) — {r['agent']} on "
                f"{r.get('model') or '?'}"
                + (f", skills: {', '.join(r['skills'])}"
                   if r.get("skills") else "")
                + (f", attempt {r['attempts']}" if r["attempts"] > 1 else ""))
            if r["files"]:
                out.append("   - **Changed:** " + ", ".join(
                    f"`{f['path']}` ({f['action']}, +{f['added']}/"
                    f"−{f['removed']} lines)" for f in r["files"]))
            else:
                out.append("   - **Changed:** no files — the result is the "
                           "task's written output")
            if r.get("why"):
                out.append(f"   - **Why:** {r['why']}")
            if r.get("notes"):
                out.append(f"   - **What was done:** {r['notes']}")
            out.append("   - **Verified:** "
                       + (r.get("feedback") or "passed")
                       + ("" if r.get("verified") else " (unreviewed)"))
            refs = [f"`{r['output']}`"]
            if r.get("commit"):
                refs.append(f"commit `{r['commit']}`")
            refs += [f"[{s.get('title') or s['url']}]({s['url']})"
                     for s in r.get("sources", [])]
            out.append("   - **References:** " + " · ".join(refs))
        out.append("")

    if failed:
        out += ["## Not completed", ""]
        out += [f"- **{r['title']}** (`{r['task']}`) — failed after "
                f"{r['attempts']} attempt(s): {r.get('feedback') or '?'} "
                f"(attempts kept in `{r['output']}`; nothing from them was "
                "left in the project)" for r in failed]
        out.append("")

    if touched:
        out += ["## Files touched", "", "| File | Tasks |", "|---|---|"]
        out += [f"| `{p}` | {', '.join(t)} |"
                for p, t in sorted(touched.items())]
        out.append("")

    earlier = sorted({r.get("run") for r in rows if r.get("run") != run
                      and r.get("run")})
    if earlier:
        out += ["## Earlier runs", ""]
        for e in earlier:
            n = len([r for r in rows if r.get("run") == e
                     and r["status"] == "done"])
            out.append(f"- Run {e}: {n} change(s) — see "
                       f"`{auto.name}/reports/`")
        out.append("")
    out += ["---", f"Complete record: `{auto.name}/changes.jsonl` · "
            f"readable log: `{auto.name}/CHANGELOG.md` · every task's full "
            f"output: `{auto.name}/task-<id>.md`."]
    return "\n".join(out) + "\n"


def _finalize(slug, emit, reason, detail=""):
    """Stand down: write the report, sync, commit, and leave everything
    exactly as it is."""
    auto = _auto(slug)
    cfg = eng.config()
    state = _state(slug)
    (auto / "STOP").unlink(missing_ok=True)
    state.update(phase="reporting", current=None, waiting="",
                 backoff_until=0)
    _save_state(slug, state)
    mine = [r for r in eng.project_changes(slug)
            if r.get("run") == state["run"]]
    summary = ""
    try:
        memory = (auto / "MEMORY.md").read_text(encoding="utf-8") \
            if (auto / "MEMORY.md").is_file() else ""
        facts = (f"Project: {slug}. Run ended because: {reason}"
                 + (f" ({detail})" if detail else "") + ".\n\nTasks:\n"
                 + "\n".join(f"- {r['title']} — {r['status']}"
                             + (f" ({', '.join(f['path'] for f in r['files'][:3])})"
                                if r.get("files") else "")
                             for r in mine[-40:])
                 + f"\n\nProject memory:\n{eng.clip(memory, 2400)}")
        if mine:
            emit({"event": "thinking",
                  "label": f"writing {slug}'s final report"})
            summary = eng.strip_think(eng.chat_stream(
                eng.resolve_tier("fast", cfg), facts, SUMMARY_SYSTEM,
                timeout=min(600, _idle_timeout(cfg, "fast"))))
    except Exception:
        summary = ""                  # the list below never depends on this
    report = build_report(slug, state, reason, detail, summary)
    (auto / "reports").mkdir(exist_ok=True)
    stamp = f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
    (auto / "reports" / f"run-{state['run']:03d}-{stamp}.md").write_text(
        report, encoding="utf-8")
    (auto / "FINAL-REPORT.md").write_text(report, encoding="utf-8")
    state["status"] = reason if reason in ("stopped", "complete",
                                           "stalled") else "stopped"
    state["stop_reason"] = detail or reason
    state["phase"] = ""
    state["finished"] = _stamp()
    _save_state(slug, state)
    _update_status_file(slug, state)
    _commit(slug, state, cfg, [], f"run {state['run']}: final report "
            f"({reason})", emit)
    pushed = ""
    if cfg["github_access"] and aria_git.is_repo_root(_proj(slug)) \
            and aria_git.remote_url(_proj(slug)) and not state.get("git_off"):
        try:
            pushed = eng.project_push(slug, eng.clip(report, 6000)).get(
                "pr", "")
        except Exception as exc:
            emit({"event": "error",
                  "log": f"{slug}: could not push to GitHub — {exc}"})
    _sync_brain(slug)
    eng.brain_journal(f"[[{slug}]] — run {state['run']} {reason}: "
                      f"{len([r for r in mine if r['status'] == 'done'])} "
                      "change(s), report written")
    with _LIVE_LOCK:
        LIVE.pop(slug, None)
    emit({"event": "stage_done", "stage": "autopilot",
          "log": f"{slug}: run {state['run']} {reason} — final report "
                 f"written ({auto.name}/FINAL-REPORT.md)"
                 + (f"; pull request: {pushed}" if pushed else "")})
    try:
        if "mission-done" in cfg["webhooks"]:
            eng.trigger_webhook("mission-done",
                                f"autopilot {slug} {reason}"
                                + (f": {detail}" if detail else ""))
    except Exception:
        pass


# ------------------------------------------------------------ for the UI

def view(slug):
    """Everything the Projects page shows for one project."""
    proj = eng._project_dir(slug)
    auto = eng._auto_dir(slug, create=False)
    state = _state(slug) if auto.is_dir() else {
        "status": "idle", "iteration": 0, "stats": {}}

    def text(name, tail=0):
        f = auto / name
        if not f.is_file():
            return ""
        body = f.read_text(encoding="utf-8", errors="replace")
        return body[-tail:] if tail else body

    plan = _plan(slug) if (auto / "plan.json").is_file() else {"tasks": []}
    try:
        rel = str(proj.relative_to(eng.ROOT))
    except ValueError:
        rel = ""
    try:
        git = eng.project_git(slug)
    except Exception:
        git = {"repo": False}
    return {
        "slug": slug, "dir": rel, "path": str(proj),
        "kind": eng.project_kind(proj),
        "approved": eng.project_approved(slug),
        "has_plan": eng._project_doc(proj, "proposed-plan") is not None,
        "pitch": eng.plan_pitch(proj),
        "state": {k: v for k, v in state.items() if k != "run_started"},
        "plan": plan,
        "assessment": text("ASSESSMENT.md"),
        "memory": text("MEMORY.md"),
        "steering": text("STEERING.md"),
        "changelog": text("CHANGELOG.md", tail=16000),
        "report": text("FINAL-REPORT.md"),
        "changes": eng.project_changes(slug, limit=300),
        "activity": activity(slug) if auto.is_dir() else [],
        "live": live(slug),
        "git": git,
        "auto_dir": auto.name,
    }
