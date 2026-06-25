"""Recover lost mid-turn steering messages from the Claude Code exthost log.

**The hole this closes.** Mid-turn steering messages — typed into the Claude Code
VS Code extension *while the model is streaming* ("Queue another message…") — reach
the model but are **never written to the session JSONL**. So every other watcher
source, which imports from the JSONL, silently drops them (~5% of typed messages;
they're disproportionately decisions/steering). They are, however, recorded in VS
Code's extension-host log, which logs every webview→extension message:

    2026-06-25 14:59:13.302 [info] Received message from webview: {"type":
      "io_message","channelId":"…","cwd":"…","resume":"<session-id>","message":
      {"type":"user","uuid":"…","session_id":"","message":{"role":"user",
      "content":[{"type":"text","text":"…"}]}}}

**How it captures without duplicating.** For each webview user message this source
reconstructs a Claude-Code-shaped line and runs it through the *standard* CC import
path (same parser, same builder). ``dedup_key`` does the discriminating: a message
that *did* persist to the JSONL produces the identical key and dedups to nothing, so
only the genuinely-lost steering messages are written. No "is it in a JSONL?" check
is needed — the natural key is the filter.

**Thread assignment.** The webview message carries an empty ``session_id``, so the
thread is resolved from its **channel**: other lines on the same ``channelId`` carry
the ``cwd`` and the ``resume`` (the active session id), which compose the exact
``"{munged-cwd}:{session-id}"`` source_id the ClaudeCodeWatcher uses — so a recovered
message lands in its real thread, no temporal guessing. If the channel's session/cwd
or the thread isn't known yet (a brand-new session the JSONL importer hasn't created),
the message is left for a later poll (the exthost log retains ~1 day).
"""

from __future__ import annotations

import glob
import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from thread_import import DefaultEventBuilder
from thread_import.parsers.claude_code import ClaudeCodeParser

from ..importers._events import import_lines
from ..importers._state import get_thread_by_source
from ..store import get_session
from .base import SourceWatcher, WatchResult

logger = logging.getLogger(__name__)

# Recovered steering messages belong to their Claude Code thread, so they import
# under the same source identity the JSONL path uses — which is what makes dedup_key
# collapse a message that turns out to have persisted after all.
SOURCE = "claude-code"

_RECV = re.compile(r"Received message from webview: (\{.*)$")
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) ")
# Don't act on a webview message until it's this old. A genuinely-lost mid-turn
# message is *never* written to the JSONL, so the grace only delays its capture; a
# between-turn message that's merely mid-write reaches the JSONL within seconds, so
# the grace lets the JSONL path claim it first and prevents a context-injected
# duplicate. (The exthost log stamps local wall-clock; ``now()`` is local too.)
_GRACE_SECONDS = 300
# Bare slash-command lines ("/debrief") — the CC importer drops these by design
# (the injected skill doc persists instead, carrying the command), so flagging them
# as lost would cry wolf on every command typed. Commands WITH args stay capturable.
_BARE_COMMAND = re.compile(r"^/[\w:-]+\s*$")


def _log_globs() -> list[str]:
    """Glob patterns for the Claude Code exthost logs under VS Code (+ Insiders)."""
    base = Path.home() / "Library" / "Application Support"
    leaf = ("logs", "*", "window*", "exthost", "Anthropic.claude-code", "Claude VSCode.log")
    return [str(base.joinpath(app, *leaf)) for app in ("Code - Insiders", "Code")]


def _munge_cwd(cwd: str) -> str:
    """Claude Code's project-dir munge (the source_id prefix): ``/`` and ``.`` → ``-``."""
    return cwd.replace("/", "-").replace(".", "-")


def _session_jsonl(cwd: str, session_id: str) -> Optional[Path]:
    """The Claude Code session transcript for ``(cwd, session_id)``, if on disk.

    This is the ground-truth gate for "lost vs persisted": a webview message whose
    uuid is in this file *did* persist (the JSONL importer will/has captured it, with
    whatever IDE context CC injected), so the exthost path must skip it. Only a uuid
    absent from the file is a genuinely-lost steering message worth importing."""
    proj = _munge_cwd(cwd)
    for base in sorted(Path.home().glob(".claude*/projects")):
        f = base / proj / f"{session_id}.jsonl"
        if f.exists():
            return f
    return None


def _too_fresh(iso: Optional[str]) -> bool:
    """True if a message is younger than the grace period (so defer — it may still
    be mid-write to the JSONL). Unparseable/absent timestamp → not fresh (act now)."""
    if not iso:
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:  # pragma: no cover — malformed stamp
        return False
    return age < _GRACE_SECONDS


def _content_text(content) -> Optional[str]:
    """Plain text of a webview message's content (string, or joined text blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts) if parts else None
    return None


def parse_exthost_log(text: str, *, skip_uuids: Optional[set] = None):
    """Parse one exthost log's text into the pieces import needs.

    Returns ``(chan_cwd, chan_session, messages, bare_commands)``:
      * ``chan_cwd`` / ``chan_session`` — ``channelId`` → cwd / resumed session id,
        from the control lines (the two halves of a thread's source_id);
      * ``messages`` — ``(channelId, uuid, content, iso_ts)`` per fresh webview user
        message (content kept raw so the CC parser normalizes it identically to the
        JSONL path — that's what keeps dedup_key aligned);
      * ``bare_commands`` — uuids of bare slash-command lines, which aren't losses.

    ``skip_uuids`` short-circuits messages already handled this process.
    """
    skip = skip_uuids or set()
    chan_cwd: dict[str, str] = {}
    chan_session: dict[str, str] = {}
    messages: list[tuple[str, str, object, Optional[str]]] = []
    bare_commands: list[str] = []

    for line in text.splitlines():
        m = _RECV.search(line)
        if not m:
            continue
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        ch = obj.get("channelId")
        if ch:
            if obj.get("cwd"):
                chan_cwd.setdefault(ch, obj["cwd"])
            if obj.get("resume"):
                chan_session.setdefault(ch, obj["resume"])
        inner = obj.get("message", {})
        if not isinstance(inner, dict) or inner.get("type") != "user":
            continue
        uuid = inner.get("uuid")
        if not uuid or uuid in skip or not ch:
            continue
        content = inner.get("message", {}).get("content")
        msg_text = _content_text(content)
        if msg_text is None:
            continue
        if _BARE_COMMAND.match(msg_text.strip()):
            bare_commands.append(uuid)
            continue
        ts = _TS.match(line)
        iso = ts.group(1).replace(" ", "T") if ts else None
        messages.append((ch, uuid, content, iso))

    return chan_cwd, chan_session, messages, bare_commands


class ExthostWatcher(SourceWatcher):
    """Reads the Claude Code exthost log and imports lost mid-turn steering messages."""

    def __init__(self, log_globs: Optional[list[str]] = None) -> None:
        self._globs = log_globs if log_globs is not None else _log_globs()
        # Per-log (mtime_ns, size) fingerprints — an unchanged log is skipped wholesale.
        self._seen_fp: dict[str, tuple[int, int]] = {}
        # uuids handled this process (imported, deduped, or skipped) — a cheap pre-DB
        # filter so a re-scan of an unchanged-tail log doesn't re-resolve every message.
        self._handled: set[str] = set()
        self._parser = ClaudeCodeParser()
        self._builder = DefaultEventBuilder()

    @property
    def source_name(self) -> str:
        return "cc-exthost"

    def _logs(self) -> list[str]:
        out: list[str] = []
        for g in self._globs:
            out.extend(glob.glob(g))
        return sorted(set(out))

    def is_available(self) -> bool:
        return bool(self._logs())

    def poll(self) -> WatchResult:
        result = WatchResult()
        seen_this_poll: set[str] = set()
        for path in self._logs():
            p = Path(path)
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue
            key = str(p)
            seen_this_poll.add(key)
            fp = (st.st_mtime_ns, st.st_size)
            if self._seen_fp.get(key) == fp:
                result = result + WatchResult(sources_checked=1)
                continue
            try:
                r, deferred = self._process_log(p)
                # Fingerprint only when nothing is still aging through the grace window;
                # a deferred message must be reconsidered next poll even if the (idle)
                # log stops changing, so leave the log un-fingerprinted until it clears.
                if not deferred:
                    self._seen_fp[key] = fp
                result = result + r
            except Exception as e:  # noqa: BLE001 — one bad log must not stop the poll
                msg = f"cc-exthost {p.name}: {e}"
                logger.warning(msg)
                result = result + WatchResult(sources_checked=1, errors=[msg])
        if seen_this_poll:
            self._seen_fp = {k: v for k, v in self._seen_fp.items() if k in seen_this_poll}
        return result

    def _process_log(self, path: Path) -> tuple[WatchResult, int]:
        chan_cwd, chan_session, webview_users, bare = parse_exthost_log(
            path.read_text(errors="replace"), skip_uuids=self._handled
        )
        # A bare slash-command isn't a loss — mark it handled so it's never reconsidered.
        self._handled.update(bare)

        # Resolve each message's thread from its channel and group by thread —
        # keeping only the genuinely-lost ones (uuid absent from the session JSONL).
        by_thread: dict[tuple[int, str], list[tuple[str, dict]]] = defaultdict(list)
        jsonl_cache: dict[str, str] = {}  # session-file path → its text, read once per poll
        deferred = 0
        with get_session() as s:
            thread_cache: dict[str, Optional[int]] = {}
            for ch, uuid, content, iso in webview_users:
                cwd = chan_cwd.get(ch)
                session_id = chan_session.get(ch)
                if not cwd or not session_id:
                    continue  # channel context not seen yet — retry a later poll

                # The lost-vs-persisted gate: if the uuid is in the session JSONL it
                # persisted (with whatever IDE context CC injected) — the JSONL path owns
                # it, so skip. uuid identity sidesteps the content-hash drift that context
                # injection causes. No JSONL on disk → can't confirm → leave it.
                jsonl = _session_jsonl(cwd, session_id)
                if jsonl is None:
                    continue
                jkey = str(jsonl)
                if jkey not in jsonl_cache:
                    try:
                        jsonl_cache[jkey] = jsonl.read_text(errors="replace")
                    except OSError:
                        jsonl_cache[jkey] = ""
                if uuid in jsonl_cache[jkey]:
                    self._handled.add(uuid)  # persisted — never reconsider
                    continue

                # Grace: a fresh message may still be mid-write to the JSONL. Defer it
                # (don't mark handled) so a later poll re-checks once it's aged out.
                if _too_fresh(iso):
                    deferred += 1
                    continue

                source_id = f"{_munge_cwd(cwd)}:{session_id}"
                if source_id not in thread_cache:
                    t = get_thread_by_source(s, SOURCE, source_id)
                    thread_cache[source_id] = t.id if t else None
                tid = thread_cache[source_id]
                if tid is None:
                    continue  # thread not created yet (JSONL importer will); retry later
                line = {
                    "type": "user",
                    "uuid": uuid,
                    "timestamp": iso,
                    "sessionId": session_id,
                    "cwd": cwd,
                    "message": {"role": "user", "content": content},
                }
                by_thread[(tid, source_id)].append((uuid, line))

            events_created = 0
            handled: list[str] = []
            for (tid, source_id), items in by_thread.items():
                lines = [ln for _, ln in items]
                created, _ = import_lines(
                    s, tid, lines, self._parser, self._builder, source=SOURCE, source_id=source_id
                )
                events_created += created
                handled.extend(u for u, _ in items)
            s.commit()

        self._handled.update(handled)
        if events_created:
            logger.info(
                "cc-exthost: recovered %d lost steering event(s) across %d thread(s) from %s",
                events_created, len(by_thread), path.name,
            )
        return (
            WatchResult(
                sources_checked=1,
                items_imported=sum(1 for _ in by_thread) if events_created else 0,
                events_created=events_created,
            ),
            deferred,
        )
