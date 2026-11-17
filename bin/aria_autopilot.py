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

Guardrails for runs that last days
  - A task the planner marks needs_human waits for the owner's answer;
    everything that does not depend on it carries on, and she only stops
    to wait when nothing else is left.
  - Budgets (hours, batches, tasks) and the stall limit end a run with its
    report instead of letting it burn a week unnoticed.
  - Work that is already done, was declined, or failed twice is dropped
    when the planner proposes it again.

Everything lives in <project>/autopilot/. The loop is driven by tick(): one
call = one step, so a crash or restart loses at most the step in flight.
Signals from outside the loop are files (STOP, STEER, decision-<id>.json):
the plan and the state are only ever written by whoever holds the lock.
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
import urllib.parse
from pathlib import Path

import aria_engine as eng
import aria_garage
import aria_git

MAX_ATTEMPTS = 3            # generations a task gets before it is failed
MAX_FILE_BYTES = 400_000    # per file an agent may write
MAX_FILES_PER_TASK = 25
# finished tasks kept on the board; older ones move to plan-archive.jsonl
# (the Gantt and the Done column still show them)
KEEP_FINISHED = int(os.environ.get("ARIA_BOARD_KEEP") or 40)
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
                         ("waiting", ""), ("backoff_until", 0),
                         ("run_tasks", 0), ("empty_plans", 0)):
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
        t.setdefault("title", "untitled")
        t.setdefault("detail", "")
        t.setdefault("agent", "planner")
        t.setdefault("model", "")
        t.setdefault("done_when", "the work product exists and is complete")
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
        if path.stat().st_size > 600_000:      # keep the live feed quick…
            lines = path.read_text(encoding="utf-8").splitlines()
            half = len(lines) // 2
            with (path.parent / "activity-archive.jsonl").open(
                    "a", encoding="utf-8") as old:   # …and lose nothing
                old.write("\n".join(lines[:half]) + "\n")
            path.write_text("\n".join(lines[half:]) + "\n",
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

BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip",
    ".gz", ".xz", ".bz2", ".7z", ".tar", ".whl", ".so", ".o", ".a", ".dll",
    ".exe", ".bin", ".pyc", ".class", ".jar", ".woff", ".woff2", ".ttf",
    ".otf", ".mp3", ".mp4", ".ogg", ".wav", ".webm", ".mov", ".sqlite",
    ".db", ".gguf"}


def _is_text(path):
    """Text by its name where the name says so; otherwise by its first
    bytes — a file type nobody listed is still readable if it reads."""
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    try:
        with path.open("rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    if path.suffix.lower() in TEXT_SUFFIXES or path.name.lower() in (
            "makefile", "dockerfile", "license", "readme"):
        return True
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        return exc.start >= len(head) - 4     # only a character cut in half


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


HIDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea"}


def files(slug, limit=4000):
    """Every file in the project, for the Files view: the work and — marked
    as such — her own records. [{path, size, lines, text, mtime, record,
    by}] where `by` is the last task that changed the file."""
    proj = _proj(slug)
    auto_name = eng._auto_dir(slug, create=False).name
    last = {}
    for r in eng.project_changes(slug):
        if r.get("status") == "done":
            for f in r.get("files", []):
                last[f["path"]] = {"task": r["task"], "title": r["title"],
                                   "t": r.get("t", "")}
    rows = []
    for root, dirs, names in os.walk(proj):
        dirs[:] = sorted(d for d in dirs if d not in HIDE_DIRS)
        for name in sorted(names):
            f = Path(root) / name
            rel = f.relative_to(proj).as_posix()
            if name in ("LOCK",) or name.endswith(".tmp"):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            text = st.st_size < 2_000_000 and _is_text(f)
            lines = 0
            if text and st.st_size < 400_000:
                try:
                    with open(f, "rb") as fh:
                        lines = fh.read().count(b"\n") + 1
                except OSError:
                    pass
            row = {"path": rel, "size": st.st_size, "lines": lines,
                   "text": bool(text), "mtime": int(st.st_mtime),
                   "record": rel.startswith(auto_name + "/")}
            if rel in last:
                row["by"] = last[rel]
            rows.append(row)
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
    giants = []
    if cfg["giants_use"] != "never":
        for role, info in aria_garage.configured(cfg).items():
            ok, why = aria_garage.available(role, cfg)
            giants.append({"role": role, "label": info["label"],
                           "ok": ok, "why": why})
    tools = ["write and rewrite project files (whole files, verified "
             "before they are kept)",
             "check code for syntax errors (Python, JSON, shell, "
             "JavaScript) — parsing only"]
    if eng.checks_available(cfg)[0]:
        tools.append("the project's own tests are run after every code "
                     "change, in a sandbox with no network — a change that "
                     "breaks tests that passed is rejected")
        limits = ["cannot install packages or reach the network when tests "
                  "run: tests must be self-contained and quick",
                  "cannot run anything else, or use a shell",
                  "cannot see images, audio or binary files"]
    else:
        limits = ["cannot run or test code, install packages, or use a "
                  "shell",
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
            "models": extra, "giants": giants, "use": cfg["giants_use"],
            "tools": tools, "limits": limits}


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
    usable = [g for g in cap.get("giants", []) if g["ok"]]
    if usable:
        lines.append("GIANTS — the strongest models here and by far the "
                     "slowest (about 0.5–2 words a second: one task can "
                     "take an hour). Assign one by its role name only when "
                     "the work is worth that wait"
                     + (" — they are assigned automatically to matching "
                        "work" if cap.get("use") == "always" else "") + ":")
        lines += [f"- {g['role']} = {g['label']}  (best for "
                  f"{aria_garage.ROLE_SAYS[g['role']]})" for g in usable]
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
  "research": false, "needs_human": false,
  "done_when": "<observable criterion a reviewer can check>"}],
 "project_complete": false}

Rules:
- 3 to 8 tasks. Each is concrete, self-contained, and produces one thing.
- "agent": pick the specialist that fits the task from AGENTS.
- "skills": up to 3 names from SKILLS that genuinely help; otherwise [].
- "model": "" uses the agent's default. Set "fast", "smart" or "deep" (or a
  listed backend/model, or a GIANT's role name) only when the task clearly
  needs less or more than the default — fast for simple drafting, deep or
  a giant for the hardest work.
- "depends_on": ids from THIS list whose output the task builds on.
- "reads": files from FILES that the task must see to do its work.
- "research": true only for tasks that need facts from the live web%s
- "needs_human": true ONLY when the task turns on something that is the
  owner's to decide or do — spending money, an irreversible choice, an
  account or credential only they hold. Such a task waits for their answer
  while the rest carries on, so make nothing depend on it that does not
  have to. Everything else you decide yourself.
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

REVIEW_SYSTEM = """You are reviewing ONE file of a working project, to find
what would make it better WITHOUT changing what it does. Respond with ONLY
JSON:

{"findings": [{"kind": "bug|dead-code|duplication|complexity|performance|error-handling|naming|docs|tests",
  "where": "<the function, class or lines>",
  "problem": "<what is wrong, concretely>",
  "fix": "<the change to make>",
  "risk": "safe|careful", "severity": 2}]}

Rules:
- At most 5 findings, and only ones worth a change. No taste, no style
  nitpicks, no "could be rewritten as".
- "risk": "safe" = cannot change behaviour (dead code, a duplicated block
  folded into a helper, a clearer local name, a missing docstring, an
  unused import). "careful" = touches logic.
- "severity": 3 = a real defect or a serious waste, 2 = worth doing,
  1 = minor.
- A real bug counts: kind "bug", and say how it shows itself.
- If the file is fine as it is, answer {"findings": []}. Most small files
  are."""

REFINE_RULE = """THIS TASK REFINES WORKING CODE. It must not change what the
code does: the same functions with the same signatures, the same results
for the same inputs, the same files read and written, the same command
line. Fail it if anything a user or a caller could observe may differ, if
it does more than the listed points, or if you cannot tell."""

SUMMARY_SYSTEM = """You write the opening summary of a report on an
autonomous project run, for the project's owner. At most 170 words of plain
prose, no headings or lists: what was accomplished, what state the project
is in now, and anything the owner should check or worry about. Only use
what you are given."""


# ------------------------------------------------------------ memory

def _memory(slug):
    """The working memory. A project first run under the earlier autopilot
    kept its lessons in lessons.md — they carry over until the first
    rewrite folds them into MEMORY.md."""
    auto = _auto(slug)
    for name in ("MEMORY.md", "lessons.md"):
        if (auto / name).is_file():
            return (auto / name).read_text(encoding="utf-8", errors="replace")
    return ""


def _remember(state, key, title, cap=400):
    """Keep a normalised task title in one of the state's bounded lists."""
    norm = _norm_title(title)
    if norm:
        state[key] = ([x for x in state.get(key, []) if x != norm]
                      + [norm])[-cap:]


def _notify(cfg, text, title="A.R.I.A. needs you"):
    """Reach the owner: a notification on their desktop, and the
    mission-done webhook if they named one."""
    try:
        eng.desktop_notify(title, text)
    except Exception:
        pass                               # a notification is never fatal
    try:
        if "mission-done" in cfg["webhooks"]:
            eng.trigger_webhook("mission-done", text)
    except Exception:
        pass


# ------------------------------------------------------------ the owner's gate
#
# A task marked needs_human is the owner's to decide. It stays in To Do
# until they answer — approve (optionally with the decision the task must
# follow) or decline — and so does everything that builds on it. The rest
# of the project carries on around it.

def _gated(plan):
    """Tasks that wait for the owner's answer before they may run."""
    return [t for t in plan["tasks"]
            if t["status"] == "todo" and t.get("needs_human")]


def _held(plan):
    """Ids of the unstarted tasks that cannot move until the owner answers:
    the gated ones and everything that builds on them."""
    held = {t["id"] for t in _gated(plan)}
    grew = bool(held)
    while grew:
        grew = False
        for t in plan["tasks"]:
            if t["status"] == "todo" and t["id"] not in held \
                    and any(d in held for d in t["depends_on"]):
                held.add(t["id"])
                grew = True
    return held


def _pending_decisions(auto):
    """Answers the owner has given that are not folded into the plan yet."""
    out = {}
    for f in sorted(auto.glob("decision-*.json")):
        d = _read_json(f, None)
        if isinstance(d, dict) and d.get("task") \
                and d.get("answer") in ("approved", "declined"):
            out[str(d["task"])] = d
    return out


def _settle(task, decision, run=0):
    """Apply the owner's answer to a task."""
    note = str(decision.get("note") or "")
    task["needs_human"] = False
    task["decision"] = {"answer": decision["answer"], "note": note,
                        "via": str(decision.get("via") or ""),
                        "at": str(decision.get("at") or ""), "run": run}
    if decision["answer"] == "declined":
        task["status"] = "skipped"
        task["finished"] = _stamp()
        task["feedback"] = "declined by you" + (f" — {note}" if note else "")


def _apply_decisions(slug, state):
    """Fold the owner's answers into the plan. Only ever called with the
    project lock held. Returns how many were applied."""
    auto = _auto(slug)
    files = sorted(auto.glob("decision-*.json"))
    if not files:
        return 0
    plan = _plan(slug)
    by = {t["id"]: t for t in plan["tasks"]}
    applied = 0
    for f in files:
        d = _read_json(f, None)
        task = by.get(str((d or {}).get("task"))) if isinstance(d, dict) \
            else None
        if task is not None and task["status"] == "todo" \
                and task.get("needs_human") \
                and d.get("answer") in ("approved", "declined"):
            _settle(task, d, state["run"])
            if d["answer"] == "declined":
                state["stats"]["skipped"] += 1
                _remember(state, "declined_titles", task["title"], 100)
            applied += 1
        f.unlink(missing_ok=True)
    if applied:
        _save_plan(slug, plan)
        state["gate_wait"] = False
        _save_state(slug, state)
    return applied


def decide(slug, task_id, approve=True, note="", via="app"):
    """The owner's answer on a task that waits for them. `note` is the
    decision itself ("call it Larder", "use the free tier") — an approved
    task is told to follow it. The answer is a file, like STOP: it shows in
    the app at once and is folded into the plan before her next step."""
    eng._project_dir(slug)                       # validates the slug
    task_id = str(task_id or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", task_id):
        raise ValueError("that is not a task id")
    auto = _auto(slug)
    task = next((t for t in _plan(slug)["tasks"] if t["id"] == task_id), None)
    if task is None or task["status"] != "todo" \
            or not task.get("needs_human") \
            or task_id in _pending_decisions(auto):
        raise ValueError(f"no task with the id {task_id} is waiting on you "
                         f"in {slug}")
    note = " ".join(str(note or "").split())[:600]
    decision = {"task": task_id, "answer": "approved" if approve
                else "declined", "note": note, "via": via, "at": _stamp()}
    _write_json(auto / f"decision-{task_id}.json", decision)
    _activity(slug, "info",
              f"you {'approved' if approve else 'declined'} {task_id} — "
              f"{task['title']}" + (f": {note}" if note else ""))
    try:
        with _locked(slug):
            _apply_decisions(slug, _state(slug))
    except Busy:
        pass                 # she is mid-step; it is applied before the next
    return {"title": task["title"], **decision}


def _with_decisions(plan, auto):
    """The plan as the owner should see it: their answers show at once,
    also while she is mid-step and has not folded them in yet."""
    pending = _pending_decisions(auto)
    for t in plan["tasks"]:
        d = pending.get(t["id"])
        if d and t["status"] == "todo" and t.get("needs_human"):
            _settle(t, d)
    return plan


# ------------------------------------------------------------ assess

def assess(slug, emit):
    """Step 1: take stock. Writes ASSESSMENT.md and returns its text."""
    cfg = eng.config()
    goal = eng._project_goal(slug, 3500)
    cap = capabilities(goal, cfg)
    rows = inventory(_proj(slug))
    key = [r["path"] for r in rows if r["path"].lower() in (
        "readme.md", "readme.rst", "readme.txt", "package.json",
        "pyproject.toml", "cargo.toml", "go.mod", "makefile")]
    excerpts = _read_for_task(slug, key, 2400)
    prompt = (f"PROJECT: {slug}\n\nGOAL / REQUIREMENTS\n{goal}\n\n"
              f"WHAT I HAVE TO WORK WITH\n{capabilities_text(cap)}\n\n"
              f"FILES THAT ALREADY EXIST\n{_inventory_text(rows, 1200)}"
              + (f"\n\nKEY FILES (excerpts)\n{excerpts}" if excerpts else "")
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
    role = eng.giant_role(spec)
    if role:                 # only the giants the owner set up and allows
        return role if cfg["giants_use"] != "never" \
            and role in aria_garage.configured(cfg) else ""
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


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return value is True or value == 1


_CODE_WORDS = re.compile(
    r"\.(py|js|ts|tsx|jsx|rs|go|java|kt|c|cpp|h|cs|rb|php|swift|sh|sql|"
    r"html|css|vue|svelte|dart|lua)\b|\b(code|implement|function|class|"
    r"refactor|bug|test|script|api|endpoint|module)\b", re.I)


def _matching_giant(task, cfg):
    """With giants_use: always — the giant whose role fits the work."""
    have = aria_garage.configured(cfg)
    if task.get("research") or task["agent"] == "researcher":
        role = "research"
    elif task["agent"] == "coder" or _CODE_WORDS.search(
            task["title"] + " " + task["detail"]):
        role = "coder"
    else:
        role = "general"
    return role if role in have and aria_garage.available(role, cfg)[0] \
        else ""


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
    held = _held(plan)

    sections = [f"PROJECT: {slug}",
                f"GOAL / REQUIREMENTS\n{goal}"]
    memory = _memory(slug)
    if memory:
        sections.append("PROJECT MEMORY (what has happened so far)\n"
                        + eng.clip(memory, 2600))
    elif (auto / "ASSESSMENT.md").is_file():
        sections.append("YOUR ASSESSMENT\n" + eng.clip(
            (auto / "ASSESSMENT.md").read_text(encoding="utf-8"), 2600))
    sections.append("FILES (what exists now)\n" + _inventory_text(rows, 1500))
    declined = [t for t in plan["tasks"]
                if (t.get("decision") or {}).get("answer") == "declined"]
    finished = [t for t in plan["tasks"]
                if t["status"] in ("done", "failed") or t in declined]
    if finished:
        recent = finished[-14:]
        sections.append("RECENT RESULTS\n" + "\n".join(
            f"- {t['title']} — "
            + ("declined by the owner" + (
                f" ({eng.clip(t['decision']['note'], 140)})"
                if t["decision"].get("note") else "") if t in declined
               else t["status"] + (
                   f" ({eng.clip(t.get('feedback', ''), 140)})"
                   if t["status"] == "failed" else "")) for t in recent))
    if held:
        sections.append(
            "WAITING ON THE OWNER'S DECISION (already planned — do not plan "
            "these again, and plan nothing that needs their outcome)\n"
            + "\n".join(f"- {t['title']}" for t in plan["tasks"]
                        if t["id"] in held))
    unstarted = [t for t in plan["tasks"]
                 if t["status"] == "todo" and t["id"] not in held]
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
            "MODE: the stated goal is met"
            + (" and audited." if not state.get("audit_gaps") else
               " as far as you could take it; the gaps above are still "
               "open — close them first if files can close them.")
            + " Plan the most valuable IMPROVEMENTS now — fix weaknesses, "
            "harden, document, extend toward the obvious next step. Only "
            "real improvements: if nothing worthwhile is left, answer "
            "project_complete.")
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
    # the duplicate guard: what is done stays done however often the planner
    # proposes it again — also after the task has left the board. The list
    # is seeded once from the complete record.
    if "done_titles" not in state:
        state["done_titles"] = []
        for r in eng.project_changes(slug):
            if r.get("status") == "done":
                _remember(state, "done_titles", r.get("title", ""))
    done_titles = set(state["done_titles"]) | {
        _norm_title(t["title"]) for t in plan["tasks"]
        if t["status"] == "done"}
    declined_titles = set(state.get("declined_titles", []))
    failed_twice = {k for k, n in state.get("failed_titles", {}).items()
                    if n >= 2}
    held_by_title = {_norm_title(t["title"]): t["id"]
                     for t in plan["tasks"] if t["id"] in held}
    iteration = state["iteration"] + 1
    # ids never repeat — also not for a plan written by an older version,
    # which numbered tasks without keeping a counter
    used = [int(t["id"][1:]) for t in plan["tasks"]
            if re.fullmatch(r"t\d+", str(t.get("id", "")))]
    state["next_id"] = max(state["next_id"], max(used, default=0) + 1)
    fresh, idmap, by_title, dropped = [], {}, {}, []
    for raw in raw_tasks[:10]:
        title = " ".join(str(raw.get("title") or "").split())[:140]
        norm = _norm_title(title)
        if not norm:
            continue
        again = held_by_title.get(norm) or by_title.get(norm)
        reason = ("already done" if norm in done_titles else
                  "you declined it" if norm in declined_titles else
                  "it failed twice before" if norm in failed_twice else
                  "already waiting on you" if norm in held_by_title else
                  "listed twice" if norm in by_title else "")
        if reason:
            if again:              # what builds on it builds on the original
                idmap[str(raw.get("id"))] = again
            dropped.append(f"{title} ({reason})")
            continue
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
        by_title[norm] = tid
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
            "needs_human": _truthy(raw.get("needs_human")),
            "done_when": str(raw.get("done_when") or "the work product "
                             "exists and is complete").strip()[:400],
            "status": "todo", "attempts": 0, "iteration": iteration,
        })
    if cfg["giants_use"] == "always":
        for t in fresh:
            if not t["model"]:
                t["model"] = _matching_giant(t, cfg)
    for t in fresh:
        t["depends_on"] = list(dict.fromkeys(
            idmap[d] for d in t["depends_on"]
            if d in idmap and idmap[d] != t["id"]))
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
                  + (" (needs you)" if t["needs_human"] else "")
                  for t in fresh[:8])
              if fresh else "the planner has nothing more to add")})
    if dropped:
        emit({"event": "stage_done", "stage": "plan",
              "log": f"{slug}: left out {len(dropped)} task(s) the planner "
                     "proposed again — " + "; ".join(dropped[:5])})
    gates = [t for t in fresh if t["needs_human"]]
    if gates:
        names = "; ".join(f"{t['id']} — {t['title']}" for t in gates[:4])
        emit({"event": "stage_done", "stage": "plan",
              "log": f"{slug}: {len(gates)} task(s) need your decision "
                     f"before they can run — {names}. I'm carrying on with "
                     "everything that does not depend on them."})
        _notify(cfg, f"autopilot {slug} needs your decision: {names}")
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

def _task_model(task):
    """The model spec a task runs on: its own, else its agent's."""
    if task.get("model"):
        return task["model"]
    agent = eng.get_agent(task["agent"])
    return (agent or {}).get("model") or "smart"


def _runnable(plan):
    done = {t["id"] for t in plan["tasks"] if t["status"] == "done"}
    known = {t["id"] for t in plan["tasks"]}
    ready = [t for t in plan["tasks"] if t["status"] == "todo"
             and not t.get("needs_human")       # the owner has not answered
             and all(d in done or d not in known for d in t["depends_on"])]
    # first attempts before retries, so one stubborn task cannot starve
    # the rest of the batch
    return sorted(ready, key=lambda t: t["attempts"] > 0)


def _task_prompt(slug, task, plan, cfg):
    auto = _auto(slug)
    rows = inventory(_proj(slug))
    # sized to the window of the model this task runs on; the rest of
    # that window is the system prompt and the answer
    budget = int(eng.prompt_budget(cfg, _task_model(task)) * 0.55)
    parts = []
    decision = task.get("decision") or {}
    if decision.get("answer") == "approved":
        parts.append(
            "THE OWNER APPROVED THIS TASK"
            + (f" WITH THIS DECISION: {decision['note']}\nFollow it exactly "
               "— it is theirs to make, not yours to revisit."
               if decision.get("note") else
               " — go ahead and make the call the task asks for."))
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
    memory = _memory(slug)
    if memory:
        parts.append("PROJECT MEMORY\n" + eng.clip(memory, 1400))
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


def _wrote_code(records):
    """Did the task change anything a test could notice?"""
    return any(r["action"] != "unchanged" and not r["path"].lower().endswith(
        (".md", ".txt", ".rst")) for r in records)


def _run_checks(slug, emit, label):
    """The project's own tests, in the sandbox. The result, trimmed for
    the record — plus the output's tail under "output"."""
    emit({"event": "thinking", "label": label})
    res = eng.project_check(slug)
    return {k: res[k] for k in ("ran", "ok", "cmd", "summary", "why",
                                "sandboxed", "seconds", "output")}


def _baseline(slug, state, cfg, emit):
    """What the project's tests said BEFORE this task touches anything —
    known from the last verified task, else found out now. A task is
    blamed for a failure only if the tests passed without it."""
    if state.get("check") is not None or not eng.checks_available(cfg)[0]:
        return state.get("check")
    res = _run_checks(slug, emit, f"running {slug}'s tests before the task")
    if not res["ran"]:
        return None                      # no tests yet: nothing to compare
    res.pop("output", None)
    state["check"] = res
    _save_state(slug, state)
    if res["ok"] is False:
        emit({"event": "error",
              "log": f"{slug}: the project's tests are already failing "
                     f"({res['summary']}) — the next task is not blamed "
                     "for that"})
    return res


def _verify(slug, task, output, records, problems, cfg, emit, checks=None,
            before=None):
    """Step 4. Mechanical problems — and tests the change broke — fail
    outright; otherwise a judge reads the work against done_when, with the
    test result in front of it."""
    if problems:
        return {"pass": False, "reviewed": True,
                "feedback": "; ".join(problems)[:1800]}
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
    if checks and checks["ran"]:
        body += (f"\n\nTHE PROJECT'S OWN TESTS (`{checks['cmd']}`, run in a "
                 "sandbox): " + (f"PASS — {checks['summary']}" if checks["ok"]
                                 else "FAILING, as they already were before "
                                      f"this task — {checks['summary']}"))
    elif task.get("refine"):
        body += ("\n\nTHE PROJECT'S OWN TESTS could not be run, so nothing "
                 "but your reading stands between this change and a "
                 "regression.")
    if task.get("refine"):
        body += "\n\n" + REFINE_RULE + _before_after(slug, records, cfg)
    emit({"event": "thinking", "label": f"verifying {task['id']}"})
    try:
        data = eng.ask_json(eng.resolve_tier("smart", cfg),
                            eng.clip(body, budget + 1500), VERIFY_SYSTEM,
                            on_token=lambda t: emit({"event": "token",
                                                     "text": t}),
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


def _decision_line(decision, lead):
    """How the owner's answer on a gated task reads in the records."""
    return (f"{lead}Your decision: {decision.get('answer', 'approved')}"
            + (f" via {decision['via']}" if decision.get("via") else "")
            + (f" — “{decision['note']}”" if decision.get("note") else ""))


def _checks_line(checks):
    """How a test run reads in the records."""
    if not checks:
        return ""
    if not checks.get("ran"):
        return "not run — " + (checks.get("why") or "no reason given")
    return (f"`{checks['cmd']}` "
            + ("in a sandbox" if checks.get("sandboxed") else "unsandboxed")
            + " — " + ("" if checks["ok"] else "FAILING (as before this "
                       "task): ") + (checks.get("summary") or "ok"))


def _record(slug, state, task, records, info, verdict, output, commit="",
            checks=None):
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
    if task.get("decision"):
        row["decision"] = task["decision"]
    if checks:
        row["checks"] = {k: v for k, v in checks.items() if k != "output"}
    if task.get("refine"):
        row["refine"] = True
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
    if task.get("decision"):
        line += _decision_line(task["decision"], "\n  - ")
    if checks:
        line += "\n  - Tests: " + _checks_line(checks)
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
                                       "plan.json", "plan-archive.jsonl",
                                       "MEMORY.md", "ASSESSMENT.md",
                                       "REVIEW.md", "FINAL-REPORT.md",
                                       "STEERING.md")
        ] + [p.relative_to(proj).as_posix()
             for p in list(auto.glob("task-*.md"))
             + list(auto.glob("reports/*.md"))
             + list(auto.glob("reviews/*.md"))] + ["STATUS.md", ".gitignore"]
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
    task.setdefault("first_started", task["started"])
    _save_plan(slug, plan)
    picked = eng.get_agent(task["agent"])
    target = eng.resolve_model(
        task["model"] or (picked["model"] if picked else "smart"), cfg)
    state.update(phase="working", waiting="", current={
        "id": task["id"], "title": task["title"], "agent": task["agent"],
        "model": (target or {}).get("spec") or task["model"] or "smart",
        "started": _stamp(), "attempt": task["attempts"]})
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
    role = eng.task_role(model_spec, cfg)
    if role and role not in aria_garage.loaded_roles(cfg) \
            and aria_garage.available(role, cfg)[0]:
        emit({"event": "thinking",
              "label": f"loading the {role} giant for {task['id']}"})
        emit({"event": "stage_done", "stage": "garage",
              "log": f"{slug}: {task['id']} needs the {role} giant — "
                     "loading it (others are unloaded first; this can take "
                     "several minutes)"})
    checks = None
    try:
        before = _baseline(slug, state, cfg, emit)
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
        problems = static_checks(slug, records)
        if not problems and _wrote_code(records) \
                and eng.checks_available(cfg)[0]:
            checks = _run_checks(slug, emit,
                                 f"running {slug}'s tests on {task['id']}")
            if checks["ran"] and checks["ok"] is False \
                    and (before is None or before["ok"]):
                # they passed (or did not exist) without this change
                problems = [
                    f"the project's own tests fail with this change "
                    f"(`{checks['cmd']}`: {checks['summary']}). They "
                    + ("passed before it" if before else
                       "are new with it, and must pass")
                    + ". Test output:\n" + eng.clip(checks["output"], 1300)]
                emit({"event": "error",
                      "log": f"{slug}: {task['id']} breaks the project's "
                             f"tests — {checks['summary']}"})
        verdict = _verify(slug, task, output, records, problems, cfg, emit,
                          checks, before)
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
    task["seconds"] = round(task.get("seconds", 0) + time.time() - t0, 1)
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
        state["run_tasks"] += 1
        _remember(state, "done_titles", task["title"])
        _save_plan(slug, plan)
        sha = _commit(
            slug, state, cfg, task["files"],
            f"{task['id']}: {task['title']}\n\n"
            + (f"Why: {task['why']}\n" if task.get("why") else "")
            + f"Agent: {task['agent']} · model: {info.get('model', '?')}\n"
              f"Verified: {verdict['feedback'] or 'passed'}", emit)
        _record(slug, state, task, records, info, verdict, output, sha,
                checks)
        if checks and checks["ran"]:        # the new "before", for the next
            state["check"] = {k: v for k, v in checks.items()
                              if k != "output"}
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
            state["run_tasks"] += 1
            norm = _norm_title(task["title"])
            failed = dict(state.get("failed_titles", {}))
            failed[norm] = failed.pop(norm, 0) + 1      # newest last
            state["failed_titles"] = dict(list(failed.items())[-200:])
            _record(slug, state, task, [], info, verdict, output,
                    checks=checks)
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
    old = _memory(slug)
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
    rows = inventory(_proj(slug))
    key = [r["path"] for r in rows if r["lines"]][:8]
    prompt = (f"REQUIREMENTS\n{eng._project_goal(slug, 3500)}\n\n"
              f"FILES THAT EXIST\n{_inventory_text(rows, 1500)}\n\n"
              + ("MEMORY OF THE RUN\n" + eng.clip(_memory(slug), 2000)
                 + "\n\n" if _memory(slug) else "")
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
    held = _held(plan)                 # waiting on the owner: they carry over
    for t in plan["tasks"]:
        if t["status"] == "todo" and t["id"] not in held:
            t["status"] = "skipped"          # its dependencies never landed
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
        if state.get("iter_done"):
            state["dry"] = 0
        elif any(t["status"] == "failed" for t in batch):
            # work was attempted and none of it landed. (A batch you
            # declined or steered away from is not a dry one.)
            state["dry"] += 1
    state["iter_done"] = 0
    state["closed_iteration"] = it
    _save_state(slug, state)
    _commit(slug, state, cfg, [], f"iteration {it}: memory and records", emit)
    _sync_brain(slug)
    limit = cfg["autopilot_stall_limit"]
    if state["dry"] >= limit:
        _finalize(slug, emit, "stalled",
                  f"{limit} batch(es) in a row completed nothing")
        return "finalized"
    return "reflected"


def _advance(slug, state, cfg, emit):
    """Plan the next batch — or audit, switch to improvements, or finish."""
    state["phase"] = "planning"
    _save_state(slug, state)
    added, complete = _plan_next(slug, state, emit, cfg)
    plan = _plan(slug)
    gated = _gated(plan)
    if added:
        state["empty_plans"] = 0
    if gated and not _runnable(plan):
        # all that is left — or all that was just planned — waits on the
        # owner. Park: "done" cannot be claimed over their head, and
        # planning on around tasks nobody may run would never end.
        return _wait_for_owner(slug, state, gated, cfg, emit)
    if added:
        state["phase"] = "working"
        _save_state(slug, state)
        return "replanned"
    if not state["stats"]["done"]:
        # nothing has ever been completed and the planner offers nothing:
        # that is a planner problem, never "the goal is met"
        state["empty_plans"] += 1
        _save_state(slug, state)
        limit = cfg["autopilot_stall_limit"]
        if state["empty_plans"] >= limit:
            _finalize(slug, emit, "stalled",
                      f"the planner returned no tasks {limit} time(s) in a "
                      "row — check the smart-tier model")
            return "finalized"
        return "replanned"
    open_gaps = state.get("audit_gaps") or []
    if state["mode"] == "improve":
        if not open_gaps and _refine_due(slug, state, cfg):
            # enough has changed since the code was last read
            _refine_begin(slug, state, emit, "improvements made")
            return "refining"
        _save_state(slug, state)
        _finalize(slug, emit, "complete",
                  "the goal is met and no further improvement was worth "
                  "making" if not open_gaps else
                  "the planner has nothing more to add; an audit still "
                  "lists: " + "; ".join(open_gaps[:4]))
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
        state.pop("audit_gaps", None)
        emit({"event": "stage_done", "stage": "audit",
              "log": f"{slug}: audit passed — the requirements are met"})
    state["audits"] = 0
    open_gaps = state.get("audit_gaps") or []     # audited twice, not closed
    if not open_gaps and _refine_due(slug, state, cfg):
        _refine_begin(slug, state, emit, "goal met")
        return "refining"
    if cfg["autopilot_after_goal"] == "improve":
        state["mode"] = "improve"
        _save_state(slug, state)
        emit({"event": "stage_done", "stage": "autopilot",
              "log": f"{slug}: " + (
                  "goal met — moving on to improvements until you stop me"
                  if not open_gaps else
                  f"the planner has nothing more to add and {len(open_gaps)} "
                  "audit gap(s) stay open — moving on to improvements with "
                  "those first")})
        return "improving"
    _save_state(slug, state)
    _finalize(slug, emit, "complete",
              "the goal is met and audited" if not open_gaps else
              "the planner has nothing more to add; an audit still lists: "
              + "; ".join(open_gaps[:4]))
    return "complete"


def _wait_for_owner(slug, state, gated, cfg, emit):
    """Everything that can be done without the owner is done. Park the run
    — no model is called again until they answer, steer, or stop."""
    names = ", ".join(f"{t['id']} ({t['title']})" for t in gated[:3]) \
        + (f" and {len(gated) - 3} more" if len(gated) > 3 else "")
    ids = sorted(t["id"] for t in gated)
    fresh_wait = state.get("gate_notified") != ids
    state.update(gate_wait=True, phase="waiting", current=None,
                 waiting=f"for your decision on {names}", gate_notified=ids)
    _save_state(slug, state)
    _update_status_file(slug, state)
    if fresh_wait:
        emit({"event": "stage_done", "stage": "autopilot",
              "log": f"{slug}: everything that does not need you is done — "
                     f"waiting for your decision on {names}"})
        _notify(cfg, f"autopilot {slug} is waiting for your decision on "
                     f"{names}")
    return "waiting"


# ------------------------------------------------------------ review & refine
#
# When the goal is met — and whenever the owner asks — she reads the whole
# codebase file by file, writes down what is worth changing (REVIEW.md),
# and turns the findings into tasks that must leave behaviour exactly as
# it was. Those run through the ordinary loop with a stricter judge: it is
# shown the change as a diff, told that nothing observable may differ, and
# has the project's own tests in front of it. Anything else is rolled
# back, like any rejected attempt.

CODE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rs",
                 ".go", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs",
                 ".rb", ".php", ".swift", ".lua", ".sh", ".bash", ".sql",
                 ".html", ".css", ".scss", ".vue", ".svelte", ".dart",
                 ".ex", ".exs", ".gd", ".r"}
REVIEW_FILES = 60           # files read in one pass, largest first
REFINE_TASKS = 8            # refinements planned from one review
REFINE_AFTER = 8            # verified changes before the code is re-read


def _review_queue(slug):
    """The source files worth reading: code, not documents or her records,
    most substantial first."""
    rows = [r for r in inventory(_proj(slug), limit=1000)
            if Path(r["path"]).suffix.lower() in CODE_SUFFIXES
            and r["lines"] > 3 and r["bytes"] < 200_000
            and not _protected(r["path"])]
    rows.sort(key=lambda r: -r["bytes"])
    return [r["path"] for r in rows[:REVIEW_FILES]]


def _before_after(slug, records, cfg):
    """A refinement as a diff — what its judge has to rule on."""
    proj = _proj(slug)
    budget = int(eng.prompt_budget(cfg) * 0.5)
    parts = []
    for r in records:
        if r["action"] != "modified" or r["old"] is None:
            continue
        try:
            new = (proj / r["path"]).read_text(encoding="utf-8",
                                               errors="replace")
        except OSError:
            continue
        diff = "\n".join(difflib.unified_diff(
            r["old"].splitlines(), new.splitlines(), "before/" + r["path"],
            "after/" + r["path"], lineterm="", n=2))
        parts.append(eng.clip(diff, max(800, budget // max(1, len(records)))))
    return ("\n\nTHE CHANGE, AS A DIFF\n" + "\n\n".join(parts)) if parts \
        else ""


def _refine_due(slug, state, cfg):
    """Is it time to read the code again? Once the goal is met, and then
    after every REFINE_AFTER verified changes."""
    if not cfg["autopilot_refine"]:
        return False
    last = state.get("refined_at")
    if last is not None and state["stats"]["done"] - last < REFINE_AFTER:
        return False
    return bool(_review_queue(slug))


def _refine_begin(slug, state, emit, why):
    queue = _review_queue(slug)
    state["mode"] = "refine"
    state["refine"] = {"stage": "review", "queue": queue, "total": len(queue),
                       "findings": [], "started": _stamp()}
    state["phase"] = "reviewing the code"
    _save_state(slug, state)
    emit({"event": "stage_done", "stage": "refine",
          "log": f"{slug}: {why} — reviewing the whole codebase "
                 f"({len(queue)} file(s)) for refinements that leave its "
                 "behaviour unchanged"})


def _review_file(slug, state, path, cfg, emit):
    """One file, one reviewer call. Returns its findings, cleaned."""
    proj = _proj(slug)
    try:
        text = (proj / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    critic = eng.get_agent("critic")
    spec = (critic or {}).get("model") or "smart"
    target = eng.resolve_model(spec, cfg) or eng.resolve_tier("smart", cfg)
    budget = eng.prompt_budget(cfg, spec)
    prompt = (f"PROJECT: {slug}\nWHAT IT IS FOR\n"
              f"{eng.clip(eng._project_goal(slug, 900), 900)}\n\n"
              f"THE FILE: {path}\n```\n{eng.clip(text, budget - 1800)}\n```")
    emit({"event": "thinking", "label": f"reviewing {path}"})
    data = eng.ask_json(target, prompt, REVIEW_SYSTEM,
                        on_token=lambda t: emit({"event": "token", "text": t}),
                        timeout=_idle_timeout(cfg, spec))
    state["stats"]["calls"] += 1
    raw = data.get("findings") if isinstance(data, dict) else data
    out = []
    for f in (raw or [])[:5]:
        if not isinstance(f, dict):
            continue
        problem = " ".join(str(f.get("problem") or "").split())[:400]
        fix = " ".join(str(f.get("fix") or "").split())[:400]
        if not problem or not fix:
            continue
        try:
            severity = min(3, max(1, int(f.get("severity") or 2)))
        except (TypeError, ValueError):
            severity = 2
        kind = str(f.get("kind") or "").lower().strip()
        out.append({
            "file": path, "problem": problem, "fix": fix,
            "kind": kind if kind in (
                "bug", "dead-code", "duplication", "complexity",
                "performance", "error-handling", "naming", "docs",
                "tests") else "other",
            "where": " ".join(str(f.get("where") or "").split())[:120],
            "risk": "careful" if str(f.get("risk") or "").lower().strip()
            != "safe" else "safe",
            "severity": severity})
    return out


def _write_review(slug, state, findings, planned):
    """REVIEW.md: what the review found, file by file, and what became of
    each finding. Every pass is kept under reviews/."""
    auto = _auto(slug)
    by_file = {}
    for f in findings:
        by_file.setdefault(f["file"], []).append(f)
    out = [f"# Code review — {slug}", "",
           f"*{_stamp()[:16].replace('T', ' ')} · {state['refine']['total']} "
           f"file(s) read · {len(findings)} finding(s) in "
           f"{len(by_file)} file(s) · {len(planned)} refinement task(s) "
           "planned.*", "",
           "Each refinement must leave the code's behaviour unchanged: it "
           "is judged on a diff, with the project's tests run on it where "
           "they can be, and rolled back otherwise.", ""]
    if not findings:
        out += ["Nothing worth changing was found.", ""]
    for path in sorted(by_file, key=lambda p: -max(
            f["severity"] for f in by_file[p])):
        task = planned.get(path)
        out += [f"## {_flink(path)}"
                + (f" — planned as `{task}`" if task else " — noted, not "
                   "planned in this pass"), ""]
        for f in sorted(by_file[path], key=lambda f: -f["severity"]):
            out.append(
                f"- **{f['kind']}** ({'!' * f['severity']}, {f['risk']})"
                + (f" in `{f['where']}`" if f["where"] else "")
                + f": {f['problem']} → {f['fix']}")
        out.append("")
    text = "\n".join(out)
    (auto / "reviews").mkdir(exist_ok=True)
    stamp = f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
    (auto / "reviews" / f"review-{stamp}.md").write_text(
        place_report(text, "../..", ".."), encoding="utf-8")
    (auto / "REVIEW.md").write_text(place_report(text, "..", "."),
                                    encoding="utf-8")


def _refine_plan(slug, state, cfg, emit):
    """Findings -> tasks, one per file, most serious first. No model is
    asked: the review already said what to change."""
    findings = state["refine"]["findings"]
    by_file = {}
    for f in findings:
        by_file.setdefault(f["file"], []).append(f)
    order = sorted(by_file, key=lambda p: (
        -max(f["severity"] for f in by_file[p]), -len(by_file[p])))
    plan = _plan(slug)
    names = {a["name"] for a in eng.list_agents()}
    iteration = state["iteration"] + 1
    used = [int(t["id"][1:]) for t in plan["tasks"]
            if re.fullmatch(r"t\d+", str(t.get("id", "")))]
    state["next_id"] = max(state["next_id"], max(used, default=0) + 1)
    fresh, planned = [], {}
    for path in order[:REFINE_TASKS]:
        points = sorted(by_file[path], key=lambda f: (
            -f["severity"], f["risk"] != "safe"))[:4]
        tid = f"t{state['next_id']}"
        state["next_id"] += 1
        planned[path] = tid
        task = {
            "id": tid, "title": f"Refine {path}"[:140],
            "detail": (f"Refine `{path}`. Make exactly these changes and "
                       "nothing else:\n" + "\n".join(
                           f"{i}. [{f['kind']}]"
                           + (f" {f['where']}:" if f["where"] else "")
                           + f" {f['problem']} — {f['fix']}"
                           for i, f in enumerate(points, 1))
                       + "\nKeep every name, signature and behaviour that "
                         "anything else relies on. Output the complete "
                         "file.")[:1800],
            "why": "found by the code review: " + "; ".join(
                f"{f['kind']} ({f['where'] or 'file'})" for f in points),
            "agent": "coder" if "coder" in names else _guess_agent(
                "refactor code", names),
            "skills": [], "auto_skills": False, "model": "",
            "phase": "refine", "est_hours": 1.0, "depends_on": [],
            "reads": [path], "research": False, "needs_human": False,
            "refine": True,
            "done_when": f"every listed point is addressed in {path} and "
                         "what the code does is unchanged",
            "status": "todo", "attempts": 0, "iteration": iteration}
        if cfg["giants_use"] == "always":
            task["model"] = _matching_giant(task, cfg)
        fresh.append(task)
    _write_review(slug, state, findings, planned)
    if not fresh:
        emit({"event": "stage_done", "stage": "refine",
              "log": f"{slug}: the review found nothing worth changing"})
        return 0
    state["iteration"] = iteration
    plan["tasks"].extend(fresh)
    plan["iteration"] = iteration
    plan["updated"] = _stamp()
    _save_plan(slug, plan)
    emit({"event": "stage_done", "stage": "refine",
          "log": f"{slug}: review done — {len(findings)} finding(s); "
                 f"{len(fresh)} refinement(s) planned: "
                 + "; ".join(t["title"] for t in fresh[:6])
                 + f". The full review is in {_auto(slug).name}/REVIEW.md"})
    return len(fresh)


def _refine_step(slug, state, cfg, emit):
    """One step of the review-and-refine pass (see above)."""
    auto = _auto(slug)
    if (auto / "REFINE").exists() or not state.get("refine"):
        (auto / "REFINE").unlink(missing_ok=True)
        if not _review_queue(slug):
            emit({"event": "stage_done", "stage": "refine",
                  "log": f"{slug}: there is no code to review yet"})
            state["mode"] = "improve" if state["stats"]["done"] else "build"
            state.pop("refine", None)
            _save_state(slug, state)
            return "reflected"
        if not state.get("refine"):
            _refine_begin(slug, state, emit, "asked to refine")
    ref = state["refine"]
    if ref["stage"] == "review":
        if ref["queue"]:
            path = ref["queue"][0]
            done = ref["total"] - len(ref["queue"])
            state["phase"] = f"reviewing the code ({done + 1} of {ref['total']})"
            _save_state(slug, state)
            try:
                ref["findings"] += _review_file(slug, state, path, cfg, emit)
            except Exception as exc:
                if eng.backend_trouble(exc):
                    raise                  # wait for the model, try again
                emit({"event": "error",    # one unreadable answer: move on
                      "log": f"{slug}: could not review {path} ({exc}) — "
                             "skipped"})
            ref["queue"].pop(0)
            state["errors"] = 0
            _save_state(slug, state)
            return "reviewed"
        ref["stage"] = "apply"
        state["phase"] = "working"
        added = _refine_plan(slug, state, cfg, emit)
        _save_state(slug, state)
        if added:
            return "replanned"
    # the refinements have run (or there were none): the pass is over
    state["refined_at"] = state["stats"]["done"]
    state.pop("refine", None)
    if cfg["autopilot_after_goal"] == "improve":
        state["mode"] = "improve"
        _save_state(slug, state)
        emit({"event": "stage_done", "stage": "autopilot",
              "log": f"{slug}: code reviewed and refined — moving on to "
                     "improvements until you stop me"})
        return "improving"
    state["mode"] = "build"
    _save_state(slug, state)
    _finalize(slug, emit, "complete",
              "the goal is met and audited, and the code reviewed and "
              "refined")
    return "complete"


def refine(slug, via="app"):
    """The owner asks for a review-and-refine pass now. A signal file, like
    STEER; autopilot is started if it is not running."""
    eng._project_dir(slug)
    (_auto(slug) / "REFINE").touch()
    _activity(slug, "info", f"you asked for a code review and refinement "
                            f"(via {via})")
    state = _state(slug)
    if state["status"] != "running":
        state = start(slug, via=via)
    return state


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
    if not eng.project_approved(slug):
        # e.g. a run begun under an older version that had no gate
        state.update(status="idle", phase="", current=None, waiting="")
        _save_state(slug, state)
        emit({"event": "error",
              "log": f"{slug}: paused — its plan has not been approved. "
                     "Approve it and start again; nothing is lost."})
        return "idle"
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


def _over_budget(state, cfg):
    """Which of the run's budgets has been reached, in words — or ""."""
    hours = (time.time() - state.get("run_started", time.time())) / 3600
    if cfg["autopilot_max_hours"] and hours >= cfg["autopilot_max_hours"]:
        return f"{cfg['autopilot_max_hours']:g} hour(s)"
    if cfg["autopilot_max_tasks"] \
            and state["run_tasks"] >= cfg["autopilot_max_tasks"]:
        return f"{cfg['autopilot_max_tasks']} task(s)"
    if cfg["autopilot_max_iterations"] \
            and state["iteration"] - state.get("run_first_iteration", 0) \
            >= cfg["autopilot_max_iterations"] \
            and state.get("closed_iteration") == state["iteration"]:
        return f"{cfg['autopilot_max_iterations']} batch(es)"
    return ""


def _steered(auto, state):
    """New guidance since the last plan? Returns the marker's timestamp
    (truthy) so that only the guidance that was planned for is cleared."""
    try:
        return (auto / "STEER").stat().st_mtime_ns
    except OSError:
        return -1 if state.get("steer_pending") else 0   # -1: an older state


def _clear_steer(auto, state, seen):
    state.pop("steer_pending", None)
    try:
        if (auto / "STEER").stat().st_mtime_ns == seen:
            (auto / "STEER").unlink()
    except OSError:
        pass


def _step(slug, state, cfg, emit):
    auto = _auto(slug)
    over = _over_budget(state, cfg)
    if over:
        _finalize(slug, emit, "budget", f"the run's budget of {over} was "
                  "reached")
        return "finalized"
    _apply_decisions(slug, state)          # the owner's answers on held tasks
    steered = _steered(auto, state)
    if state.get("gate_wait"):
        waiting_plan = _plan(slug)
        if _gated(waiting_plan) and not _runnable(waiting_plan) \
                and not steered and not (auto / "REFINE").exists():
            return "waiting"               # nothing to do until they answer
        state["gate_wait"] = False
    if state.get("waiting") or state["backoff_until"]:
        state["waiting"], state["backoff_until"] = "", 0
        _save_state(slug, state)
    if not (auto / "ASSESSMENT.md").is_file():
        state["phase"] = "assessing"
        _save_state(slug, state)
        assess(slug, emit)
        state["stats"]["calls"] += 1
        state["errors"] = 0
        _save_state(slug, state)
        return "assessed"
    plan = _plan(slug)
    if steered:
        held = _held(plan)
        replan = any(t["status"] == "todo" and t["id"] not in held
                     for t in plan["tasks"])
        if replan:                # work that has not started is planned anew
            state["phase"] = "planning"
            _save_state(slug, state)
            _plan_next(slug, state, emit, cfg, replace_unstarted=True)
            state["errors"] = 0
            state["phase"] = "working"
        _clear_steer(auto, state, steered)
        _save_state(slug, state)
        if replan:
            return "replanned"
    ready = _runnable(plan)
    if len(ready) > 1 and aria_garage.configured(cfg):
        # swapping a giant in costs minutes and a cold page cache: run what
        # the loaded giant (or no giant) can do before what needs a swap
        loaded = aria_garage.loaded_roles(cfg)

        def swap(t):
            role = eng.task_role(_task_model(t), cfg)
            return 1 if role and role not in loaded else 0
        ready.sort(key=lambda t: (t["attempts"] > 0, swap(t)))
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
    unsettled = state.get("closed_iteration") != state["iteration"] or any(
        not t.get("reflected") and t["status"] in ("done", "failed", "skipped")
        for t in plan["tasks"])       # e.g. a held task that finished later
    if plan["tasks"] and unsettled:
        result = _close_iteration(slug, state, plan, cfg, emit)
        state["errors"] = 0
        return result
    if state.get("mode") == "refine" or (auto / "REFINE").exists():
        result = _refine_step(slug, state, cfg, emit)
    else:
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
                 run_first_iteration=state["iteration"], current=None,
                 run_tasks=0, empty_plans=0, gate_wait=False,
                 gate_notified=[], check=None)
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
    auto = _auto(slug)
    with (auto / "STEERING.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- [{_stamp()[:16].replace('T', ' ')} via {via}] {text}\n")
    # a signal file, like STOP: her state is only written under her lock,
    # so guidance that arrives while she is mid-task is never overwritten
    (auto / "STEER").touch()
    _activity(slug, "info", f"guidance from you: {text}")
    return _state(slug)


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
             "budget": "Paused (budget reached) — see "
                       "autopilot/FINAL-REPORT.md",
             }.get(state["status"], "Not started")
    cur = state.get("current") or {}
    gated = _gated(plan)
    focus = cur.get("title") or (
        "Waiting for your decision on: "
        + "; ".join(t["title"] for t in gated[:3])
        if state.get("gate_wait") and gated else
        open_tasks[0]["title"] if open_tasks else "Nothing queued.")
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


def _flink(path):
    """A link to a project file, from wherever the report ends up."""
    return f"[`{path}`](@@ROOT@@/{urllib.parse.quote(path)})"


def _alink(output):
    """A link to one of her own records ("autopilot/task-t3.md")."""
    rel = output.split("/", 1)[1] if "/" in output else output
    return f"[`{output}`](@@AUTO@@/{urllib.parse.quote(rel)})"


def place_report(report, root, auto):
    """Resolve the report's file links for where a copy is written: `root`
    and `auto` are the paths from that copy to the project folder and to
    her records."""
    return report.replace("@@ROOT@@", root).replace("@@AUTO@@", auto)


def unlinked(report):
    """The report with file links as plain names (for a pull request body
    or anywhere else a relative link would lead nowhere)."""
    return re.sub(r"\[(`[^`\n]+`)\]\(@@(?:ROOT|AUTO)@@/[^)\s]*\)", r"\1",
                  report)


def build_report(slug, state, reason, detail="", summary=""):
    """The final report: a complete, deterministic list of every change in
    this run — files, reasoning, references — built from the structured
    record, never from a model's memory of it. Files are links (see
    place_report)."""
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
              "stalled": "paused itself (stalled)",
              "budget": "paused at its budget"}.get(reason, reason)
    plan = _with_decisions(_plan(slug), auto)
    waiting = _gated(plan)
    held = _held(plan)
    declined = [t for t in plan["tasks"]
                if (t.get("decision") or {}).get("answer") == "declined"
                and t["decision"].get("run") in (run, 0)]
    out = [f"# Final report — {slug}", "",
           f"Run {run} {ending} at {_stamp()[:16].replace('T', ' ')}"
           + (f" — {detail}" if detail else "") + ".",
           f"Started {str(state.get('started', '?'))[:16].replace('T', ' ')}"
           + (f" · iterations {state.get('run_first_iteration', 0) + 1}–"
              f"{state['iteration']}" if state["iteration"]
              > state.get("run_first_iteration", 0) else
              " · no new batch was planned")
           + f" · {len(done)} task(s) completed and "
           f"verified, {len(failed)} failed · {created} file(s) created, "
           f"{modified} modified.", ""]
    out += ["## Summary", "", summary.strip() or (
        f"{len(done)} task(s) were completed and verified in this run."), ""]
    ran = [r for r in done if (r.get("checks") or {}).get("ran")]
    code = [r for r in done if r.get("checks")]
    if ran:
        last = ran[-1]["checks"]
        out += [f"**Tested by running it:** the project's own tests were run "
                f"in a sandbox after {len(ran)} of {len(done)} change(s). "
                f"At the last one: `{last['cmd']}` — "
                + ("" if last["ok"] else "FAILING: ")
                + f"{last.get('summary') or 'ok'}.", ""]
    elif done:
        why = next((r["checks"].get("why") for r in reversed(code)
                    if r["checks"].get("why")), "") \
            or eng.checks_available()[1]
        out += ["**Not tested by running it.** Every change was checked by "
                "parsing and by a reviewing model only"
                + (f" — {why}" if why else "") + ". Run it yourself before "
                "relying on it (the Run tab).", ""]

    look = []
    for name in ("README.md", "docs/README.md"):
        if (_proj(slug) / name).is_file():
            look.append(f"{_flink(name)} — the project's own front page")
            break
    touched = {}
    for r in done:
        for f in r["files"]:
            touched.setdefault(f["path"], []).append(r["task"])
    for path, _tasks in sorted(touched.items(),
                               key=lambda kv: -len(kv[1]))[:5]:
        if not any(f"`{path}`" in x for x in look) and len(look) < 5 \
                and (_proj(slug) / path).is_file():
            look.append(f"{_flink(path)} — changed by {len(_tasks)} task(s)")
    if waiting:
        look.insert(0, f"**{len(waiting)} task(s) waiting for your "
                       "decision** — listed below; nothing that builds on "
                       "them can move until you answer")
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
                    f"{_flink(f['path'])} ({f['action']}, +{f['added']}/"
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
            if r.get("decision"):
                out.append(_decision_line(r["decision"], "   - **")
                           .replace("Your decision:", "Your decision:**", 1))
            if r.get("checks"):
                out.append("   - **Tests:** " + _checks_line(r["checks"]))
            refs = [_alink(r["output"])]
            if r.get("commit"):
                refs.append(f"commit `{r['commit']}`")
            refs += [f"[{s.get('title') or s['url']}]({s['url']})"
                     for s in r.get("sources", [])]
            out.append("   - **References:** " + " · ".join(refs))
        out.append("")

    if waiting:
        out += ["## Waiting for your decision", "",
                "These are yours to decide. Approve one (with your decision, "
                "if it needs one) or skip it — on the Projects page, or "
                f"`aria autopilot {slug} approve-task <id> \"…\"`.", ""]
        for w in waiting:
            behind = [t["id"] for t in plan["tasks"] if t["id"] in held
                      and t["id"] != w["id"] and w["id"] in t["depends_on"]]
            out.append(
                f"- **{w['title']}** (`{w['id']}`)"
                + (f" — {w['why'].rstrip('. ')}." if w.get("why") else ".")
                + (f" Waiting behind it: {', '.join(behind)}." if behind
                   else ""))
        out.append("")

    if failed or declined:
        out += ["## Not completed", ""]
        out += [f"- **{r['title']}** (`{r['task']}`) — failed after "
                f"{r['attempts']} attempt(s): {r.get('feedback') or '?'} "
                f"(attempts kept in {_alink(r['output'])}; nothing from "
                "them was left in the project)" for r in failed]
        out += [f"- **{d['title']}** (`{d['id']}`) — {d.get('feedback')}; "
                "it was not attempted" for d in declined]
        out.append("")

    if touched:
        out += ["## Files touched", "", "| File | Tasks |", "|---|---|"]
        out += [f"| {_flink(p)} | {', '.join(t)} |"
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
            f"readable log: " + (_alink(auto.name + "/CHANGELOG.md")
                                 if (auto / "CHANGELOG.md").is_file()
                                 else f"`{auto.name}/CHANGELOG.md`")
            + " · every "
            f"task's full output: `{auto.name}/task-<id>.md`. The project "
            f"lives in `{_proj(slug)}`."]
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
        memory = _memory(slug)
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
        place_report(report, "../..", ".."), encoding="utf-8")
    (auto / "FINAL-REPORT.md").write_text(
        place_report(report, "..", "."), encoding="utf-8")
    state["status"] = reason if reason in ("stopped", "complete", "stalled",
                                           "budget") else "stopped"
    state["gate_wait"] = False
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
            pushed = eng.project_push(
                slug, eng.clip(unlinked(report), 6000)).get("pr", "")
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
    _notify(cfg, f"autopilot {slug} {reason}"
            + (f": {detail}" if detail else ""),
            title={"complete": "A.R.I.A. — run complete",
                   "stalled": "A.R.I.A. paused herself",
                   "budget": "A.R.I.A. — budget reached",
                   }.get(reason, "A.R.I.A. — run stopped"))


# ------------------------------------------------------------ for the UI

def _archived(auto):
    """Finished tasks moved off the board (they stay in the history)."""
    rows = []
    path = auto / "plan-archive.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _count_lines(path):
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _brief_task(t):
    keep = ("id", "title", "status", "iteration", "agent", "model",
            "model_used", "phase", "est_hours", "seconds", "started",
            "first_started", "finished", "depends_on", "attempts", "files",
            "skills", "why", "detail", "done_when", "feedback", "decision",
            "research")
    row = {k: t[k] for k in keep if k in t}
    row.setdefault("depends_on", [])
    row["took"] = t.get("seconds", 0)
    row["archived"] = True
    return row


def _ts(stamp):
    try:
        return datetime.datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _timings(tasks, archived, state):
    """Real time on the board and the Gantt: how long each finished task
    took, and — for work not done yet — a prediction calibrated on how
    long this project's tasks have actually taken (per model where there
    is enough history), rather than the planner's guess."""
    finished = [t for t in archived + tasks if t.get("seconds")
                and t.get("status") in ("done", "failed")]
    ratios, by_model = [], {}
    for t in finished:
        size = max(0.25, float(t.get("est_hours") or 1))
        r = t["seconds"] / size
        ratios.append(r)
        by_model.setdefault(t.get("model_used") or t.get("model") or "",
                            []).append(r)

    def median(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else 0

    overall = median(ratios)
    for t in tasks:
        size = max(0.25, float(t.get("est_hours") or 1))
        if "seconds" in t and t["status"] in ("done", "failed", "skipped"):
            t["took"] = t["seconds"]          # measured, however short
            continue
        model_r = median(by_model.get(t.get("model") or "", []))
        rate = model_r if len(by_model.get(t.get("model") or "", [])) >= 3 \
            else overall
        t["predict"] = int(size * rate) if rate else 0
        if t["status"] in ("inprogress", "reviewing") and t.get("started"):
            t["elapsed"] = int(time.time() - _ts(t["started"]))
    return overall


def reports(slug):
    """Every run's final report, newest first: {name, run, title}."""
    folder = eng._auto_dir(slug, create=False) / "reports"
    out = []
    if folder.is_dir():
        for f in sorted(folder.glob("run-*.md"), reverse=True):
            m = re.match(r"run-(\d+)-(\d{8})-(\d{6})", f.stem)
            try:
                head = f.read_text(encoding="utf-8", errors="replace")[:600]
            except OSError:
                continue
            line = next((ln for ln in head.splitlines()
                         if ln.startswith("Run ")), "")
            out.append({"name": f.name, "run": int(m.group(1)) if m else 0,
                        "title": line.rstrip(".")})
    return out


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

    plan = _with_decisions(_plan(slug), auto) \
        if (auto / "plan.json").is_file() else {"tasks": []}
    pace = _timings(plan["tasks"], _archived(auto), state)
    held = _held(plan)
    for t in plan["tasks"]:
        t["held"] = t["id"] in held and not t.get("needs_human")
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
        "needs_you": [{"id": t["id"], "title": t["title"],
                       "why": t.get("why", "")} for t in _gated(plan)],
        "assessment": text("ASSESSMENT.md"),
        "review": text("REVIEW.md"),
        "memory": text("MEMORY.md") or text("lessons.md"),
        "steering": text("STEERING.md"),
        "changelog": text("CHANGELOG.md", tail=16000),
        "report": text("FINAL-REPORT.md"),
        "reports": reports(slug),
        "history": [_brief_task(t) for t in _archived(auto)],
        # measured seconds of work per unit of the planner's size estimate
        # (0 until a task has finished): what predictions are scaled by
        "pace": pace,
        "changes": eng.project_changes(slug, limit=300),
        "changes_total": _count_lines(auto / "changes.jsonl"),
        "activity": activity(slug) if auto.is_dir() else [],
        "live": live(slug),
        "git": git,
        "auto_dir": auto.name,
    }
