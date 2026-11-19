"""aria_voice — her voice and her ears, on this machine.

A browser can speak and listen only when it has a speech service behind it
(Chrome borrows Google's). A.R.I.A.'s own window has none, so there she was
mute and deaf. This module gives her both locally, with nothing leaving the
machine:

  speaking    piper (a neural voice that sounds like a person), through a
              worker process that keeps the voice loaded — a sentence takes
              a fraction of a second. Without it: espeak-ng, robotic but
              installed nearly everywhere.
  listening   faster-whisper, through the same worker.

`aria voice install` sets both up in a private Python environment
(vendor/voice/venv — nothing is installed system-wide) and downloads one
voice; the recogniser's model lands beside them (vendor/voice/models) the
first time she listens. The engine then finds them by itself; the app asks
/api/voice what there is and falls back to the browser's own speech where
that exists.
"""

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICE_DIR = ROOT / "vendor" / "voice"
VENV = VOICE_DIR / "venv"
VOICES = VOICE_DIR / "voices"
LOG = ROOT / "system" / "voice.log"
PACKAGES = ("piper-tts", "faster-whisper")
DEFAULT_VOICE = "en_GB-cori-medium"
# a few voices worth trying first (the full list: `aria voice voices`)
SUGGESTED = (("en_GB-cori-medium", "British, female"),
             ("en_GB-jenny_dioco-medium", "British, female, softer"),
             ("en_GB-alba-medium", "Scottish, female"),
             ("en_GB-alan-medium", "British, male"),
             ("en_GB-northern_english_male-medium", "Northern English, male"),
             ("en_US-amy-medium", "American, female"),
             ("en_US-lessac-medium", "American, female"),
             ("en_US-ryan-medium", "American, male"))
SETUP = "bin/aria voice install"


class VoiceError(RuntimeError):
    """Speech is not possible right now; the message says what to do."""


# ------------------------------------------------------------ the worker

_LOCK = threading.RLock()
_PROC = None
_HAVE = None                    # the worker's hello: {"tts": bool, "stt": bool}


def python():
    """The Python that has (or would have) the speech packages."""
    exe = VENV / "bin" / "python"
    return str(exe) if exe.is_file() else sys.executable


def _worker():
    return os.environ.get("ARIA_VOICE_WORKER") \
        or str(ROOT / "bin" / "aria-voice-worker")


def _stop():
    global _PROC, _HAVE
    with _LOCK:
        proc, _PROC, _HAVE = _PROC, None, None
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


atexit.register(_stop)


def reset():
    """Forget the worker — after an install, a new voice, a new Python."""
    _stop()


def _start():
    global _PROC
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(LOG, "wb")
    except OSError:
        log = subprocess.DEVNULL
    _PROC = subprocess.Popen([python(), _worker()], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=log)
    return _PROC


def _call(header, payload=b"", timeout=120):
    """One request to the worker, which is started (or restarted, once) as
    needed. Returns (header, payload). A worker that hangs past `timeout`
    is killed."""
    with _LOCK:
        for attempt in (1, 2):
            proc = _PROC if _PROC is not None and _PROC.poll() is None \
                else None
            try:
                proc = proc or _start()
            except OSError as exc:
                raise VoiceError(f"the voice worker could not start: {exc}")
            watchdog = threading.Timer(timeout, proc.kill)
            watchdog.start()
            try:
                head = dict(header)
                if payload:
                    head["bytes"] = len(payload)
                proc.stdin.write(json.dumps(head).encode() + b"\n" + payload)
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    raise EOFError("no answer")
                answer = json.loads(line)
                size = int(answer.get("bytes") or 0)
                data = proc.stdout.read(size) if size else b""
                if len(data) != size:
                    raise EOFError("short answer")
                return answer, data
            except (OSError, ValueError, EOFError):
                _stop()
                if attempt == 2:
                    raise VoiceError("the voice worker stopped answering — "
                                     "its last words are in system/voice.log")
            finally:
                watchdog.cancel()
    raise VoiceError("the voice worker is unavailable")


def have():
    """What the worker's Python can do: {"tts": bool, "stt": bool}."""
    global _HAVE
    with _LOCK:
        if _HAVE is None:
            try:
                answer, _ = _call({"op": "hello"}, timeout=30)
                got = {"tts": bool(answer.get("tts")),
                       "stt": bool(answer.get("stt"))}
            except VoiceError:
                got = {"tts": False, "stt": False}
            _HAVE = got                 # _call's own failure path clears it
        return dict(_HAVE)


# ------------------------------------------------------------ speaking

def voices():
    """Installed piper voices, as [{name, path}] — a voice is name.onnx
    with name.onnx.json beside it."""
    out, seen = [], set()
    for folder in (VOICES, Path.home() / ".local" / "share" / "piper-voices",
                   Path("/usr/share/piper-voices")):
        if not folder.is_dir():
            continue
        for f in sorted(folder.rglob("*.onnx")):
            if f.stem not in seen and f.with_name(f.name + ".json").is_file():
                seen.add(f.stem)
                out.append({"name": f.stem, "path": str(f)})
    return out


def _voice(cfg):
    want = str(cfg.get("voice_name") or "").strip()
    have_voices = voices()
    if want:
        direct = Path(os.path.expanduser(want))
        if direct.suffix == ".onnx" and direct.is_file():
            return {"name": direct.stem, "path": str(direct)}
        for v in have_voices:
            if v["name"] == want:
                return v
    for v in have_voices:
        if v["name"] == DEFAULT_VOICE:
            return v
    return have_voices[0] if have_voices else None


def _espeak():
    return shutil.which("espeak-ng") or shutil.which("espeak") or ""


def tts_engine(cfg):
    """What she would speak with: {"engine": "piper" | "espeak" | "",
    "voice", "natural", "why"}."""
    if cfg.get("voice_engine") == "browser":
        return {"engine": "", "voice": "", "natural": False,
                "why": "set to use the browser's own voices"}
    voice = _voice(cfg)
    if voice and have()["tts"]:
        return {"engine": "piper", "voice": voice["name"],
                "path": voice["path"], "natural": True, "why": ""}
    if _espeak():
        lang = "en-us" if str(cfg.get("voice_name") or "").startswith(
            "en_US") else "en-gb"
        return {"engine": "espeak", "voice": lang, "natural": False,
                "why": f"a plain synthetic voice — for a natural one: {SETUP}"}
    return {"engine": "", "voice": "", "natural": False,
            "why": f"no speech engine on this machine — run: {SETUP}"}


def _fix_wav(data):
    """A WAV written to a pipe carries placeholder sizes in its header;
    put the real ones in, so every player accepts it."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data
    at = data.find(b"data", 12)
    if at < 0:
        return data
    body = len(data) - (at + 8)
    return (b"RIFF" + (len(data) - 8).to_bytes(4, "little") + data[8:at + 4]
            + body.to_bytes(4, "little") + data[at + 8:])


def _her_name(m):
    """"A.R.I.A." the way it is said: a word, not four letters — keeping
    the full stop when it was also the end of a sentence."""
    after = m.string[m.end():]
    closes = m.group(0).endswith(".") and (
        not after.strip() or (after[:1].isspace()
                              and after.lstrip()[:1].isupper()))
    return "Aria." if closes else "Aria"


def speak(text, cfg):
    """`text` as a WAV file (bytes)."""
    text = re.sub(r"A\.R\.I\.A\.?", _her_name, str(text or ""))
    text = " ".join(text.split())[:1500]
    if not text:
        raise VoiceError("nothing to say")
    engine = tts_engine(cfg)
    speed = min(2.0, max(0.5, float(cfg.get("voice_speed") or 1.0)))
    if engine["engine"] == "piper":
        answer, data = _call({"op": "tts", "text": text,
                              "voice": engine["path"], "speed": speed},
                             timeout=180)
        if not answer.get("ok") or not data:
            raise VoiceError(answer.get("error") or "the voice gave no sound")
        return data
    if engine["engine"] == "espeak":
        try:
            done = subprocess.run(
                [_espeak(), "-v", engine["voice"], "-s", str(int(165 * speed)),
                 "--stdout", "--stdin"], input=text.encode("utf-8"),
                capture_output=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceError(f"espeak did not answer: {exc}")
        if done.returncode or done.stdout[:4] != b"RIFF":
            raise VoiceError("espeak gave no sound: "
                             + done.stderr.decode("utf-8", "replace")[:200])
        return _fix_wav(done.stdout)
    raise VoiceError(engine["why"])


# ------------------------------------------------------------ listening

def stt_engine(cfg):
    """What she would listen with: {"engine": "faster-whisper" | "",
    "model", "why"}."""
    if have()["stt"]:
        return {"engine": "faster-whisper",
                "model": str(cfg.get("stt_model") or "base"), "why": ""}
    return {"engine": "", "model": "",
            "why": f"no local speech recogniser on this machine — run: {SETUP}"}


def _models_dir():
    """Where the recogniser keeps its model: beside the private environment
    when that is what runs, so everything `aria voice install` brought sits
    under vendor/voice. '' — the library's own cache — when the speech
    packages are the system's."""
    return str(VENV.parent / "models") \
        if (VENV / "bin" / "python").is_file() else ""


def transcribe(data, cfg, suffix=".wav", language=""):
    """What was said in an audio clip (WAV is best: see the worker)."""
    engine = stt_engine(cfg)
    if not engine["engine"]:
        raise VoiceError(engine["why"])
    if not data:
        return ""
    # the first use downloads the model: allow for a slow line
    answer, _ = _call({"op": "stt", "model": engine["model"],
                       "language": language, "suffix": suffix,
                       "models": _models_dir()}, data, timeout=1800)
    if not answer.get("ok"):
        raise VoiceError(answer.get("error") or "transcription failed")
    return " ".join(str(answer.get("text") or "").split())


# ------------------------------------------------------------ setting up

INSTALL = {"running": False, "step": "", "error": "", "done": 0.0}


def _run(argv, what):
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VoiceError(f"{what} failed: {exc}")
    if done.returncode:
        tail = "\n".join((done.stderr or done.stdout).strip()
                         .splitlines()[-6:])
        raise VoiceError(f"{what} failed:\n{tail}")


def install(cfg, say=print, voice=""):
    """Set up local speech: a private Python environment with piper-tts and
    faster-whisper, one voice, and a round trip to prove it — she says a
    sentence and has to hear it back. Returns the status."""
    if INSTALL["running"]:
        raise VoiceError("an install is already running")
    INSTALL.update(running=True, error="", step="starting")

    def step(text):
        INSTALL["step"] = text
        say(text)

    try:
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        venv_python = VENV / "bin" / "python"
        if not venv_python.is_file():
            step("creating a private Python environment in vendor/voice/venv")
            _run([sys.executable, "-m", "venv", str(VENV)],
                 "creating the environment (is python's venv module "
                 "installed?)")
        step("installing piper-tts and faster-whisper there — about 500 MB, "
             "a few minutes")
        _run([str(venv_python), "-m", "pip", "install", "--upgrade",
              "--disable-pip-version-check", "--quiet", *PACKAGES],
             "installing the speech packages")
        voice = (voice or str(cfg.get("voice_name") or "")
                 or DEFAULT_VOICE).strip()
        if not any(v["name"] == voice for v in voices()):
            step(f"downloading the voice {voice} — about 65 MB")
            VOICES.mkdir(parents=True, exist_ok=True)
            _run([str(venv_python), "-m", "piper.download_voices", voice,
                  "--data-dir", str(VOICES)], f"downloading {voice}")
        reset()
        step("trying it: she says a sentence and listens to herself (the "
             "recogniser downloads its model the first time, about 150 MB)")
        local = dict(cfg, voice_engine="auto", voice_name=voice)
        said = "Voice check. A.R.I.A. is online and ready."
        heard = transcribe(speak(said, local), local, language="en")
        if not heard:
            raise VoiceError("she spoke, but heard nothing back")
        step(f"she said “{said}” and heard “{heard}”")
        INSTALL.update(done=time.time())
        return status(cfg)
    except VoiceError as exc:
        INSTALL["error"] = str(exc)
        raise
    finally:
        INSTALL["running"] = False


def add_voice(name, say=print):
    """Download one more piper voice into vendor/voice/voices."""
    venv_python = VENV / "bin" / "python"
    if not venv_python.is_file():
        raise VoiceError(f"local speech is not set up yet — run: {SETUP}")
    name = name.strip()
    if not name or not all(c.isalnum() or c in "_-" for c in name):
        raise VoiceError("a voice is named like en_GB-cori-medium")
    say(f"downloading the voice {name} — about 65 MB")
    VOICES.mkdir(parents=True, exist_ok=True)
    _run([str(venv_python), "-m", "piper.download_voices", name,
          "--data-dir", str(VOICES)], f"downloading {name}")
    if not any(v["name"] == name for v in voices()):
        raise VoiceError(f"no voice called {name} was found to download")
    reset()


def status(cfg):
    """Everything the app and `aria voice` show."""
    return {"tts": tts_engine(cfg), "stt": stt_engine(cfg),
            "voices": [v["name"] for v in voices()],
            "suggested": [{"name": n, "says": s} for n, s in SUGGESTED],
            "set_up": (VENV / "bin" / "python").is_file(),
            "install": dict(INSTALL),
            "prefs": {"engine": cfg.get("voice_engine", "auto"),
                      "input": cfg.get("voice_input", "auto"),
                      "name": cfg.get("voice_name", ""),
                      "speed": cfg.get("voice_speed", 1.0)}}
