"""Code-axis extraction: a tool event's payload → the files it touched, and the
commits it produced.

The sibling of :mod:`._extract` (which turns a payload into searchable text). Both
read the importer's payload dicts directly; this one reads the *structure* instead
of the prose — ``Edit``'s ``file_path``, ``apply_patch``'s patch header, a shell
command's path-shaped arguments — so "which conversations edited rank.py" is an
indexed lookup rather than a text search that happens to match a path.

Pure functions, no I/O: every provider's naming for the same idea is handled by
name, because a path axis that only understood Claude Code's spelling would be a
Claude-Code-only feature (see ``_OP_BY_TOOL`` / ``_PATH_KEYS``).

Ops are the verb, normalized across providers: ``read`` / ``edit`` / ``write`` /
``delete`` are direct touches, ``search`` is a path used as a *scope* (a Grep's
directory), and ``run`` is a path named inside a shell command. The last two are
weaker evidence on purpose — a conversation that grepped a directory did not
change it — and callers rank them accordingly.
"""

from __future__ import annotations

import posixpath
import re
from typing import Iterable, Optional

# ── Ops ──────────────────────────────────────────────────────────────────────

#: Direct touches: the tool named the file and acted on it. Ordered by strength of
#: evidence — a change outranks a look — and every ranking and rendering in the code
#: axis reads that order off this tuple.
TOUCH_OPS = ("edit", "write", "delete", "read")
#: Every op, strongest evidence first. ``search``/``run`` are incidental mentions.
ALL_OPS = TOUCH_OPS + ("search", "run")

# Tool → op, keyed on the tool name lowercased with any MCP/plugin prefix stripped
# (``mcp__server__read_file`` and ``mcp_server_read_file`` both reduce to
# ``read_file``). Provider spellings collapse here rather than at import time: the
# importers' canonical-name map is applied unevenly across providers, so the archive
# holds both ``Read`` and ``read_file`` for the same act.
_OP_BY_TOOL: dict[str, str] = {
    # read
    "read": "read", "read_file": "read", "readfile": "read", "view": "read",
    "view_file": "read", "open_file": "read", "cat": "read", "notebookread": "read",
    "read_many_files": "read", "file_read": "read",
    # edit
    "edit": "edit", "multiedit": "edit", "edit_file": "edit", "editfile": "edit",
    "search_replace": "edit", "str_replace": "edit", "str_replace_editor": "edit",
    "str_replace_based_edit_tool": "edit", "apply_patch": "edit", "patch": "edit",
    "update_file": "edit", "notebookedit": "edit", "file_edit": "edit",
    "replace_string_in_file": "edit",
    # write
    "write": "write", "write_file": "write", "writefile": "write",
    "create_file": "write", "new_file": "write", "file_write": "write",
    "create_text_file": "write",
    # delete
    "delete_file": "delete", "remove_file": "delete", "rm": "delete",
    # search — the path is a scope, not a touch
    "grep": "search", "glob": "search", "grep_files": "search", "grep_search": "search",
    "ripgrep_raw_search": "search", "glob_file_search": "search", "file_search": "search",
    "search_files": "search", "codebase_search": "search", "list_dir": "search",
    "ls": "search", "find": "search", "list_directory": "search",
    # run — paths named inside a command line
    "bash": "run", "shell": "run", "run_shell": "run", "run_command": "run",
    "run_terminal_cmd": "run", "run_terminal_command_v2": "run", "exec": "run",
    "exec_command": "run", "terminal": "run", "shell_command": "run",
    "local_shell": "run", "process": "run",
}

# Input keys carrying one path, checked in this order (first present wins). Every
# provider spells it differently; ``uri``/``url`` are deliberately absent — a
# file:// URI is not a path and a http URL is not a file.
_PATH_KEYS = (
    "file_path", "filePath", "filepath", "target_file", "notebook_path",
    "notebookPath", "abs_path", "absolute_path", "target_path", "path", "file",
    "filename", "dir_path", "directory",
)

# Input keys carrying a list of paths.
_PATH_LIST_KEYS = ("file_paths", "paths", "files", "targets", "target_files")

# Input keys whose string value is a shell command line to scan for paths.
_COMMAND_KEYS = ("command", "cmd", "commands", "script", "shell_command")

# Input keys whose string value is a patch/diff body to parse for file headers.
_PATCH_KEYS = ("patch", "diff", "patch_text", "edits")


# ── Path shape ───────────────────────────────────────────────────────────────

# Extensions that make a bare token in a command line worth recording as a path.
# A command line is mostly not paths, so the extension is the filter that keeps
# ``run`` evidence from becoming noise; a path arriving through a *keyed* input
# (file_path=…) needs no extension, because the tool already said it was a file.
_CODE_EXTS = frozenset("""
py pyi pyx ipynb js jsx mjs cjs ts tsx vue svelte go rs rb erb java kt kts scala
swift m mm c h cc cpp cxx hpp hh cs php pl pm lua r jl dart ex exs elm clj cljs
sh bash zsh fish ps1 bat sql prisma graphql proto thrift
html htm css scss sass less styl
md mdx rst txt adoc org tex
json jsonl json5 ndjson yaml yml toml ini cfg conf env properties plist xml csv tsv
lock sum mod gradle tf tfvars hcl bzl bazel cmake mk make
patch diff snap map d.ts
""".split())

# A path-shaped token: no whitespace, no shell metacharacters, no scheme. Leading
# ``-`` is excluded so a flag (``--file=x``) is never mistaken for a path; the
# ``key=value`` split below feeds the value in separately.
_TOKEN_RE = re.compile(r"^[~./A-Za-z0-9_][A-Za-z0-9_./~@+\-]*$")

# Shell metacharacters + quoting that separate arguments. Split on these before
# testing tokens, so ``cat foo.py|head`` still yields ``foo.py``.
_SPLIT_RE = re.compile(r"""[\s|;&()<>{}'"`,:*?\[\]!$\\]+""")

# ``*** Update File: path`` (and Add/Delete/Move) — the apply_patch envelope Codex
# and Cursor both write.
_APPLY_PATCH_RE = re.compile(
    r"^\*\*\*\s+(Update|Add|Delete|Move)\s+File:\s*(.+?)\s*$", re.MULTILINE
)
_APPLY_PATCH_OPS = {"Update": "edit", "Add": "write", "Delete": "delete", "Move": "edit"}

# Unified-diff file headers (``+++ b/src/foo.py``). ``/dev/null`` marks the absent
# side of an add/delete and is never a path.
_DIFF_RE = re.compile(r"^(?:\+\+\+|---)\s+(?:[ab]/)?(\S+)\s*$", re.MULTILINE)

# The git commit summary line: ``[main 31bade5] subject``, ``[detached HEAD abc1234]
# subject``, ``[main (root-commit) abc1234] subject``.
_COMMIT_RE = re.compile(
    r"^\[(?P<branch>[^\]]+?)\s+(?:\(root-commit\)\s+)?(?P<sha>[0-9a-f]{7,40})\]\s*(?P<subject>.*)$",
    re.MULTILINE,
)
# What proves the bracket line above came from ``git commit`` and not from some
# other tool's output that happens to bracket a hex word: git's own diffstat, which
# ``commit`` always prints and no reader command does in this shape.
_COMMIT_CORROBORATION = re.compile(r"insertions?\(\+\)|deletions?\(-\)|\(root-commit\)")

#: The SQL pre-filter that finds candidate commit output without parsing 400k tool
#: payloads in Python. Kept in step with :data:`_COMMIT_CORROBORATION` — one LIKE
#: per alternative, so the cheap scan and the authoritative regex agree on the
#: candidate set instead of quietly disagreeing about what the fold ever sees.
COMMIT_PREFILTER_LIKES = ("%insertion%", "%deletion%", "%root-commit%")

#: Paths under these directories are build/dependency noise — recorded by nobody's
#: intent, and they would swamp a repository's real files.
_NOISE_SEGMENTS = frozenset({
    "node_modules", "__pycache__", ".git", ".venv", "venv", "site-packages",
    "dist-info", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next",
})

_MAX_COMMAND_PATHS = 8
_MAX_PATHS_PER_EVENT = 64
_MAX_PATH_CHARS = 400


def canonical_tool(tool_name: Optional[str]) -> str:
    """A tool name reduced to its comparable core: lowercased, MCP/plugin prefix
    stripped. ``mcp__thread-archive__thread_read`` → ``thread_read``."""
    if not tool_name:
        return ""
    name = tool_name.strip()
    # ``mcp<sep>server<sep>verb`` — split at most twice, because the *verb* keeps
    # its own separators (``read_file``, ``db_query_capture``) and taking the last
    # fragment would shear them off. The double-underscore form is checked first;
    # it also matches the single-underscore prefix.
    for sep in ("__", "_"):
        if name.lower().startswith("mcp" + sep):
            parts = name.split(sep, 2)
            if len(parts) == 3:
                name = parts[2]
            break
    return name.lower()


def tool_op(tool_name: Optional[str]) -> Optional[str]:
    """The op a tool performs on the paths in its input, or None if it touches none."""
    return _OP_BY_TOOL.get(canonical_tool(tool_name))


def normalize_path(raw: str, cwd: Optional[str] = None) -> Optional[str]:
    """A raw path from a tool input → the archive's canonical form, or None when it
    isn't a usable path.

    Absolute where it can be: a relative path is resolved against ``cwd`` (the
    thread's working directory), because the same file arrives as ``src/foo.py``
    from one session and ``/repo/src/foo.py`` from the next, and a path axis that
    kept them apart would answer "which conversations edited this" with half the
    answer. ``~`` is left symbolic — the archive has no business guessing another
    machine's home — and ``..`` is collapsed lexically, never by touching the
    filesystem (the file may be long gone, and the index must not depend on it).
    """
    if not raw or not isinstance(raw, str):
        return None
    path = raw.strip().strip("'\"")
    if not path or len(path) > _MAX_PATH_CHARS:
        return None
    if "\n" in path or "\x00" in path:
        return None
    if "://" in path:  # a URL, not a path
        return None
    if path in (".", "..", "/", "-"):
        return None
    # Windows-style separators normalize to posix so one corpus has one spelling.
    path = path.replace("\\", "/")
    if not path.startswith(("/", "~")) and cwd:
        path = posixpath.join(cwd.replace("\\", "/").rstrip("/"), path)
    path = posixpath.normpath(path)
    # Nothing left but separators and parent references — ``src/../..`` normalizes
    # to ``..``, which names no file anyone can look up.
    if path in (".", "/", "") or all(seg in ("", "..") for seg in path.split("/")):
        return None
    if any(seg in _NOISE_SEGMENTS for seg in path.split("/")):
        return None
    return path


def basename_of(path: str) -> str:
    """The path's final segment — the index's cheap first cut for a bare-name query."""
    return posixpath.basename(path.rstrip("/")) or path


# A leading ``cd <dir>`` in a command line — the shell's own cwd override, and the
# reason a bare ``src/foo.py`` in a Bash call so often isn't relative to the
# session's directory at all.
_CD_RE = re.compile(r"(?:^|[;&|]\s*)cd\s+([^\s;&|]+)")


def command_cwd(command: str, cwd: Optional[str]) -> Optional[str]:
    """The directory a command's relative paths actually resolve against.

    Agents chain ``cd lab && .venv/bin/pytest tests/x.py`` constantly, and resolving
    that ``tests/x.py`` against the *session's* directory invents a path that never
    existed. The first ``cd`` in the line wins, which is what the shell does for
    everything after it.
    """
    match = _CD_RE.search(command or "")
    if not match:
        return cwd
    target = match.group(1).strip().strip("'\"")
    if not target or target.startswith("-"):
        return cwd
    if target.startswith(("/", "~")):
        return posixpath.normpath(target)
    if not cwd:
        return cwd
    return posixpath.normpath(posixpath.join(cwd, target))


def _command_paths(command: str) -> list[str]:
    """Path-shaped tokens in a shell command line.

    Deliberately conservative: only tokens carrying a recognized source/config
    extension survive, because a command line is mostly flags, options, and prose,
    and the alternative — anything containing a slash — records every URL fragment
    and regex in the corpus as a file.
    """
    out: list[str] = []
    seen: set[str] = set()
    for chunk in _SPLIT_RE.split(command):
        if not chunk:
            continue
        # ``--file=src/x.py`` / ``PYTHONPATH=lib`` — the value is the candidate.
        if "=" in chunk:
            chunk = chunk.rsplit("=", 1)[1]
        if not chunk or not _TOKEN_RE.match(chunk):
            continue
        ext = chunk.rsplit(".", 1)[-1].lower() if "." in chunk else ""
        if ext not in _CODE_EXTS:
            continue
        if chunk in seen:
            continue
        seen.add(chunk)
        out.append(chunk)
        if len(out) >= _MAX_COMMAND_PATHS:
            break
    return out


def _patch_paths(body: str) -> list[tuple[str, str]]:
    """``(path, op)`` pairs from an apply_patch envelope or a unified diff."""
    out: list[tuple[str, str]] = []
    for verb, path in _APPLY_PATCH_RE.findall(body):
        # A Move line carries ``old -> new``; both ends are touched.
        for part in path.split(" -> "):
            out.append((part.strip(), _APPLY_PATCH_OPS[verb]))
    if not out:
        for path in _DIFF_RE.findall(body):
            if path != "/dev/null":
                out.append((path, "edit"))
    return out


def _iter_raw(value) -> Iterable[str]:
    """Strings inside a path-list value (a list, or a single string)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                for key in _PATH_KEYS:
                    if isinstance(item.get(key), str):
                        yield item[key]
                        break


def extract_paths(
    event_type: str, payload: dict, *, cwd: Optional[str] = None
) -> list[tuple[str, str]]:
    """``(normalized_path, op)`` pairs an event touched, deduped, order-preserved.

    Reads ``tool_use_complete`` / ``tool_use_started`` events (the call, which is
    where the path lives); a tool's *result* names no files of its own.
    """
    if event_type not in ("tool_use_complete", "tool_use_started"):
        return []
    if not payload:
        return []
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("input")
    op = tool_op(tool_name)

    # ``(raw value, op, the directory it resolves against)`` — the third element
    # because a command line can move the shell out from under the session's cwd.
    raw: list[tuple[str, str, Optional[str]]] = []

    def add_command(text: str) -> None:
        base = command_cwd(text, cwd)
        raw.extend((p, "run", base) for p in _command_paths(text))

    if isinstance(tool_input, str):
        # Codex's apply_patch puts the whole envelope in a bare string input.
        raw += [(p, o, cwd) for p, o in _patch_paths(tool_input)]
        if op == "run":
            add_command(tool_input)
    elif isinstance(tool_input, dict):
        for key in _PATCH_KEYS:
            body = tool_input.get(key)
            if isinstance(body, str) and body:
                raw += [(p, o, cwd) for p, o in _patch_paths(body)]
        # ``apply_patch``'s envelope also arrives under a plain ``input`` key.
        inner = tool_input.get("input")
        if isinstance(inner, str) and "*** " in inner:
            raw += [(p, o, cwd) for p, o in _patch_paths(inner)]
        if op:
            for key in _PATH_KEYS:
                value = tool_input.get(key)
                if isinstance(value, str) and value:
                    raw.append((value, op, cwd))
                    break
            for key in _PATH_LIST_KEYS:
                if key in tool_input:
                    raw += [(p, op, cwd) for p in _iter_raw(tool_input[key])]
        if op == "run":
            for key in _COMMAND_KEYS:
                value = tool_input.get(key)
                if isinstance(value, str) and value:
                    add_command(value)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        if isinstance(item, str):
                            add_command(item)

    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value, value_op, base in raw:
        path = normalize_path(value, base)
        if path is None:
            continue
        touch = (path, value_op)
        if touch in seen:
            continue
        seen.add(touch)
        out.append(touch)
        if len(out) >= _MAX_PATHS_PER_EVENT:
            break
    return out


def extract_commits(event_type: str, payload: dict) -> list[tuple[str, str]]:
    """``(sha, subject)`` pairs for commits an event's tool output *created*.

    Only commit-creation output counts. A session that ran ``git log`` saw a
    hundred shas and authored none of them, so the summary line alone is not
    enough — the output must also carry git's own diffstat (or the root-commit
    marker), which only ``commit`` prints.
    """
    if event_type not in ("tool_execution_completed", "tool_use_complete"):
        return []
    if not payload or payload.get("is_error"):
        return []
    output = payload.get("output")
    if not isinstance(output, str) or "]" not in output:
        return []
    if not _COMMIT_CORROBORATION.search(output):
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _COMMIT_RE.finditer(output):
        sha = match.group("sha").lower()
        if sha in seen:
            continue
        seen.add(sha)
        out.append((sha, match.group("subject").strip()[:500]))
    return out
