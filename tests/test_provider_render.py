"""A plugin's display policy and session-id shape reach the readers.

The three things a provider knows about itself and generic code must not hardcode:
how its user turns should be displayed, what its preserved blocks mean, and how a
session id sits inside its ``source_id``. Built-ins declare all three through the
public :class:`~thread_archive.provider.Provider` descriptor, so these tests drive
a provider installed the way a plugin author installs one — declared in
``config.json``, discovered at registry build — and assert the readers honor it.

If a built-in could do any of this and a plugin could not, the seam would be a
private convenience rather than an API.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from thread_archive._retrieval import read_thread, read_thread_structured
from thread_archive._store import Event, Thread, init_db, use_session

_PLUGIN = '''
from thread_archive.provider import DEFAULT_VIEW, Provider, RenderPolicy


def _unwrap(content):
    """Show only what the operator typed, past this harness's banner."""
    marker = "###PROMPT###"
    head, sep, tail = content.partition(marker)
    return tail.strip() if sep and tail.strip() else content


def _block(block_type, data, rendered_text):
    if block_type == "demo_noise":
        return None                      # machinery: hide it
    if block_type == "demo_note":
        return ("note", (data or {}).get("text", ""))
    return DEFAULT_VIEW                  # not ours: let the reader decide


PROVIDER = Provider(
    name="demo",
    label="Demo",
    render=RenderPolicy(user_content=_unwrap, block=_block),
    session_id_separators=("|",),
)
'''


def _dt(minute: int) -> datetime:
    return datetime(2026, 1, 1, 10, minute, 0, tzinfo=timezone.utc)


@pytest.fixture
def demo_provider(archive_home):
    """Install the demo provider through the config-declared discovery path."""
    from thread_archive import _providers

    pkg = archive_home / "plugin-src"
    pkg.mkdir()
    (pkg / "demo_archive.py").write_text(_PLUGIN, encoding="utf-8")
    (archive_home / "config.json").write_text(json.dumps({
        "providers": {"demo": {"module": "demo_archive:PROVIDER", "path": str(pkg)}}
    }), encoding="utf-8")

    _providers.reset()
    assert _providers.get("demo") is not None, "the declared plugin was not discovered"
    yield
    _providers.reset()


def _tid(n: int) -> str:
    """A fixed, valid ULID for test seed ``n`` (26 chars, Crockford alphabet,
    not all-digits so it resolves via the primary key, not legacy_id)."""
    return f"01TEST{n:020d}"


def _seed(events, *, tid=1, source="demo", source_id="demo-1"):
    if isinstance(tid, int):
        tid = _tid(tid)
    init_db()
    with use_session() as s:
        s.add(Thread(id=tid, name=f"t{tid}", title="Demo", thread_type="conversation",
                     source=source, source_id=source_id,
                     inserted_at=_dt(0), updated_at=_dt(0)))
        s.commit()
        for i, (et, payload) in enumerate(events, start=1):
            s.add(Event(id=i, thread_id=tid, stream_id="s", event_type=et,
                        payload=payload, occurred_at=_dt(i)))
        s.commit()
    return tid


_WRAPPED = "You are a helpful assistant.\n###PROMPT###\nwhat is 2+2"


def test_plugin_user_content_policy_applies_to_its_own_threads(demo_provider) -> None:
    tid = _seed([("user_message_sent", {"content": _WRAPPED}),
                 ("text_complete", {"text": "4"})])
    out = read_thread(tid, mode="user")
    assert "what is 2+2" in out
    assert "You are a helpful assistant" not in out


def test_plugin_user_content_policy_does_not_reach_other_providers(demo_provider) -> None:
    """The same banner in a claude-code turn is content, and stays."""
    tid = _seed([("user_message_sent", {"content": _WRAPPED})],
                source="claude-code", source_id="proj:x")
    assert "You are a helpful assistant" in read_thread(tid, mode="user")


def test_plugin_block_policy_hides_renders_and_defers(demo_provider) -> None:
    """All three answers a policy can give about a preserved block."""
    tid = _seed([
        ("user_message_sent", {"content": "###PROMPT###\ngo"}),
        ("content_block", {"block_type": "demo_noise", "data": {"text": "telemetry"}}),
        ("content_block", {"block_type": "demo_note", "data": {"text": "a real note"}}),
        ("content_block", {"block_type": "other", "data": {"text": "unclaimed"}}),
    ])
    blocks = [
        (b.get("block_type"), b.get("text"))
        for m in read_thread_structured(tid)["messages"] for b in m["blocks"]
        if b["type"] == "content_block"
    ]
    assert ("note", "a real note") in blocks, "a claimed block renders under its label"
    assert ("other", "unclaimed") in blocks, "DEFAULT_VIEW falls through to the reader"
    assert not any(bt == "demo_noise" for bt, _ in blocks), "hidden block leaked into the read"


def test_plugin_session_id_separator_resolves_a_bare_session_id(demo_provider) -> None:
    """A plugin composing ``{prefix}|{session}`` is resolvable by the bare session id."""
    from thread_archive._store.resolve import resolve_session_source_id

    tid = _seed([("user_message_sent", {"content": "hi"})], source_id="workspace|sess-9")
    with use_session() as s:
        assert resolve_session_source_id(s, "sess-9", source="demo") == tid
        assert resolve_session_source_id(s, "sess-9") == tid
        assert resolve_session_source_id(s, "workspace|sess-9") == tid


def test_a_plugin_declaring_no_policy_renders_as_stored(archive_home) -> None:
    """The default: no policy, no rewriting. Most providers want exactly this."""
    from thread_archive.provider import Provider

    p = Provider(name="plain", label="Plain")
    assert p.render is None
    assert p.session_id_separators == ()


# ── built-ins go through the same machinery a plugin does ────────────────────
# The API is only honest if its own providers use it. A seam a built-in reaches
# around is a seam no plugin can reach at all, and the drift is silent — the
# built-in keeps working while the published path rots untested.

def test_line_stream_builtins_are_built_by_the_public_factory() -> None:
    """archive's line-stream providers are constructed by ``line_stream_importer``.

    Not a style preference: this factory is the only line-stream path a plugin
    has, so if no built-in is built from it, nothing archive runs exercises it.
    """
    from thread_archive._providers import registry
    from thread_archive.provider import claude_code_line_stream, line_stream_importer

    # Both published line-stream factories: build your own format, or reuse Claude
    # Code's. A provider's importer should come from one of them.
    factories = {
        line_stream_importer(
            "probe", has_importable_content=lambda _: False,
            make_title=lambda *_: "", import_lines=lambda *_: (0, None),
        ).__qualname__,
        claude_code_line_stream("probe").__qualname__,
    }

    line_streams = [p for p in registry().values() if p.kind == "line-stream"]
    assert line_streams, "no line-stream providers registered — the check is vacuous"
    hand_rolled = [
        p.name for p in line_streams
        if p.importer.__qualname__ not in factories
        # Claude Code merges compaction continuations and forks into existing
        # threads, which the standard one-file-one-thread lifecycle cannot express.
        and p.name != "claude-code"
    ]
    assert not hand_rolled, (
        f"{hand_rolled} build their importer by hand instead of from a published "
        f"factory — the only line-stream path a plugin has"
    )


def test_prepare_receives_what_a_sibling_file_provider_needs(archive_home) -> None:
    """``prepare`` gets the path and the source id, not just the lines.

    Grok's timestamps live in files *next to* the transcript, so its import is not
    a function of the JSONL alone. Without both arguments a provider shaped like
    that cannot use the public factory at all, and would have to reach past it —
    which is exactly how Grok's importer was built before it went through here.
    """
    from thread_archive.provider import line_stream_importer

    init_db()
    path = archive_home / "sess.jsonl"
    path.write_text(json.dumps({"type": "message", "text": "hi"}) + "\n", encoding="utf-8")

    seen: dict = {}

    def prepare(all_lines, transcript_path, source_id):
        seen.update(lines=len(all_lines), path=transcript_path, source_id=source_id)
        return source_id

    # The context prepare returns reaches every later callback.
    def import_lines(sess, thread_id, all_lines, new_lines, ctx):
        seen["ctx_in_import_lines"] = ctx
        return 0, None

    importer = line_stream_importer(
        "probe",
        prepare=prepare,
        has_importable_content=lambda _new: True,
        make_title=lambda _all, ctx: f"probe {ctx}",
        import_lines=import_lines,
    )
    importer(path, "workspace|sess-9")

    assert seen["source_id"] == "workspace|sess-9", "prepare was not handed the source id"
    assert seen["path"] == path, "prepare was not handed the transcript path"
    assert seen["lines"] == 1
    assert seen["ctx_in_import_lines"] == "workspace|sess-9"
