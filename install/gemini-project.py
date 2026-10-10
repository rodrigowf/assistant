#!/usr/bin/env python3
"""Register Archie's repo with Gemini CLI so its project dir is context/.

Gemini CLI (0.63) keeps everything per project under
``~/.gemini/tmp/<label>/``: the session JSONLs (``chats/``) and its private
project memory, whose index ``memory/MEMORY.md`` it loads into every session.
The installers link ``~/.gemini/tmp/<label>`` to ``<repo>/context``, so
sessions land in ``context/chats/`` (history) and the memory index is
``context/memory/MEMORY.md`` (the wiki).  That only holds while Gemini keeps
choosing the same label for the repo:

* ``~/.gemini/projects.json`` maps the repo path to the label.  An
  unregistered repo gets a new label on its first run (the base name, or
  ``<base>-1`` … when that is taken).
* Each label directory carries an ownership marker ``.project_root``
  (``tmp/<label>/.project_root`` — i.e. ``context/.project_root`` through the
  link — and ``history/<label>/.project_root``).  A marker naming another
  path makes Gemini drop the mapping and claim a new label, so sessions and
  memory would silently leave ``context/``.

Usage:
  gemini-project.py REPO label   [--gemini-dir DIR]
      Print the label to link (the registered one, else the one Gemini
      would claim on its first run).
  gemini-project.py REPO check   [--gemini-dir DIR] [--label LABEL]
      Print key=value status lines (never changes anything).
  gemini-project.py REPO apply   [--gemini-dir DIR] [--label LABEL]
      Register REPO -> LABEL in projects.json if missing and fix the
      context-side ownership marker; then print the status lines.  Exit 1
      when something needs a hand fix (printed as "problem: ...").

Status keys: label, registry (ok|absent|unparseable), registered (yes|no),
tmp_link (ok|missing|other), marker_tmp / marker_history
(ok|missing|foreign:<path>), memory_index (ok|missing|elsewhere).

Stdlib only, Python 3.6+ (the Linux/macOS installers run it before the venv
exists; on Windows it runs with the venv's Python, which resolves junctions).
"""

import json
import os
import re
import sys
import tempfile

IS_WINDOWS = os.name == "nt"
MARKER = ".project_root"


def project_key(repo):
    """The path string Gemini stores for *repo* (process.cwd(), normalized)."""
    if IS_WINDOWS:
        # path.resolve(...).toLowerCase() on win32; cwd keeps junctions as-is.
        return os.path.abspath(repo).lower()
    # POSIX getcwd() returns the physical path.
    return os.path.realpath(repo)


def same_path(a, b):
    # realpath follows symlinks (and, on Windows with Python 3.8+, junctions).
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def slugify(text):
    s = re.sub(r"[^a-z0-9]", "-", text.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "project"


def load_registry(path):
    """Return (projects dict or None, status)."""
    if not os.path.exists(path):
        return {}, "absent"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, "unparseable"
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in projects.items()
    ):
        return None, "unparseable"
    return projects, "ok"


def registered_label(projects, key):
    for k, v in projects.items():
        if same_path(k, key):
            return v
    return None


def marker_owner(gemini_dir, sub, label):
    try:
        with open(os.path.join(gemini_dir, sub, label, MARKER), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def links_to_context(gemini_dir, label, ctx):
    p = os.path.join(gemini_dir, "tmp", label)
    return os.path.exists(p) and same_path(p, ctx)


def claim_label(gemini_dir, projects, key, ctx, base):
    """The label Gemini would use for an unregistered repo (ProjectRegistry)."""
    # findExistingSlugForPath: a label dir whose marker already names the repo.
    for sub in ("tmp", "history"):
        root = os.path.join(gemini_dir, sub)
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            owner = marker_owner(gemini_dir, sub, name)
            if owner and same_path(owner, key):
                return name
    # claimNewSlug: <slug>, <slug>-1, ... skipping taken ones.  A tmp/<slug>
    # that already links to our context is ours (an imported context may
    # carry another machine's marker; apply rewrites it).
    slug = slugify(base)
    taken = set(projects.values())
    n = 0
    while True:
        cand = slug if n == 0 else "%s-%d" % (slug, n)
        n += 1
        if cand in taken:
            continue
        ours = links_to_context(gemini_dir, cand, ctx)
        tmp_path = os.path.join(gemini_dir, "tmp", cand)
        if not ours and os.path.islink(tmp_path):
            continue  # someone else's link
        clash = False
        for sub in ("tmp", "history"):
            owner = marker_owner(gemini_dir, sub, cand)
            if owner and not same_path(owner, key) and not (sub == "tmp" and ours):
                clash = True
        if not clash:
            return cand


def status(gemini_dir, repo, label):
    ctx = os.path.join(repo, "context")
    key = project_key(repo)
    projects, reg = load_registry(os.path.join(gemini_dir, "projects.json"))
    out = [("label", label), ("registry", reg)]
    reg_label = registered_label(projects, key) if projects is not None else None
    out.append(("registered", "yes" if reg_label == label else ("other:" + reg_label if reg_label else "no")))
    tmp = os.path.join(gemini_dir, "tmp", label)
    if links_to_context(gemini_dir, label, ctx):
        out.append(("tmp_link", "ok"))
    elif os.path.lexists(tmp):
        out.append(("tmp_link", "other"))
    else:
        out.append(("tmp_link", "missing"))
    for sub in ("tmp", "history"):
        owner = marker_owner(gemini_dir, sub, label)
        if owner is None:
            val = "missing"
        elif same_path(owner, key):
            val = "ok"
        else:
            val = "foreign:" + owner
        out.append(("marker_" + sub, val))
    mem = os.path.join(tmp, "memory", "MEMORY.md")
    want = os.path.join(ctx, "memory", "MEMORY.md")
    if not os.path.exists(mem):
        out.append(("memory_index", "missing"))
    elif same_path(mem, want):
        out.append(("memory_index", "ok"))
    else:
        out.append(("memory_index", "elsewhere"))
    return out


def write_json_atomic(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".projects.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply(gemini_dir, repo, label):
    key = project_key(repo)
    ctx = os.path.join(repo, "context")
    reg_path = os.path.join(gemini_dir, "projects.json")
    projects, reg = load_registry(reg_path)
    problems = []
    if projects is None:
        problems.append("%s is not valid JSON (Gemini would reset it) — fix it by hand" % reg_path)
    else:
        cur = registered_label(projects, key)
        if cur is None:
            projects[key] = label
            write_json_atomic(reg_path, {"projects": projects})
            print("note: registered %s -> %s in %s" % (key, label, reg_path))
        elif cur != label:
            problems.append("%s maps %s to %r, not %r — leaving it alone" % (reg_path, key, cur, label))
    # Context-side marker: context/.project_root through the link.
    if links_to_context(gemini_dir, label, ctx):
        owner = marker_owner(gemini_dir, "tmp", label)
        if owner is None or not same_path(owner, key):
            with open(os.path.join(ctx, MARKER), "w", encoding="utf-8") as f:
                f.write(key)
            print("note: wrote %s (owner %s%s)" % (
                os.path.join(ctx, MARKER), key, "" if owner is None else ", was " + owner))
    hist_owner = marker_owner(gemini_dir, "history", label)
    if hist_owner and not same_path(hist_owner, key):
        problems.append(
            "%s names %s — Gemini would give this repo a new label; move that "
            "history dir aside (or pick another label) by hand"
            % (os.path.join(gemini_dir, "history", label, MARKER), hist_owner))
    for p in problems:
        print("problem: " + p)
    return not problems


def main(argv):
    args = list(argv)
    gemini_dir = None
    label = None
    for opt in ("--gemini-dir", "--label"):
        if opt in args:
            i = args.index(opt)
            if i + 1 >= len(args):
                print("missing value for " + opt, file=sys.stderr)
                return 2
            if opt == "--gemini-dir":
                gemini_dir = args[i + 1]
            else:
                label = args[i + 1]
            del args[i:i + 2]
    if len(args) != 2 or args[1] not in ("label", "check", "apply"):
        print(__doc__.split("Usage:")[1].split("Status keys")[0], file=sys.stderr)
        return 2
    repo = os.path.abspath(args[0])
    if gemini_dir is None:
        home = os.environ.get("GEMINI_CLI_HOME") or os.path.expanduser("~")
        gemini_dir = os.path.join(home, ".gemini")
    ctx = os.path.join(repo, "context")
    key = project_key(repo)
    projects, _ = load_registry(os.path.join(gemini_dir, "projects.json"))
    if label is None:
        label = registered_label(projects or {}, key) or claim_label(
            gemini_dir, projects or {}, key, ctx, os.path.basename(repo.rstrip("/\\")))
    if args[1] == "label":
        print(label)
        return 0
    ok = True
    if args[1] == "apply":
        ok = apply(gemini_dir, repo, label)
    for k, v in status(gemini_dir, repo, label):
        print("%s=%s" % (k, v))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
