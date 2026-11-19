"""A.R.I.A.'s hands on git and GitHub.

Stdlib only, no engine imports. The rules this module keeps:

  * it only works in a directory that is its OWN repository root — never a
    parent repository that merely contains the folder
  * work is committed to a work branch (aria/autopilot); the base branch is
    never committed to, pushed to, or rewritten
  * nothing is forced: no --force, no reset --hard, no clean
  * only the paths she is handed get committed — your own uncommitted edits
    in the same tree are left alone
  * GitHub is reached through the `gh` CLI you authenticated (gh auth
    login), or a token in ARIA_GITHUB_TOKEN — she stores no credentials
"""

import os
import shutil
import subprocess
from pathlib import Path

WORK_BRANCH = "aria/autopilot"
IGNORE_LINES = ("autopilot/state.json", "autopilot/STOP", "autopilot/LOCK",
                "autopilot/activity.jsonl", ".env", "__pycache__/",
                "node_modules/",
                # signals, and what belongs to this machine rather than to
                # the project's history
                "autopilot/STEER", "autopilot/REFINE",
                "autopilot/decision-*.json", "autopilot/run.log",
                "autopilot/run.json", "autopilot/activity-archive.jsonl",
                "autopilot/*.tmp",
                # what she keeps so the app can show the past: git has the
                # files' history already, and live output is not history
                "autopilot/versions/", "autopilot/live/")


class GitError(RuntimeError):
    pass


def _env():
    env = os.environ.copy()
    token = env.get("ARIA_GITHUB_TOKEN")
    if token and not env.get("GH_TOKEN"):
        env["GH_TOKEN"] = token
    env["GIT_TERMINAL_PROMPT"] = "0"       # never hang waiting for a password
    return env


def run(args, cwd, timeout=120, check=True):
    """Run git; returns stdout. Raises GitError with git's own message."""
    try:
        res = subprocess.run(["git", *args], cwd=str(cwd), text=True,
                             capture_output=True, timeout=timeout, env=_env())
    except FileNotFoundError:
        raise GitError("git is not installed")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out after {timeout}s")
    if check and res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        raise GitError(f"git {args[0]}: {msg[-1] if msg else 'failed'}")
    return res.stdout.strip()


def _ok(args, cwd):
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), text=True,
                              capture_output=True, timeout=60,
                              env=_env()).returncode == 0
    except Exception:
        return False


def is_repo_root(path):
    return (Path(path) / ".git").exists()


def enclosing_repo(path):
    """The repository root that CONTAINS path (when path is not itself a
    root), or None."""
    path = Path(path).resolve()
    for parent in path.parents:
        if (parent / ".git").exists():
            return parent
    return None


def _identity(cwd):
    if run(["config", "user.email"], cwd, check=False):
        return []
    return ["-c", "user.name=A.R.I.A.", "-c", "user.email=aria@localhost"]


def has_commits(cwd):
    return _ok(["rev-parse", "--verify", "-q", "HEAD"], cwd)


def current_branch(cwd):
    return run(["symbolic-ref", "--short", "-q", "HEAD"], cwd, check=False)


def branch_exists(cwd, name):
    return _ok(["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], cwd)


def remote_url(cwd, name="origin"):
    return run(["remote", "get-url", name], cwd, check=False)


def _ensure_ignore(cwd):
    path = Path(cwd) / ".gitignore"
    have = path.read_text(encoding="utf-8").splitlines() \
        if path.exists() else []
    missing = [ln for ln in IGNORE_LINES if ln not in have]
    if missing:
        with path.open("a", encoding="utf-8") as fh:
            if have and have[-1].strip():
                fh.write("\n")
            fh.write("\n".join(missing) + "\n")


def init(path, message="Initial commit by A.R.I.A."):
    """Make path its own repository (if it is not one) with a first commit
    of what is there. Explicit, owner-requested action."""
    path = Path(path)
    if not is_repo_root(path):
        run(["init", "-q"], path)
    _ensure_ignore(path)
    if not has_commits(path):
        run(["add", "-A"], path)
        run([*_identity(path), "commit", "-q", "--allow-empty", "-m", message],
            path)
    return current_branch(path)


def start_work_branch(cwd, base=""):
    """Check out the work branch, creating it from where the repo stands.
    Returns (work_branch, base_branch). Raises GitError when the tree
    cannot be switched without losing something (nothing is forced)."""
    if not is_repo_root(cwd):
        raise GitError("not a repository root")
    _ensure_ignore(cwd)
    if not has_commits(cwd):
        run(["add", "-A"], cwd)
        run([*_identity(cwd), "commit", "-q", "--allow-empty", "-m",
             "Initial commit by A.R.I.A."], cwd)
    here = current_branch(cwd)
    if here and here != WORK_BRANCH:
        base = here
    if here == WORK_BRANCH:
        return WORK_BRANCH, base
    if branch_exists(cwd, WORK_BRANCH):
        run(["checkout", "-q", WORK_BRANCH], cwd)
        if base and _ok(["merge-base", "--is-ancestor", WORK_BRANCH, base],
                        cwd):
            # everything on the work branch already landed in base —
            # catch up instead of diverging from it
            run(["merge", "-q", "--ff-only", base], cwd, check=False)
    else:
        run(["checkout", "-q", "-b", WORK_BRANCH], cwd)
    return WORK_BRANCH, base


def commit(cwd, paths, message):
    """Commit exactly these paths (relative to cwd). Returns the short
    hash, or None when they hold nothing new."""
    root = Path(cwd)
    paths = [str(p) for p in paths if (root / p).exists()]
    if not paths:
        return None
    run(["add", "--", *paths], cwd)
    if _ok(["diff", "--cached", "--quiet", "--", *paths], cwd):
        return None
    run([*_identity(cwd), "commit", "-q", "-m", message, "--", *paths], cwd)
    return run(["rev-parse", "--short", "HEAD"], cwd)


def gh():
    return shutil.which("gh")


def _gh(args, cwd, timeout=120):
    exe = gh()
    if not exe:
        raise GitError("the gh CLI is not installed — https://cli.github.com "
                       "then: gh auth login")
    try:
        res = subprocess.run([exe, *args], cwd=str(cwd), text=True,
                             capture_output=True, timeout=timeout, env=_env())
    except subprocess.TimeoutExpired:
        raise GitError(f"gh {args[0]} timed out")
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        raise GitError(f"gh {' '.join(args[:2])}: "
                       f"{msg[-1] if msg else 'failed'}")
    return res.stdout.strip()


def push(cwd, branch=WORK_BRANCH):
    """Push the work branch to origin. Never forced; refuses the base."""
    if not remote_url(cwd):
        raise GitError("no 'origin' remote — publish the project first")
    args = []
    if gh() and remote_url(cwd).startswith("http"):
        # use the owner's gh login for this one command, without touching
        # their git configuration
        args = ["-c", "credential.helper=",
                "-c", "credential.helper=!gh auth git-credential"]
    run([*args, "push", "-q", "-u", "origin", branch], cwd, timeout=300)


def pr_url(cwd, branch=WORK_BRANCH):
    """URL of the open pull request for branch, or ''."""
    try:
        out = _gh(["pr", "view", branch, "--json", "url,state",
                   "-q", 'select(.state=="OPEN") | .url'], cwd)
    except GitError:
        return ""
    return out.strip()


def pr_open(cwd, base, title, body, branch=WORK_BRANCH):
    """Open a pull request work-branch -> base (or return the open one)."""
    existing = pr_url(cwd, branch)
    if existing:
        return existing
    if not base or base == branch:
        raise GitError("no base branch to open a pull request against")
    return _gh(["pr", "create", "--base", base, "--head", branch,
                "--title", title, "--body", body], cwd).splitlines()[-1]


def pr_update(cwd, body, branch=WORK_BRANCH):
    _gh(["pr", "edit", branch, "--body", body], cwd)


def publish(cwd, name, private=True):
    """Create a GitHub repository for this project and push it. Explicit,
    owner-requested action; private unless told otherwise."""
    if remote_url(cwd):
        raise GitError(f"already has a remote: {remote_url(cwd)}")
    init(cwd)
    _gh(["repo", "create", name, "--private" if private else "--public",
         "--source", ".", "--remote", "origin", "--push"], cwd, timeout=300)
    return remote_url(cwd)


def clone(url, dest):
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise GitError(f"{dest} already exists and is not empty")
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = []
    if gh() and url.startswith("http"):
        args = ["-c", "credential.helper=",
                "-c", "credential.helper=!gh auth git-credential"]
    run([*args, "clone", "-q", url, str(dest)], dest.parent, timeout=900)
    return dest


def summary(cwd):
    """What the UI shows about a project's repository."""
    if not is_repo_root(cwd):
        parent = enclosing_repo(cwd)
        return {"repo": False,
                "inside": str(parent) if parent else ""}
    last = run(["log", "-1", "--format=%h %s"], cwd, check=False)
    dirty = run(["status", "--porcelain"], cwd, check=False)
    return {"repo": True, "branch": current_branch(cwd),
            "remote": remote_url(cwd), "last": last,
            "dirty": len([ln for ln in dirty.splitlines() if ln.strip()])}
