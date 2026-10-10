#!/usr/bin/env python3
"""Make sure Archie's Codex home config keeps Codex's own memory store off.

Archie's memory is the context/memory wiki: for sessions in the repo the
backend passes the live context/memory/MEMORY.md (plus the wiki's write rules)
as developerInstructions (backend/manager/memory_context.py).  Codex's
``memories`` feature keeps a separate store under CODEX_HOME (memories/ +
memories_1.sqlite) that is not synced or indexed, so Archie's home pins it
off with ``[features] memories = false``.  Off is also Codex 0.161's default;
the explicit key keeps it off if the default ever flips.

Usage:
  codex-home-config.py CONFIG.toml check   print one of: ok | missing | on | absent | unsupported
  codex-home-config.py CONFIG.toml apply   add or replace the key (idempotent), print what it did

``apply`` edits the file line by line so user edits and comments survive:
inside an existing ``[features]`` table the ``memories`` line is replaced or
added; a top-level dotted ``features.memories`` key is replaced; otherwise a
``[features]`` table is appended.  An inline ``features = {...}`` table is
reported as unsupported and left alone.  When tomllib is available (Python
3.11+) the result is parsed before it is written.

Stdlib only, Python 3.6+.
"""

import os
import re
import sys

KEY_LINE = "memories = false"
COMMENT = ("# Archie's memory is the context/memory wiki (MEMORY.md is passed per "
           "session); keep Codex's own memory store off.")

HEADER_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(#.*)?$")
FEATURES_HEADER_RE = re.compile(r"^\s*\[\s*features\s*\]\s*(#.*)?$")
ARRAY_HEADER_RE = re.compile(r"^\s*\[\[")
MEMORIES_RE = re.compile(r"^\s*memories\s*=\s*([^#]*?)\s*(#.*)?$")
DOTTED_RE = re.compile(r"^\s*features\s*\.\s*memories\s*=\s*([^#]*?)\s*(#.*)?$")
INLINE_RE = re.compile(r"^\s*features\s*=\s*\{")


def analyse(lines):
    """Return (state, info).  state: ok | missing | on | unsupported."""
    in_features = False
    top_level = True
    features_at = None
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        if ARRAY_HEADER_RE.match(line):
            in_features = False
            top_level = False
            continue
        m = HEADER_RE.match(line)
        if m:
            top_level = False
            in_features = bool(FEATURES_HEADER_RE.match(line))
            if in_features:
                features_at = i
            continue
        if top_level and INLINE_RE.match(line):
            return "unsupported", {"line": i}
        if top_level:
            d = DOTTED_RE.match(line)
            if d:
                return ("ok" if d.group(1) == "false" else "on"), {"line": i, "dotted": True}
        if in_features:
            mm = MEMORIES_RE.match(line)
            if mm:
                return ("ok" if mm.group(1) == "false" else "on"), {"line": i, "dotted": False}
    return "missing", {"features_at": features_at}


def end_of_table(lines, start):
    """Index after the last non-blank line of the table that starts at *start*."""
    last = start
    for j in range(start + 1, len(lines)):
        line = lines[j].rstrip("\r\n")
        if HEADER_RE.match(line) or ARRAY_HEADER_RE.match(line):
            break
        if line.strip():
            last = j
    return last + 1


def patched(lines):
    state, info = analyse(lines)
    if state in ("ok", "unsupported"):
        return state, None
    out = list(lines)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    if state == "on":
        i = info["line"]
        out[i] = ("features.memories = false\n" if info["dotted"] else KEY_LINE + "\n")
        return "replaced", out
    if info["features_at"] is not None:
        at = end_of_table(out, info["features_at"])
        out[at:at] = [COMMENT + "\n", KEY_LINE + "\n"]
        return "added", out
    if out and out[-1].strip():
        out.append("\n")
    out += ["[features]\n", COMMENT + "\n", KEY_LINE + "\n"]
    return "appended", out


def valid_toml(text):
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return True
    try:
        data = tomllib.loads(text)
    except Exception:
        return False
    return (data.get("features") or {}).get("memories") is False


def main(argv):
    if len(argv) != 2 or argv[1] not in ("check", "apply"):
        print(__doc__.split("Usage:")[1].split("``apply``")[0], file=sys.stderr)
        return 2
    path, mode = argv
    if not os.path.isfile(path):
        print("absent")
        return 0 if mode == "check" else 1
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    if mode == "check":
        print(analyse(lines)[0])
        return 0
    what, new = patched(lines)
    if new is None:
        print(what)
        return 0 if what == "ok" else 1
    text = "".join(new)
    if not valid_toml(text):
        print("unsupported (the edited file would not parse — left alone)")
        return 1
    tmp = path + ".archie-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, path)
    print(what)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
