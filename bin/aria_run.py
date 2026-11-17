"""aria_run — running a project's software, two very different ways.

1. YOU run it (the Run tab, `aria run`). The command is shown, you press
   Run, it starts in the project's folder on this machine and its output
   streams back. Nothing but a click of yours — or your own terminal —
   ever calls `start()`: not autopilot, not chat, not a voice.

2. SHE checks it (autopilot's verify step, the Run tab's "Run the checks").
   The project's own tests run in a SANDBOX — bubblewrap: no network, the
   whole machine read-only, and a scratch copy of the project as the only
   thing that can be written — so code a model wrote can be executed
   without being trusted. A change that turns passing tests into failing
   ones is rejected and rolled back, with the failure as the critique.
   Without bubblewrap the checks are simply not run, and the report says
   so (aria.autopilot_checks: direct runs them unsandboxed — your call).

How to run and check a project is detected from what is in it (Python,
Node, Rust, Go, a Makefile, a static page) and can be overridden per
project in <records>/run.json — the Run tab saves your own commands there.

Stdlib only.
"""

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

OUTPUT_CAP = 400_000            # bytes of a run's output kept in memory
LOG_CAP = 4_000_000             # …and on disk
COPY_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "autopilot", ".aria", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", "target", "dist", "build", ".idea", ".next",
             ".cache", "crew"}
BIND_BACK = ("node_modules", ".venv", "venv")   # read-only into the copy
URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\])"
                 r"(?::\d+)?[^\s\"'<>)\]]*", re.I)

_RUNS = {}                      # slug -> _Run
_LOCK = threading.Lock()
_SANDBOX = {"at": 0.0, "tool": "", "why": ""}


# ------------------------------------------------------------ the sandbox

def sandbox():
    """(path to bubblewrap, "") when a sandbox can really be built here,
    else ("", why not). Tested once, by building one."""
    now = time.time()
    if _SANDBOX["at"] and now - _SANDBOX["at"] < 300:
        return _SANDBOX["tool"], _SANDBOX["why"]
    tool, why = shutil.which("bwrap") or "", ""
    if not tool:
        why = ("bubblewrap is not installed (Arch: sudo pacman -S "
               "bubblewrap · Debian/Ubuntu: sudo apt install bubblewrap)")
    else:
        try:
            done = subprocess.run(
                [tool, "--ro-bind", "/", "/", "--dev", "/dev", "--proc",
                 "/proc", "--unshare-all", "--die-with-parent", "true"],
                capture_output=True, text=True, timeout=20)
            if done.returncode != 0:
                tool, why = "", ("bubblewrap is installed but cannot build "
                                 "a sandbox here: "
                                 + (done.stderr.strip()[:200] or "it failed"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            tool, why = "", f"bubblewrap could not be run: {exc}"
    size = False
    if tool:                                   # --size came with 0.9
        try:
            size = "--size" in subprocess.run(
                [tool, "--help"], capture_output=True, text=True,
                timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            size = False
    _SANDBOX.update(at=now, tool=tool, why=why, size=size)
    return tool, why


def _wrap_user(tool, argv, workdir):
    """A program YOU start from the Run tab, kept from damaging anything:
    it can change files only inside the project folder (and a private
    /tmp). It can still read your files, reach the network and open a
    window — it is your software, being tried out; the point is that a bug
    cannot delete or overwrite anything outside its own folder."""
    return [tool, "--die-with-parent", "--new-session", "--unshare-all",
            "--share-net", "--ro-bind", "/", "/", "--dev", "/dev",
            "--proc", "/proc", "--tmpfs", "/tmp",
            "--ro-bind-try", "/tmp/.X11-unix", "/tmp/.X11-unix",
            "--bind", str(workdir), str(workdir),
            "--chdir", str(workdir)] + list(argv)


# Toolchains that live in a home directory and hold no secrets: the check
# sandbox hides the home, then shows these again (read-only).
TOOL_DIRS = (".pyenv", ".nvm", ".rustup", ".cargo/bin", ".cargo/registry",
             ".asdf", ".volta", ".bun", ".deno", "go/pkg", "go/bin",
             ".local/bin", ".local/lib", ".local/share/uv",
             ".local/share/mise", ".local/share/pnpm", ".npm-global",
             ".sdkman", ".gradle/wrapper", ".m2/repository")
HIDDEN = ("/home", "/root", "/mnt", "/media", "/srv", "/run", "/var/tmp")


def _wrap_check(tool, argv, workdir, extra_ro=()):
    """Code SHE wrote, run without being trusted. No network. The machine
    is read-only; home directories, other drives and /run (the desktop's
    sockets — D-Bus, the display, Docker — each a way out) are not there
    at all, except the toolchains needed to run anything. The scratch copy
    in `workdir` is the one place that can be written, and it is thrown
    away afterwards."""
    home = Path.home()
    cmd = [tool, "--die-with-parent", "--new-session", "--unshare-all",
           "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    sized = ["--size", str(1 << 30)] if _SANDBOX.get("size") else []
    for place in dict.fromkeys(HIDDEN + (str(home), "/tmp")):
        if os.path.isdir(place):
            cmd += sized + ["--tmpfs", place]
    for name in TOOL_DIRS:
        if (home / name).exists():
            cmd += ["--ro-bind", str(home / name), str(home / name)]
    cmd += ["--bind", str(workdir), str(workdir)]
    for src, dest in extra_ro:
        cmd += ["--ro-bind", str(src), str(dest)]
    cmd += ["--chdir", str(workdir)]
    return cmd + list(argv)


_OURS = re.compile(r"^(ARIA_|GH_TOKEN$|GITHUB_TOKEN$|GH_ENTERPRISE_TOKEN$)")
_PASS = re.compile(r"^(PATH|HOME|USER|LOGNAME|SHELL|LANG|LANGUAGE|LC_\w+|TZ|"
                   r"PYENV_ROOT|NVM_DIR|CARGO_HOME|RUSTUP_HOME|GOPATH|GOROOT|"
                   r"JAVA_HOME|VOLTA_HOME|BUN_INSTALL|DENO_DIR|SDKMAN_DIR)$")
_FLAGS = {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
          "FORCE_COLOR": "0", "NO_COLOR": "1", "TERM": "dumb",
          "PIP_DISABLE_PIP_VERSION_CHECK": "1"}


def _env_user():
    """Your environment, minus A.R.I.A.'s own keys and tokens — the
    program being tried out has no business with those."""
    env = {k: v for k, v in os.environ.items() if not _OURS.match(k)}
    env.update(_FLAGS)
    return env


def _env_check():
    """Next to nothing: where the tools are, and that this is a test run."""
    env = {k: v for k, v in os.environ.items() if _PASS.match(k)}
    env.update(_FLAGS, CI="1", XDG_CACHE_HOME="/tmp/cache",
               npm_config_cache="/tmp/npm", GOCACHE="/tmp/go-cache",
               CARGO_TARGET_DIR="/tmp/cargo", GOFLAGS="-mod=mod",
               PYTHONPYCACHEPREFIX="/tmp/pycache")
    return env


_LIMITER = {"at": 0.0, "argv": []}
_SCOPE_BROKE = re.compile(r"Failed to (?:start|connect|create|get)\b.*"
                          r"(?:transient|scope|bus|unit)", re.I)


def _limiter():
    """A cgroup around a test run, where systemd offers one to this user:
    a ceiling on memory and on the number of processes, so that a runaway
    test cannot take the machine down with it. [] where it does not work
    — tested by doing it."""
    now = time.time()
    if _LIMITER["at"] and now - _LIMITER["at"] < 600:
        return list(_LIMITER["argv"])
    argv = []
    tool = shutil.which("systemd-run")
    if tool and os.environ.get("XDG_RUNTIME_DIR"):
        for props in (("MemoryMax=6G", "MemorySwapMax=0", "TasksMax=512"),
                      ("MemoryMax=6G", "TasksMax=512")):
            trial = [tool, "--user", "--scope", "--quiet", "--collect"]
            for prop in props:
                trial += ["-p", prop]
            try:
                if subprocess.run(trial + ["--", "true"], capture_output=True,
                                  timeout=15).returncode == 0:
                    argv = trial + ["--"]
                    break
            except (OSError, subprocess.TimeoutExpired):
                break
    _LIMITER.update(at=now, argv=argv)
    return list(argv)


def _capture(argv, cwd, env, timeout):
    """Run to completion; (exit code or None on timeout, output). On a
    timeout the whole process group goes, not just its first process."""
    import resource

    def limits():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        big = 512 * 1024 * 1024               # no single file past 512 MB
        resource.setrlimit(resource.RLIMIT_FSIZE, (big, big))
        try:                                  # nor 8 GB of heap per process
            resource.setrlimit(resource.RLIMIT_DATA, (8 << 30, 8 << 30))
        except (ValueError, OSError):
            pass

    proc = subprocess.Popen(argv, cwd=str(cwd), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True,
                            preexec_fn=limits)
    try:
        data, _ = proc.communicate(timeout=timeout)
        return proc.returncode, data.decode("utf-8", "replace")[-200_000:]
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        data, _ = proc.communicate()
        return None, data.decode("utf-8", "replace")[-200_000:]


# ------------------------------------------------------------ what to run

def _read(path, limit=200_000):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _python(proj):
    """The interpreter a project's own virtualenv provides, else python3."""
    for name in (".venv", "venv"):
        exe = proj / name / "bin" / "python"
        if exe.exists():
            return str(exe)
    return "python3"


def _has_tests(proj):
    for root, dirs, files in os.walk(proj):
        dirs[:] = [d for d in dirs if d not in COPY_SKIP]
        if any(re.fullmatch(r"(test_.*|.*_test)\.py", f) for f in files):
            return True
    return False


def detect(proj):
    """How this project is started, checked and set up, from what is in
    it: {"run": [...], "check": [...], "setup": [...]}, each entry
    {"name", "cmd"} (run entries may carry "web": True)."""
    proj = Path(proj)
    run, check, setup = [], [], []
    has = lambda name: (proj / name).is_file()  # noqa: E731
    py = _python(proj)
    shown = "python3" if py == "python3" else \
        str(Path(py).relative_to(proj))

    # ---- node ------------------------------------------------------
    if has("package.json"):
        try:
            scripts = json.loads(_read(proj / "package.json")).get(
                "scripts") or {}
        except ValueError:
            scripts = {}
        if not (proj / "node_modules").is_dir():
            setup.append({"name": "Install dependencies",
                          "cmd": "npm install"})
        for key in ("dev", "start", "serve", "preview"):
            if key in scripts:
                run.append({"name": f"npm {key}", "web": True, "cmd":
                            "npm start" if key == "start"
                            else f"npm run {key}"})
        if "test" in scripts and "no test specified" not in scripts["test"]:
            check.append({"name": "npm test", "cmd": "npm test"})

    # ---- python ----------------------------------------------------
    pyfiles = [f for f in ("main.py", "app.py", "run.py", "cli.py",
                           "manage.py", "src/main.py", "src/app.py",
                           "src/cli.py", "src/__main__.py") if has(f)]
    for pkg in sorted(p.parent.name for p in proj.glob("*/__main__.py")
                      if p.parent.name not in COPY_SKIP):
        if pkg != "src":
            run.append({"name": f"Run {pkg}", "cmd": f"{shown} -m {pkg}"})
    for f in pyfiles:
        body = _read(proj / f, 20_000)
        mod = f[:-3].replace("/", ".")
        if f == "manage.py":
            run.append({"name": "Django dev server", "web": True,
                        "cmd": f"{shown} manage.py runserver"})
        elif "streamlit" in body:
            run.append({"name": "Streamlit app", "web": True,
                        "cmd": f"{shown} -m streamlit run {f}"})
        elif re.search(r"\bFastAPI\(", body):
            run.append({"name": "FastAPI server", "web": True,
                        "cmd": f"{shown} -m uvicorn {mod}:app --reload"})
        elif re.search(r"\bFlask\(", body):
            run.append({"name": "Flask server", "web": True,
                        "cmd": f"{shown} -m flask --app {mod} run"})
        elif f == "src/__main__.py":
            run.append({"name": "Run it", "cmd": f"{shown} -m src"})
        elif "/" in f:
            run.append({"name": f"Run {f}", "cmd": f"{shown} -m {mod}"})
        else:
            run.append({"name": f"Run {f}", "cmd": f"{shown} {f}"})
    if has("requirements.txt") and py == "python3":
        setup.append({"name": "Create a virtualenv and install requirements",
                      "cmd": "python3 -m venv .venv && .venv/bin/pip install "
                             "-r requirements.txt"})
    if _has_tests(proj):
        # pytest when the interpreter has it; else the built-in runner,
        # which covers plain test functions and unittest classes
        check.append({"name": "Python tests", "cmd":
                      f"{shown} -m pytest -q", "fallback":
                      f"{shown} {{minitest}}"})

    # ---- rust, go, make, shell, a plain page -----------------------
    if has("Cargo.toml"):
        run.append({"name": "cargo run", "cmd": "cargo run"})
        check.append({"name": "cargo test", "cmd": "cargo test --offline"})
    if has("go.mod"):
        run.append({"name": "go run", "cmd": "go run ."})
        check.append({"name": "go test", "cmd": "go test ./..."})
    if has("Makefile"):
        targets = set(re.findall(r"^([A-Za-z][\w-]*):", _read(proj /
                                                                "Makefile"),
                                 re.M))
        for target in ("run", "start", "dev"):
            if target in targets:
                run.append({"name": f"make {target}", "cmd": f"make {target}"})
        if "test" in targets and not check:
            check.append({"name": "make test", "cmd": "make test"})
    for script in ("run.sh", "start.sh"):
        if has(script):
            run.append({"name": script, "cmd": f"bash {script}"})
    if has("index.html") and not has("package.json"):
        run.append({"name": "Open the page (local web server)", "web": True,
                    "cmd": "python3 -m http.server {port}"})
    return {"run": run, "check": check, "setup": setup}


def profile(proj, records):
    """What the Run tab offers: the owner's own commands (run.json in her
    records) first, then what was detected."""
    found = detect(proj)
    saved = {}
    try:
        saved = json.loads((Path(records) / "run.json").read_text("utf-8"))
    except (OSError, ValueError):
        saved = {}
    for kind in ("run", "check", "setup"):
        mine = [dict(c, mine=True) for c in saved.get(kind, [])
                if isinstance(c, dict) and c.get("cmd")]
        have = {c["cmd"] for c in mine}
        found[kind] = mine + [c for c in found[kind] if c["cmd"] not in have]
    return found


def save_command(records, kind, name, cmd):
    """Remember one of the owner's own commands for this project."""
    if kind not in ("run", "check", "setup"):
        raise ValueError("kind must be run, check or setup")
    cmd = " ".join(str(cmd).split())
    if not cmd:
        raise ValueError("an empty command")
    path = Path(records) / "run.json"
    try:
        saved = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        saved = {}
    rows = [c for c in saved.get(kind, []) if c.get("cmd") != cmd]
    saved[kind] = [{"name": " ".join(str(name).split())[:60] or cmd[:40],
                    "cmd": cmd}] + rows[:9]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return saved


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------ you run it

class _Run:
    """One program started from the Run tab."""

    def __init__(self, slug, proj, records, cmd, sandboxed, tool):
        self.slug, self.cmd, self.sandboxed = slug, cmd, sandboxed
        self.started, self.exit, self.ended = time.time(), None, 0.0
        self.chunks, self.size, self.base = [], 0, 0
        self.urls = []
        self.lock = threading.Lock()
        self.drained = threading.Event()   # exited AND all output read
        shell = ["bash", "-lc", cmd]
        argv = _wrap_user(tool, shell, proj) if sandboxed else shell
        self.proc = subprocess.Popen(
            argv, cwd=str(proj), env=_env_user(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True)
        self.log = open(Path(records) / "run.log", "wb")
        self.log.write((f"$ {cmd}\n# in {proj}"
                        + (" — sandboxed: it can only write inside this "
                           "folder\n" if sandboxed else
                           " — not sandboxed\n")).encode())
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        fd = self.proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            with self.lock:
                self.chunks.append(data)
                self.size += len(data)
                while self.size > OUTPUT_CAP and len(self.chunks) > 1:
                    gone = self.chunks.pop(0)
                    self.size -= len(gone)
                    self.base += len(gone)
                if self.log.tell() < LOG_CAP:
                    self.log.write(data)
                    self.log.flush()
            for hit in URL.findall(data.decode("utf-8", "replace")):
                url = re.sub(r"//(0\.0\.0\.0|\[::\])", "//localhost",
                             hit.rstrip(".,;"))
                if url not in self.urls:
                    self.urls.append(url)
        self.exit = self.proc.wait()
        self.ended = time.time()
        with self.lock:
            self.log.write(f"\n# exited with code {self.exit}\n".encode())
            self.log.close()
        self.drained.set()

    def read(self, since):
        with self.lock:
            data = b"".join(self.chunks)
            start = max(0, since - self.base)
            return (data[start:].decode("utf-8", "replace"),
                    self.base + len(data), since < self.base)

    def alive(self):
        return self.proc.poll() is None

    def info(self):
        # "running" until the last of its output has been read, so that
        # whoever is watching never stops one read short
        over = self.drained.is_set()
        return {"running": not over, "cmd": self.cmd,
                "sandboxed": self.sandboxed, "started": self.started,
                "ended": self.ended, "exit": self.exit if over else None,
                "urls": list(self.urls[:6])}

    def stop(self):
        if self.alive():
            for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
                try:
                    os.killpg(self.proc.pid, sig)
                except OSError:
                    break
                try:
                    self.proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        self.drained.wait(5)


def start(slug, proj, records, cmd, sandboxed=True):
    """Start `cmd` in the project folder — only ever on the owner's own
    click or command. One program per project at a time."""
    cmd = str(cmd or "").strip()
    if not cmd:
        raise ValueError("no command to run")
    if len(cmd) > 2000 or "\x00" in cmd:
        raise ValueError("that is not a command")
    proj = Path(proj).resolve()
    if "{port}" in cmd:
        cmd = cmd.replace("{port}", str(free_port()))
    tool = ""
    if sandboxed:
        tool, why = sandbox()
        if not tool:
            raise ValueError("cannot sandbox it — " + why + ". Untick "
                             "“sandbox” to run it with your own permissions.")
    with _LOCK:
        old = _RUNS.get(slug)
        if old is not None and old.alive():
            raise ValueError("something is already running for this project "
                             f"({old.cmd}) — stop it first")
        _RUNS[slug] = _Run(slug, proj, records, cmd, sandboxed, tool)
        return _RUNS[slug].info()


def stop(slug):
    run = _RUNS.get(slug)
    if run is None:
        return {"running": False}
    run.stop()
    return run.info()


def status(slug):
    run = _RUNS.get(slug)
    return run.info() if run else {"running": False}


def output(slug, since=0):
    run = _RUNS.get(slug)
    if run is None:
        return {"text": "", "next": 0, "running": False}
    text, nxt, dropped = run.read(int(since or 0))
    return {"text": text, "next": nxt, "dropped": dropped, **run.info()}


def send(slug, text):
    """A line typed for the running program."""
    run = _RUNS.get(slug)
    if run is None or not run.alive():
        raise ValueError("nothing is running for this project")
    try:
        run.proc.stdin.write((str(text) + "\n").encode())
        run.proc.stdin.flush()
    except OSError as exc:
        raise ValueError(f"it is not taking input: {exc}")
    return run.info()


def stop_all():
    for run in list(_RUNS.values()):
        run.stop()


# ------------------------------------------------------------ she checks it

def _copy(proj, dest):
    """A scratch copy of the project's own files — what the checks may
    scribble on instead of the real thing."""
    def skip(folder, names):
        return [n for n in names if n in COPY_SKIP
                or (Path(folder) / n).is_symlink()
                and not (Path(folder) / n).exists()]
    shutil.copytree(proj, dest, ignore=skip, symlinks=True)


def _tail(text, limit=2400):
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _summary(text):
    lines = [ln.strip(" =") for ln in text.strip().splitlines() if ln.strip()]
    for ln in reversed(lines[-12:]):
        if re.search(r"\b(passed|failed|errors?|ok|FAILED|OK|tests?)\b", ln):
            return ln[:160]
    return lines[-1][:160] if lines else ""


def _nothing_to_run(cmd, code, text):
    """Did the check command find no tests / lack its tool? Then it has
    told us nothing about the project."""
    low = text.lower()
    if "pytest" in cmd and (code == 5 or "no module named pytest" in low):
        return "pytest is not installed" if code != 5 else "no tests found"
    if "minitest" in cmd and code == 5:
        return ("no test could run without pytest (install it: pacman -S "
                "python-pytest, or pip install pytest)"
                if "needs pytest" in low else "no tests found")
    if "unittest" in cmd and re.search(r"\bran 0 tests\b", low):
        return "no tests found"
    if code == 127 or "command not found" in low:
        return "its tool is not installed here"
    if "cargo" in cmd and ("no matching package" in low
                           or "failed to download" in low
                           or "unable to update" in low):
        return "its dependencies are not available offline"
    if "npm" in cmd and "missing script" in low:
        return "no test script"
    return ""


def check(proj, records, mode="sandbox", timeout=300, commands=None):
    """Run the project's own checks, safely. Returns
    {"ran": bool, "ok": bool|None, "cmd", "summary", "output", "seconds",
     "why", "sandboxed"} — `ran` False means the result says nothing
    (no tests, no sandbox, tool missing), and `why` says which."""
    proj = Path(proj).resolve()
    out = {"ran": False, "ok": None, "cmd": "", "summary": "", "output": "",
           "seconds": 0, "why": "", "sandboxed": False}
    if mode == "off":
        out["why"] = "running checks is switched off (aria.autopilot_checks)"
        return out
    cmds = commands if commands is not None else \
        profile(proj, records)["check"]
    if not cmds:
        out["why"] = "the project has no tests to run yet"
        return out
    tool, why = sandbox()
    if mode != "direct" and not tool:
        out["why"] = "not sandboxed here — " + why
        return out
    scratch = Path(tempfile.mkdtemp(prefix="aria-check-"))
    work = scratch / proj.name
    t0 = time.time()
    try:
        _copy(proj, work)
        binds = [(proj / name, work / name) for name in BIND_BACK
                 if (proj / name).is_dir()]
        if not tool:                           # direct: link them in
            for src, dest in binds:
                os.symlink(src, dest)
        entry = cmds[0]
        # the built-in test runner, visible inside the sandbox
        runner = Path(__file__).resolve().with_name("aria-minitest")
        inside = "/tmp/aria-minitest.py" if tool else str(runner)
        if tool:
            binds.append((runner, inside))
        for cmd in [entry["cmd"]] + ([entry["fallback"]]
                                     if entry.get("fallback") else []):
            cmd = cmd.replace("{minitest}", inside)
            shell = ["bash", "-c", cmd.replace("{port}", str(free_port()))]
            argv = _wrap_check(tool, shell, work, extra_ro=binds) \
                if tool else shell
            scope = _limiter()
            code, text = _capture(scope + argv, work, _env_check(), timeout)
            if scope and code not in (0, None) and _SCOPE_BROKE.search(text):
                # the cgroup could not be made after all: that says
                # nothing about the project — run it without one
                _LIMITER.update(at=time.time(), argv=[])
                code, text = _capture(argv, work, _env_check(), timeout)
            out.update(cmd=cmd.replace(inside, "aria-minitest"),
                       output=_tail(text), sandboxed=bool(tool),
                       seconds=round(time.time() - t0, 1))
            if code is None:
                out.update(ran=True, ok=False, why="", output=_tail(
                    text + f"\n(stopped: still running after {timeout}s)"),
                    summary=f"timed out after {timeout}s")
                break
            nothing = _nothing_to_run(cmd, code, text)
            if nothing:
                out["why"] = nothing
                continue                       # try the fallback, if any
            out.update(ran=True, ok=code == 0, why="",
                       summary=_summary(text))
            break
    except (OSError, shutil.Error) as exc:
        out["why"] = f"could not prepare a scratch copy: {exc}"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return out
