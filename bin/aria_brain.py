"""A.R.I.A.'s Obsidian brain — everything she writes into your vault.

Stdlib only, no engine imports. Every write is confined to <vault>/ARIA/:
she adds to your second brain and never touches your own notes.

Layout
  ARIA/<slug>.md               project hub note (links everything below)
  ARIA/<slug>/<slug>-<doc>.md  project documents (idea, plan, changelog…)
  ARIA/Notes/…                 things you told her to remember
  ARIA/Research/…              web research, with its sources
  ARIA/Journal/YYYY-MM-DD.md   what she did each day
"""

import datetime
import os
import re
import threading
from pathlib import Path

ROOT_FOLDER = "ARIA"
_LOCK = threading.Lock()


class BrainError(ValueError):
    """The vault is not usable; callers treat brain writes as best-effort."""


def vault_dir(path):
    """The configured vault as a Path, or BrainError saying what to fix."""
    if not path:
        raise BrainError("no Obsidian vault set — add its path in Settings "
                         "(or: aria settings set obsidian_vault <folder>)")
    vault = Path(os.path.expanduser(str(path)))
    if not vault.is_dir():
        raise BrainError(f"Obsidian vault not found: {vault}")
    return vault


def safe_name(name, limit=80):
    """A single file-name component that Obsidian and every filesystem
    accept, and that cannot escape its folder."""
    name = re.sub(r'[\\/:*?"<>|#^\[\]\x00-\x1f]', " ", str(name))
    name = " ".join(name.split()).strip(". ")
    return name[:limit].rstrip(". ") or "untitled"


def _target(vault, *parts):
    base = (vault / ROOT_FOLDER).resolve()
    dest = base.joinpath(*[safe_name(p) for p in parts[:-1]],
                         safe_name(parts[-1]) + ".md").resolve()
    if base != dest and base not in dest.parents:
        raise BrainError(f"refused a path outside {ROOT_FOLDER}/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _yaml_value(value):
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    text = str(value)
    if re.search(r'[:#\[\]{}"\n]|^\s|\s$', text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') \
                         .replace("\n", " ") + '"'
    return text


def frontmatter(props):
    return "---\n" + "".join(f"{k}: {_yaml_value(v)}\n"
                             for k, v in props.items()) + "---\n\n"


def _unique(path):
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem} {n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def write(vault, parts, body, props=None, overwrite=True):
    """Write one note under ARIA/. `parts` is the path below ARIA/ without
    the .md extension, e.g. ("Notes", "2026-11-16 bird feeder")."""
    dest = _target(vault, *parts)
    with _LOCK:
        if not overwrite:
            dest = _unique(dest)
        dest.write_text((frontmatter(props) if props else "")
                        + body.rstrip() + "\n", encoding="utf-8")
    return dest


def note(vault, text, title=None, source="chat", tags=("aria", "note")):
    """Something the owner asked her to remember."""
    text = text.strip()
    if not text:
        raise BrainError("nothing to write")
    now = datetime.datetime.now()
    title = title or " ".join(text.split()[:8])
    return write(
        vault, ("Notes", f"{now:%Y-%m-%d} {title}"),
        f"# {safe_name(title)}\n\n{text}\n",
        {"tags": list(tags), "created": f"{now:%Y-%m-%dT%H:%M}",
         "source": f"aria-{source}"},
        overwrite=False)


def research(vault, question, answer, sources):
    """A web-research answer worth keeping, with where it came from."""
    now = datetime.datetime.now()
    lines = [f"# {safe_name(question, 120)}", "", answer.strip(), "",
             "## Sources", ""]
    lines += [f"- [{s.get('title') or s['url']}]({s['url']})"
              for s in sources if s.get("url")] or ["- (none recorded)"]
    return write(
        vault, ("Research", f"{now:%Y-%m-%d} {question}"),
        "\n".join(lines) + "\n",
        {"tags": ["aria", "research"], "created": f"{now:%Y-%m-%dT%H:%M}",
         "source": "aria-web"},
        overwrite=False)


def journal(vault, line, when=None):
    """Append one line to today's journal note."""
    when = when or datetime.datetime.now()
    dest = _target(vault, "Journal", f"{when:%Y-%m-%d}")
    with _LOCK:
        if not dest.exists():
            dest.write_text(
                frontmatter({"tags": ["aria", "journal"],
                             "date": f"{when:%Y-%m-%d}"})
                + f"# A.R.I.A. journal — {when:%A %d %B %Y}\n\n",
                encoding="utf-8")
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(f"- {when:%H:%M} — {' '.join(line.split())}\n")
    return dest


def sync_project(vault, slug, docs, title=None, pitch="", status="",
                 source=""):
    """Mirror a project into the vault: one note per document plus a hub
    note that links them. `docs` is an ordered list of
    (name, text, authored_by_aria). Returns the hub note's path."""
    slug = safe_name(slug)
    today = datetime.date.today().isoformat()
    links = []
    for name, text, authored in docs:
        if not text or not text.strip():
            continue
        label = name.replace("-", " ").title()
        body = text
        if authored and not text.lstrip().startswith("---"):
            body = (frontmatter({"tags": ["aria", slug], "aria-slug": slug,
                                 "updated": today})
                    + text.rstrip() + f"\n\n↑ [[{slug}]]\n")
        dest = _target(vault, slug, f"{slug}-{name}")
        with _LOCK:
            dest.write_text(body if body.endswith("\n") else body + "\n",
                            encoding="utf-8")
        links.append(f"- [[{slug}-{name}|{label}]]")
    props = {"tags": ["aria", "project"], "aria-slug": slug,
             "exported": today}
    if status:
        props["status"] = status
    hub = (f"# {title or slug.replace('-', ' ').title()}\n\n"
           + (f"> {pitch}\n\n" if pitch else "")
           + (f"**Status:** {status}\n\n" if status else "")
           + "\n".join(links)
           + (f"\n\nSource: A.R.I.A. — `{source}`\n" if source else "\n"))
    return write(vault, (slug,), hub, props)
