"""aria_garage — the giant models, one role each, swapped in on demand.

The owner keeps a few very large GGUF models on disk — larger than RAM, so
llama.cpp streams their experts from NVMe. Each gets a role:

    general    all-round reasoning and writing   (e.g. DeepSeek-R1 671B)
    research   knowledge and research            (e.g. Kimi K2 1T)
    coder      code                              (e.g. Qwen3-Coder 480B)

They share one GPU and one page cache, so by default only one is loaded at
a time. When work needs a role, `serving(role)` makes sure its llama-server
is up — stopping the least recently used giant first if there is no room —
and holds it for the length of the request, so a giant is never unloaded in
the middle of an answer.

Everything is kept in system/garage/: a pidfile and a log per giant this
module started, and a note of any load that failed (an incomplete download,
a file llama.cpp cannot read). A failed load is not retried until the files
change, six hours pass, or the owner asks for it.

Stdlib only; works across processes (the always-on server, a terminal
`aria autopilot run`, missions) through file locks.
"""

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GARAGE = ROOT / "system" / "garage"
ROLES = ("general", "research", "coder")
ROLE_SAYS = {"general": "all-round reasoning, planning and writing",
             "research": "research and broad knowledge",
             "coder": "writing and reviewing code"}
LOAD_TIMEOUT = 3 * 3600        # a giant's first load can be slow
BUSY_WAIT = 2 * 3600           # how long a swap waits for an answer to end
FAILURE_TTL = 6 * 3600
FRESH_SECONDS = 90             # a part written this recently is downloading

_SHARD = re.compile(r"^(?P<base>.+?)-(?P<n>\d{5})-of-(?P<of>\d{5})\.gguf$",
                    re.I)
_PROCS = {}                    # role -> Popen, for the process that started it
_HEADER_CACHE = {}


class GarageError(RuntimeError):
    """A giant cannot be used (not set, incomplete, failed to load)."""


class GarageBusy(GarageError):
    """A swap has to wait for another giant to finish answering."""


# ------------------------------------------------------------ configuration

def configured(cfg):
    """{role: {"path", "port", "label"}} for every role with a model set."""
    out = {}
    base = int(cfg.get("giants_port") or 8090)
    for i, role in enumerate(ROLES):
        raw = (cfg.get("giants") or {}).get(role, "")
        if not raw:
            continue
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            path = ROOT / path
        out[role] = {"path": path, "port": base + i, "label": label(path)}
    return out


def label(path):
    name = Path(path).name
    m = _SHARD.match(name)
    return (m.group("base") if m else re.sub(r"\.gguf$", "", name, flags=re.I))


def guess_role(name):
    n = name.lower()
    if "coder" in n or "codestral" in n or "devstral" in n:
        return "coder"
    if "kimi" in n or "research" in n:
        return "research"
    if re.search(r"deepseek|\br1\b|-r1-|reason|think|qwq|glm|gpt-oss", n):
        return "general"
    return ""


# ------------------------------------------------------------ files on disk

_FIXED = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8,
          12: 8}


def _gguf_min_size(path):
    """The size a GGUF file must have, by its own header: where its tensor
    data starts plus the offset of its last tensor. None if unreadable —
    the check is a safeguard, never a reason to refuse a file."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime)
    if key in _HEADER_CACHE:
        return _HEADER_CACHE[key]
    result = None
    try:
        with open(path, "rb", buffering=1 << 20) as f:
            def u32():
                return struct.unpack("<I", f.read(4))[0]

            def u64():
                return struct.unpack("<Q", f.read(8))[0]

            def skip(vt):
                if vt in _FIXED:
                    f.seek(_FIXED[vt], 1)
                elif vt == 8:
                    f.seek(u64(), 1)
                elif vt == 9:
                    et, n = u32(), u64()
                    if et in _FIXED:
                        f.seek(_FIXED[et] * n, 1)
                    else:
                        for _ in range(n):
                            skip(et)
                else:
                    raise ValueError(f"unknown GGUF value type {vt}")

            if f.read(4) == b"GGUF" and u32() >= 2:
                n_tensors, n_kv = u64(), u64()
                if n_tensors < 10_000_000 and n_kv < 1_000_000:
                    align = 32
                    for _ in range(n_kv):
                        key_name = f.read(u64())
                        vt = u32()
                        if key_name == b"general.alignment" and vt == 4:
                            align = u32() or 32
                        else:
                            skip(vt)
                    last = 0
                    for _ in range(n_tensors):
                        f.seek(u64(), 1)            # name
                        f.seek(8 * u32(), 1)        # dims
                        u32()                       # type
                        last = max(last, u64())     # offset
                    start = (f.tell() + align - 1) // align * align
                    result = start + last
    except (OSError, ValueError, struct.error):
        result = None
    _HEADER_CACHE[key] = result
    return result


def _size(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e8 else f"{n / 1e6:.1f} MB"


def model_set(path):
    """What is on disk for one model: every part of a multi-part GGUF and
    whether it is complete. `path` may be any part, or a single file."""
    path = Path(path)
    m = _SHARD.match(path.name)
    if m:
        of = int(m.group("of"))
        parts = [path.with_name(f"{m.group('base')}-{i:05d}-of-{of:05d}"
                                + path.suffix) for i in range(1, of + 1)]
    else:
        of, parts = 1, [path]
    found = [p for p in parts if p.is_file()]
    size = sum(p.stat().st_size for p in found)
    why = ""
    if len(found) < of:
        why = f"{len(found)} of {of} parts downloaded"
    else:
        now = time.time()
        for p in found:
            st = p.stat()
            if now - st.st_mtime < FRESH_SECONDS:
                why = f"{p.name} is still being written"
                break
            need = _gguf_min_size(p)
            if need and st.st_size < need:
                why = (f"{p.name} is incomplete ({_size(st.st_size)} of at "
                       f"least {_size(need)})")
                break
    sig = "|".join(f"{p.name}:{p.stat().st_size}:{int(p.stat().st_mtime)}"
                   for p in found)
    return {"path": str(parts[0]), "name": label(parts[0]), "parts": of,
            "found": len(found), "gb": round(size / 1e9, 1),
            "complete": not why, "why": why, "sig": sig}


def scan(folders, depth=4):
    """Every GGUF model (a multi-part set counted once) under `folders`."""
    seen, out = set(), []
    for folder in folders:
        root = Path(os.path.expanduser(str(folder)))
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            if len(Path(dirpath).parts) - base_depth >= depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith(".")
                           and d not in ("node_modules", "__pycache__")]
            for name in sorted(filenames):
                if not name.lower().endswith(".gguf") \
                        or name.lower().startswith("mmproj"):
                    continue
                info = model_set(Path(dirpath) / name)
                if info["path"] in seen:
                    continue
                seen.add(info["path"])
                info["guess"] = guess_role(info["name"])
                out.append(info)
    return out


def default_folders():
    home = Path.home()
    return [ROOT / "vendor" / "models", home / "models", home / "Models",
            home / "llm", home / "Downloads", home / ".cache" / "llama.cpp"]


# ------------------------------------------------------------ bookkeeping

def _file(name):
    GARAGE.mkdir(parents=True, exist_ok=True)
    return GARAGE / name


def _read(name, default=None):
    try:
        return json.loads(_file(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(name, obj):
    path = _file(name)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _alive(rec):
    """Is the process in this pidfile record still ours and running?"""
    if not rec or not rec.get("pid"):
        return False
    pid = int(rec["pid"])
    proc = _PROCS.get(rec.get("role"))
    if proc is not None and proc.pid == pid:
        return proc.poll() is None            # we started it: we know
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():                      # guard against a reused pid
        try:
            text = cmdline.read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace")
        except OSError:
            return True
        return str(rec.get("port")) in text or rec.get("path", "") in text \
            or "aria-llama" in text or "llama-server" in text
    return True


def _pidrec(role):
    rec = _read(f"{role}.json")
    return rec if _alive(rec) else None


def health(port, timeout=2):
    """'ready', 'loading', 'down' or 'other' for a llama-server port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=timeout) as r:
            return "ready" if r.status == 200 else "other"
    except urllib.error.HTTPError as exc:
        return "loading" if exc.code == 503 else "other"
    except (urllib.error.URLError, OSError, ValueError):
        return "down"


def _touch(role):
    used = _read("used.json", {}) or {}
    used[role] = time.time()
    _write("used.json", used)


def _failure(role, info):
    rec = _read(f"{role}.failed.json")
    if not rec:
        return None
    if time.time() - rec.get("at", 0) > FAILURE_TTL \
            or rec.get("sig") != model_set(info["path"])["sig"]:
        return None
    return rec


@contextlib.contextmanager
def _lock(name, mode, wait):
    """flock with a deadline (wait seconds; 0 = try once)."""
    fh = open(_file(name), "a+")
    deadline = time.time() + wait
    try:
        while True:
            try:
                fcntl.flock(fh, mode | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise GarageBusy(f"{name} is held")
                time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _set_status(role, phase, message=""):
    _write("status.json", {"role": role, "phase": phase, "message": message,
                           "at": time.time()})


# ------------------------------------------------------------ what is going on

def status(cfg, probe=True):
    """Everything the Settings page shows about the giants."""
    conf = configured(cfg)
    used = _read("used.json", {}) or {}
    out = {}
    for i, role in enumerate(ROLES):
        info = conf.get(role)
        row = {"role": role, "says": ROLE_SAYS[role],
               "port": int(cfg.get("giants_port") or 8090) + i,
               "path": str(info["path"]) if info else "",
               "label": info["label"] if info else "",
               "last_used": used.get(role, 0)}
        if not info:
            row.update(state="unset")
            out[role] = row
            continue
        ms = model_set(info["path"])
        row.update(parts=ms["parts"], found=ms["found"], gb=ms["gb"])
        rec = _pidrec(role)
        h = health(info["port"], timeout=1) if probe else (
            "ready" if rec else "down")
        if not ms["complete"]:
            row.update(state="incomplete", error=ms["why"])
        elif h == "ready":
            row.update(state="ready" if rec else "external",
                       since=(rec or {}).get("started", 0))
        elif rec or h == "loading":
            row.update(state="loading", since=(rec or {}).get("started", 0))
        elif not server_binary():
            row.update(state="blocked", error=NO_SERVER)
        else:
            fail = _failure(role, info)
            row.update(state="failed" if fail else "stopped",
                       error=(fail or {}).get("error", ""))
        test = _read(f"{role}.test.json")
        if test and test.get("sig") == ms["sig"]:
            row["test"] = test                # the last end-to-end proof
        out[role] = row
    now = next(({"role": r, "phase": "loading",
                 "message": f"loading {row['label']}"}
                for r, row in out.items() if row["state"] == "loading"), {})
    st = _read("status.json") or {}
    if not now and st.get("phase") == "waiting" \
            and time.time() - st.get("at", 0) < BUSY_WAIT:
        now = st
    return {"roles": out, "loaded": int(cfg.get("giants_loaded") or 1),
            "use": cfg.get("giants_use", "auto"), "now": now}


NO_SERVER = ("llama-server is not installed, so no giant can start — "
             "run: bin/aria llama install")


def server_binary():
    """Where llama-server is — the places bin/aria-llama looks, in its
    order. '' when there is none. (The self-test's stand-in command counts
    as one.)"""
    if os.environ.get("ARIA_GARAGE_COMMAND"):
        return "stand-in"
    vendor = ROOT / "vendor" / "llama.cpp"
    for path in (vendor / "bin" / "llama-server", vendor / "llama-server",
                 vendor / "build" / "bin" / "llama-server",
                 ROOT / "vendor" / "llama.cpp-src" / "build" / "bin"
                 / "llama-server"):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which("llama-server") or ""


def available(role, cfg):
    """(usable, why not) — cheap enough to ask before every call."""
    info = configured(cfg).get(role)
    if not info:
        return False, f"no model is set for the {role} role"
    ms = model_set(info["path"])
    if not ms["complete"]:
        return False, f"{info['label']}: {ms['why']}"
    if not server_binary():
        return False, NO_SERVER
    fail = _failure(role, info)
    if fail:
        return False, f"{info['label']} failed to load: {fail['error']}"
    return True, ""


def loaded_roles(cfg):
    """Roles whose giant is running now (ours, by pidfile — no network)."""
    return {r for r in configured(cfg) if _pidrec(r)}


# ------------------------------------------------------------ start and stop

def _command(info, cfg):
    """How a giant is started: aria-llama's `giant` preset, so the flags
    live in one place. ARIA_GARAGE_COMMAND overrides it (the self-test runs
    a stand-in server this way): a template with {gguf} and {port}."""
    template = os.environ.get("ARIA_GARAGE_COMMAND", "")
    if template:
        return [part.format(gguf=info["path"], port=info["port"])
                for part in shlex.split(template)]
    return [str(ROOT / "bin" / "aria-llama"), "serve", "giant",
            str(info["path"])]


def _start(role, info, cfg):
    log = _file(f"{role}.log")
    env = dict(os.environ, ARIA_LLAMACPP_PORT=str(info["port"]),
               ARIA_LLAMACPP_CTX=str(int(cfg.get("giants_ctx") or 8192)),
               ARIA_LLAMA_EXTRA=str(cfg.get("giants_flags") or ""))
    with open(log, "ab") as fh:
        fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} starting "
                 f"{info['label']} on :{info['port']}\n".encode())
        proc = subprocess.Popen(_command(info, cfg), stdout=fh,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env,
                                start_new_session=True, cwd=str(ROOT))
    _PROCS[role] = proc
    rec = {"role": role, "pid": proc.pid, "port": info["port"],
           "path": str(info["path"]), "started": time.time()}
    _write(f"{role}.json", rec)
    return rec


def _log_tail(role, lines=6):
    try:
        text = _file(f"{role}.log").read_text(encoding="utf-8",
                                              errors="replace")
    except OSError:
        return ""
    text = text.rsplit("\n=== ", 1)[-1]            # this start only
    tail = [ln.strip() for ln in text.splitlines()[1:] if ln.strip()]
    errors = [ln for ln in tail if re.search(r"error|fail|unable|not found|"
                                             r"cannot|exception", ln, re.I)]
    return " | ".join((errors or tail)[-lines:])[-600:]


def _wait_ready(role, info, rec, notify):
    t0 = time.time()
    last_note = 0
    while True:
        h = health(info["port"])
        if h == "ready":
            _set_status(role, "ready")
            if notify:
                notify(f"the {role} giant ({info['label']}) is loaded")
            return info["port"]
        if rec and not _alive(rec):
            error = _log_tail(role) or "the server exited while loading"
            _write(f"{role}.failed.json", {
                "at": time.time(), "error": error,
                "sig": model_set(info["path"])["sig"]})
            _file(f"{role}.json").unlink(missing_ok=True)
            _set_status(role, "failed", error)
            raise GarageError(f"{info['label']} could not be loaded: {error}")
        waited = time.time() - t0
        if waited > LOAD_TIMEOUT:
            stop(role)
            raise GarageError(f"{info['label']} did not finish loading in "
                              f"{LOAD_TIMEOUT // 60} minutes")
        if notify and time.time() - last_note > 30:
            last_note = time.time()
            notify(f"loading the {role} giant ({info['label']}) — "
                   f"{int(waited // 60)} min so far")
        _set_status(role, "loading",
                    f"loading {info['label']} — {int(waited // 60)} min")
        time.sleep(1 if waited < 20 else 3)


def _stop_process(rec):
    pid = int(rec["pid"])
    for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
        try:
            os.killpg(pid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                break
        deadline = time.time() + wait
        while time.time() < deadline and _alive(rec):
            proc = _PROCS.get(rec.get("role"))
            if proc is not None and proc.pid == pid:
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            else:
                time.sleep(0.5)
        if not _alive(rec):
            break


def stop(role, wait=0):
    """Unload a giant this module started. With `wait`, first wait (up to
    that many seconds) for any answer it is giving to finish; raises
    GarageBusy if it does not."""
    rec = _pidrec(role)
    if not rec:
        _file(f"{role}.json").unlink(missing_ok=True)
        return False
    drain = _file(f"{role}.draining")
    drain.touch()
    try:
        with _lock(f"{role}.busy", fcntl.LOCK_EX, wait):
            _stop_process(rec)
            _file(f"{role}.json").unlink(missing_ok=True)
            _PROCS.pop(role, None)
    finally:
        drain.unlink(missing_ok=True)
    _set_status(role, "stopped")
    return True


# Callables (role, cfg, notify) run just before a giant's server is
# started — the engine uses this to have the everyday model server let go
# of its memory first, so the giant gets it.
BEFORE_START = []


def _make_room(role, cfg, notify):
    limit = max(1, int(cfg.get("giants_loaded") or 1))
    used = _read("used.json", {}) or {}
    others = sorted((r for r in ROLES if r != role and _pidrec(r)),
                    key=lambda r: used.get(r, 0))
    while len(others) >= limit:
        victim = others.pop(0)
        if notify:
            notify(f"unloading the {victim} giant to make room for the "
                   f"{role} giant")
        _set_status(role, "waiting",
                    f"waiting for the {victim} giant to finish its answer")
        stop(victim, wait=BUSY_WAIT)


def ensure(role, cfg, notify=None):
    """Make sure the giant for `role` is up; returns its port. Starts it
    (unloading others first if needed) and waits until it is ready."""
    info = configured(cfg).get(role)
    if not info:
        raise GarageError(f"no model is set for the {role} role")
    ms = model_set(info["path"])
    if not ms["complete"]:
        raise GarageError(f"{info['label']}: {ms['why']}")
    if health(info["port"]) == "ready":
        return info["port"]
    with _lock("swap.lock", fcntl.LOCK_EX, BUSY_WAIT + 600):
        h = health(info["port"])
        if h == "ready":
            return info["port"]
        rec = _pidrec(role)
        if rec or h == "loading":         # someone already started it
            return _wait_ready(role, info, rec, notify)
        if not server_binary():
            raise GarageError(NO_SERVER)
        fail = _failure(role, info)
        if fail:
            raise GarageError(f"{info['label']} failed to load earlier: "
                              f"{fail['error']}")
        _make_room(role, cfg, notify)
        for hook in list(BEFORE_START):
            try:
                hook(role, cfg, notify)
            except Exception:
                pass                    # a courtesy, never a blocker
        if notify:
            notify(f"loading the {role} giant ({info['label']}) — the first "
                   "load can take several minutes")
        rec = _start(role, info, cfg)
        return _wait_ready(role, info, rec, notify)


@contextlib.contextmanager
def serving(role, cfg, notify=None):
    """Hold the giant for `role` while a request runs: it is loaded first
    if needed, and cannot be unloaded until the block ends."""
    for _ in range(5):
        port = ensure(role, cfg, notify)
        drain = _file(f"{role}.draining")
        while True:                       # it is being unloaded; let it go
            try:
                if time.time() - drain.stat().st_mtime > BUSY_WAIT + 60:
                    break                 # left behind by a crashed process
            except OSError:
                break
            time.sleep(1)
        with _lock(f"{role}.busy", fcntl.LOCK_SH, BUSY_WAIT):
            if health(port) == "ready":
                _touch(role)
                try:
                    yield port
                finally:
                    _touch(role)
                return
    raise GarageBusy(f"the {role} giant kept being unloaded — try again")


def load(role, cfg, notify=None, retry=False):
    """Load a giant now (Settings' Load button, `aria models load`)."""
    if retry:
        _file(f"{role}.failed.json").unlink(missing_ok=True)
    return ensure(role, cfg, notify)


def note_test(role, cfg, result):
    """Keep the outcome of an end-to-end test of a giant (see the engine's
    garage_test) for the Settings page."""
    info = configured(cfg).get(role)
    sig = model_set(info["path"])["sig"] if info else ""
    _write(f"{role}.test.json", dict(result, at=time.time(), sig=sig))


def stop_all():
    return [r for r in ROLES if stop(r)]


def stop_idle():
    """Unload every giant this module started that is not answering right
    now — so an everyday model can have the memory. Returns the roles
    unloaded; a giant mid-answer is left alone."""
    out = []
    for role in ROLES:
        if not _pidrec(role):
            continue
        try:
            if stop(role, wait=0):
                out.append(role)
        except GarageBusy:
            continue
    return out


def reap(cfg):
    """Unload giants nobody has used for giants_idle_minutes (0 = never)."""
    idle = float(cfg.get("giants_idle_minutes") or 0)
    if idle <= 0:
        return []
    used = _read("used.json", {}) or {}
    out = []
    for role in ROLES:
        rec = _pidrec(role)
        if rec and time.time() - max(used.get(role, 0),
                                     rec.get("started", 0)) > idle * 60:
            try:
                if stop(role, wait=0):
                    out.append(role)
            except GarageBusy:
                continue                  # answering right now
    return out
