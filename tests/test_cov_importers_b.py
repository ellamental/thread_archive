"""Branch-coverage tests for the bulk/DB importers.

Cluster: ``_importers/exports.py`` (claude.ai/ChatGPT/xAI account exports),
``_importers/opencode.py``, ``_importers/claude_science.py``,
``_importers/antigravity.py`` and ``_importers/_titles.py``.

These target the format-detection / loader / error-preservation / derivation
branches the happy-path preserve tests don't reach. Pure helpers are exercised
directly; DB/JSONL importers are fed small fixtures that hit their uncovered
record shapes. Coverage tag: impb.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from thread_archive._importers import _titles as titles
from thread_archive._importers import antigravity as ag
from thread_archive._importers import claude_science as cs
from thread_archive._importers import exports as ex
from thread_archive._importers import (
    import_antigravity_session_incremental,
    import_claude_science_db,
    import_opencode_db,
)
from thread_archive._importers import opencode as oc
from thread_archive._importers.claude_science import import_claude_science_frame
from thread_archive._importers.opencode import import_opencode_from_payload
from thread_archive._store import (
    Event,
    ImportState,
    Thread,
    get_session,
    init_db,
)

# ══════════════════════════════════════════════════════════════════════════════
# _titles.py — pure title/description derivation
# ══════════════════════════════════════════════════════════════════════════════


def _cc_command_msg(content):
    """A NormalizedMessage carrying a raw CC line (for _cc_command_title)."""
    return {
        "role": "user",
        "content_text": "",
        "provider_data": {"line": {"message": {"content": content}}},
    }


def test_cc_command_title_with_args() -> None:
    msg = _cc_command_msg("<command-name>/garden</command-name><command-args>weeds</command-args>")
    assert titles._cc_command_title(msg) == "/garden weeds"


def test_cc_command_title_no_args() -> None:
    msg = _cc_command_msg("<command-name>/sprawl</command-name>")
    assert titles._cc_command_title(msg) == "/sprawl"


def test_cc_command_title_empty_args_ignored() -> None:
    msg = _cc_command_msg("<command-name>/x</command-name><command-args>   </command-args>")
    assert titles._cc_command_title(msg) == "/x"


def test_cc_command_title_guard_paths() -> None:
    # provider_data not a dict
    assert titles._cc_command_title({"provider_data": "nope"}) is None
    # no line
    assert titles._cc_command_title({"provider_data": {}}) is None
    # line not a dict
    assert titles._cc_command_title({"provider_data": {"line": 5}}) is None
    # message not a dict
    assert titles._cc_command_title({"provider_data": {"line": {"message": 3}}}) is None
    # content not a str
    assert titles._cc_command_title({"provider_data": {"line": {"message": {"content": []}}}}) is None
    # no command-name marker
    assert titles._cc_command_title(_cc_command_msg("plain text, no command")) is None


def test_extract_ai_title_last_wins() -> None:
    lines = [
        {"type": "ai-title", "aiTitle": "First"},
        {"type": "ai-title", "aiTitle": "  "},  # blank ignored
        {"type": "ai-title", "aiTitle": "Second"},
    ]
    assert titles._extract_ai_title(lines) == "Second"
    assert titles._extract_ai_title([{"type": "user"}]) is None


def test_extract_custom_title_last_wins_and_clear() -> None:
    assert titles._extract_custom_title(
        [{"type": "custom-title", "customTitle": "Renamed"}]
    ) == "Renamed"
    # A non-str customTitle is ignored (loop continues past it).
    assert titles._extract_custom_title([
        {"type": "custom-title", "customTitle": 123},
        {"type": "custom-title", "customTitle": "Renamed"},
    ]) == "Renamed"
    # A later empty custom-title is CC's "clear the rename" signal (last wins → "").
    assert titles._extract_custom_title([
        {"type": "custom-title", "customTitle": "Renamed"},
        {"type": "custom-title", "customTitle": ""},
    ]) == ""


def test_extract_session_title_custom_beats_ai() -> None:
    lines = [
        {"type": "ai-title", "aiTitle": "Auto Title"},
        {"type": "custom-title", "customTitle": "Human Title"},
    ]
    assert titles.extract_session_title(lines) == "Human Title"
    # No custom → the ai-title wins.
    assert titles.extract_session_title([{"type": "ai-title", "aiTitle": "Auto Title"}]) == "Auto Title"
    assert titles.extract_session_title([{"type": "user"}]) is None


def test_title_from_messages_first_nonxml_line() -> None:
    # XML line skipped, blank line skipped, first real line wins.
    msg = {"role": "user", "content_text": "<meta>skip</meta>\n\nreal first line\nsecond"}
    assert titles._title_from_messages([msg]) == "real first line"


def test_title_from_messages_empty_then_real() -> None:
    # A user turn with empty content_text and no command falls through to the next.
    msgs = [
        {"role": "user", "content_text": "", "provider_data": {}},
        {"role": "user", "content_text": "the real title"},
    ]
    assert titles._title_from_messages(msgs) == "the real title"


def test_title_from_messages_truncates_long_line() -> None:
    long = "x" * 150
    out = titles._title_from_messages([{"role": "user", "content_text": long}])
    assert out == "x" * 100 + "..."


def test_title_from_messages_bare_command() -> None:
    # Empty content_text but a bare slash-command in the raw line → command title.
    msg = _cc_command_msg("<command-name>/review</command-name>")
    assert titles._title_from_messages([msg]) == "/review"


def test_title_from_messages_skips_non_user_and_returns_none() -> None:
    # Assistant skipped; a user turn whose only line is XML yields nothing.
    msgs = [
        {"role": "assistant", "content_text": "ignored"},
        {"role": "user", "content_text": "<tag>only</tag>"},
    ]
    assert titles._title_from_messages(msgs) is None


def test_extract_title_prefers_session_title() -> None:
    assert titles.extract_title([{"type": "custom-title", "customTitle": "Named"}]) == "Named"


def test_extract_title_fallback_to_first_user_message() -> None:
    lines = [{"type": "user", "uuid": "u1", "message": {"role": "user", "content": "make it fast"}}]
    assert titles.extract_title(lines) == "make it fast"


def test_extract_title_default_when_no_messages() -> None:
    assert titles.extract_title([]) == "Claude Code Session"


def test_extract_title_parser_exception_falls_back(monkeypatch) -> None:
    class _Boom:
        def parse_export(self, *a, **k):
            raise RuntimeError("parser blew up")

    monkeypatch.setattr(titles, "ClaudeCodeParser", _Boom)
    # No session title present → parser path → raises → default.
    assert titles.extract_title([{"type": "user"}]) == "Claude Code Session"


def test_user_content_texts_str_and_blocks() -> None:
    assert titles._user_content_texts("just a string") == ["just a string"]
    blocks = [
        {"type": "text", "text": "a"},
        {"type": "image", "url": "x"},  # non-text block ignored
        {"type": "text", "text": "b"},
    ]
    assert titles._user_content_texts(blocks) == ["a", "b"]
    assert titles._user_content_texts(42) == []


def test_description_from_texts_branches() -> None:
    # First blank, then XML, then continuation, then the real line.
    texts = ["   \n<tag>x</tag>\nThis session is being continued from before\nkeep me"]
    assert titles._description_from_texts(texts) == "keep me"
    # Nothing usable → None.
    assert titles._description_from_texts(["   ", "<only/>"]) is None


def test_description_from_texts_truncates() -> None:
    long = "y" * 250
    out = titles._description_from_texts([long])
    assert out == "y" * 197 + "..."


def test_extract_description_from_first_user_line() -> None:
    lines = [
        {"type": "assistant", "message": {"content": "ignored"}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "the ask"}]}},
    ]
    assert titles.extract_description(lines) == "the ask"


def test_extract_description_none_when_absent() -> None:
    assert titles.extract_description([{"type": "assistant", "message": {"content": "x"}}]) is None


def test_extract_description_user_line_without_usable_text() -> None:
    # A user line whose only content is XML yields no description, and the scan
    # continues to the end returning None.
    lines = [{"type": "user", "message": {"content": [{"type": "text", "text": "<only/>"}]}}]
    assert titles.extract_description(lines) is None


# ══════════════════════════════════════════════════════════════════════════════
# exports.py — classification, loaders, dispatch, error preservation
# ══════════════════════════════════════════════════════════════════════════════

_CHATGPT_CONV = {
    "id": "gconv-1",
    "title": "GPT Chat",
    "create_time": 1767261600.0,
    "update_time": 1767261610.0,
    "current_node": "n2",
    "mapping": {
        "root": {"id": "root", "parent": None, "children": ["n1"], "message": None},
        "n1": {"id": "n1", "parent": "root", "children": ["n2"], "message": {
            "id": "n1", "author": {"role": "user"}, "create_time": 1767261600.0,
            "content": {"content_type": "text", "parts": ["hello from chatgpt"]},
            "status": "finished_successfully", "metadata": {},
        }},
        "n2": {"id": "n2", "parent": "n1", "children": [], "message": {
            "id": "n2", "author": {"role": "assistant"}, "create_time": 1767261605.0,
            "content": {"content_type": "text", "parts": ["hi from gpt"]},
            "status": "finished_successfully", "metadata": {"model_slug": "gpt-4o"},
        }},
    },
}


def _claude_conv(uuid="c1", name="Chat"):
    return {
        "uuid": uuid, "name": name,
        "created_at": "2026-01-01T10:00:00Z", "updated_at": "2026-01-01T10:00:10Z",
        "chat_messages": [
            {"uuid": "m1", "sender": "human", "text": "hi",
             "content": [{"type": "text", "text": "hi"}], "created_at": "2026-01-01T10:00:00Z"},
            {"uuid": "m2", "sender": "assistant", "text": "hello",
             "content": [{"type": "text", "text": "hello"}], "created_at": "2026-01-01T10:00:05Z"},
        ],
    }


def test_sniff_conversations_shape_variants() -> None:
    assert ex._sniff_conversations_shape("not a list") is None
    assert ex._sniff_conversations_shape([42, {"chat_messages": []}]) == "claude"
    assert ex._sniff_conversations_shape([{"mapping": {}}]) == "chatgpt"
    assert ex._sniff_conversations_shape([{"neither": 1}]) is None


def test_classify_conversations_json_read_error_returns_none() -> None:
    def _boom():
        raise ValueError("bad json")

    assert ex._classify_conversations_json(set(), _boom) is None


def test_classify_dir_with_neither_marker_is_none(tmp_path) -> None:
    d = tmp_path / "mystery"
    d.mkdir()
    (d / "random.txt").write_text("nothing", encoding="utf-8")
    assert ex.classify_export(d) is None


def test_classify_zip_xai_marker(tmp_path) -> None:
    z = tmp_path / "grok.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("export_data/u/prod-grok-backend.json", json.dumps({"conversations": []}))
    assert ex.classify_export(z) == "xai"


def test_classify_zip_unrecognized_is_none(tmp_path) -> None:
    z = tmp_path / "junk.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("readme.txt", "hello")
    assert ex.classify_export(z) is None


def test_claude_zip_full_bundle(archive_home) -> None:
    """A claude.ai ZIP with memories(list)/projects/users all present exercises the
    zip loader's success paths, including memories[0]."""
    init_db()
    z = archive_home / "claude_full.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json", json.dumps([_claude_conv("zc1", "Zip Chat")]))
        zf.writestr("memories.json", json.dumps([{"summary": "remember"}]))
        zf.writestr("projects.json", json.dumps([{"uuid": "p1"}]))
        zf.writestr("users.json", json.dumps([{"uuid": "u1"}]))
    result = ex.import_claude_ai_export(z)
    assert result.imported == 1
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude")).scalar_one()
        assert t.title == "Zip Chat"


def test_claude_zip_missing_sidecars_defaults(archive_home) -> None:
    """A claude.ai ZIP with only conversations.json — the memories/projects/users
    reads all KeyError and fall back to their defaults."""
    init_db()
    z = archive_home / "claude_min.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json", json.dumps([_claude_conv("zc2", "Bare Zip")]))
    result = ex.import_claude_ai_export(z)
    assert result.imported == 1


def test_claude_dir_missing_conversations_raises(tmp_path) -> None:
    d = tmp_path / "empty_export"
    d.mkdir()
    with pytest.raises(FileNotFoundError):
        ex._load_claude_export_from_dir(d)


def test_claude_dir_memories_list_and_projects_dir(archive_home) -> None:
    """Directory loader: memories.json as a list ([0] taken), a projects/ dir with a
    good file and a malformed one (skipped with a warning)."""
    init_db()
    d = archive_home / "claude_dir"
    d.mkdir()
    (d / "conversations.json").write_text(json.dumps([_claude_conv("dc1", "Dir Chat")]), encoding="utf-8")
    (d / "memories.json").write_text(json.dumps([{"summary": "m"}]), encoding="utf-8")
    (d / "users.json").write_text(json.dumps([{"uuid": "u"}]), encoding="utf-8")
    pdir = d / "projects"
    pdir.mkdir()
    (pdir / "p1.json").write_text(json.dumps({"uuid": "p1"}), encoding="utf-8")
    (pdir / "bad.json").write_text("{not json", encoding="utf-8")
    bundle = ex._load_claude_export(d)
    assert bundle["memories"] == {"summary": "m"}
    assert bundle["projects"] == [{"uuid": "p1"}]  # malformed skipped


def test_claude_dir_memories_dict_and_projects_file(tmp_path) -> None:
    """Directory loader: memories.json as a bare dict, projects via projects.json file."""
    d = tmp_path / "claude_dir2"
    d.mkdir()
    (d / "conversations.json").write_text(json.dumps([_claude_conv("dc2")]), encoding="utf-8")
    (d / "memories.json").write_text(json.dumps({"summary": "direct"}), encoding="utf-8")
    (d / "projects.json").write_text(json.dumps([{"uuid": "pf"}]), encoding="utf-8")
    bundle = ex._load_claude_export(d)
    assert bundle["memories"] == {"summary": "direct"}
    assert bundle["projects"] == [{"uuid": "pf"}]


def test_import_claude_ai_missing_path_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        ex.import_claude_ai_export(tmp_path / "nope")


def test_import_claude_ai_limit(archive_home) -> None:
    init_db()
    d = archive_home / "claude_many"
    d.mkdir()
    convs = [_claude_conv("cm1", "One"), _claude_conv("cm2", "Two")]
    (d / "conversations.json").write_text(json.dumps(convs), encoding="utf-8")
    result = ex.import_claude_ai_export(d, limit=1)
    assert result.processed == 1


def test_chatgpt_dir_missing_conversations_raises(tmp_path) -> None:
    d = tmp_path / "gpt_empty"
    d.mkdir()
    with pytest.raises(FileNotFoundError):
        ex._load_chatgpt_export(d)


def test_chatgpt_missing_path_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        ex.import_chatgpt_export(tmp_path / "nope")


def test_chatgpt_sort_ts_fallback() -> None:
    assert ex._chatgpt_sort_ts({"update_time": 5.0}) == 5.0
    assert ex._chatgpt_sort_ts({"create_time": 3}) == 3.0
    assert ex._chatgpt_sort_ts({"update_time": "not-a-number"}) == 0.0
    assert ex._chatgpt_sort_ts({}) == 0.0


def test_chatgpt_skips_no_id_and_empty_and_honours_limit(archive_home) -> None:
    """conv with no id → skipped (no source_id); conv whose mapping yields no messages
    → skipped; the valid conv imports. limit is respected."""
    init_db()
    valid = dict(_CHATGPT_CONV, id="gv1", title="Valid")
    no_id = {"title": "NoId", "mapping": _CHATGPT_CONV["mapping"]}  # missing id
    empty_map = {"id": "gempty", "title": "Empty",
                 "mapping": {"root": {"id": "root", "parent": None, "children": [], "message": None}}}
    d = archive_home / "gpt_mix"
    d.mkdir()
    (d / "conversations.json").write_text(json.dumps([valid, no_id, empty_map]), encoding="utf-8")
    result = ex.import_chatgpt_export(d)
    assert result.imported == 1
    assert result.skipped == 2


def test_chatgpt_limit(archive_home) -> None:
    init_db()
    c1 = dict(_CHATGPT_CONV, id="lg1")
    c2 = dict(_CHATGPT_CONV, id="lg2")
    d = archive_home / "gpt_limit"
    d.mkdir()
    (d / "conversations.json").write_text(json.dumps([c1, c2]), encoding="utf-8")
    result = ex.import_chatgpt_export(d, limit=1)
    assert result.processed == 1


def test_xai_dir_no_payload_raises(tmp_path) -> None:
    d = tmp_path / "xai_empty"
    d.mkdir()
    with pytest.raises(FileNotFoundError):
        ex._load_xai_export(d)


def test_xai_missing_path_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        ex.import_xai_export(tmp_path / "nope")


def _xai_payload(cid="xc1", title="Grok"):
    return {"conversations": [{
        "conversation": {"id": cid, "title": title, "create_time": "2026-01-01T10:00:00Z"},
        "responses": [
            {"response": {"_id": "r1", "sender": "human", "message": "q",
                          "create_time": "2026-01-01T10:00:00Z"}},
            {"response": {"_id": "r2", "sender": "ASSISTANT", "message": "a", "model": "grok-4",
                          "create_time": "2026-01-01T10:00:05Z"}},
        ],
    }]}


def test_xai_zip_import(archive_home) -> None:
    init_db()
    z = archive_home / "grok.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("export_data/u/prod-grok-backend.json", json.dumps(_xai_payload()))
    result = ex.import_xai_export(z)
    assert result.imported == 1


def test_xai_zip_no_marker_raises(tmp_path) -> None:
    z = tmp_path / "grok_bad.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("other.json", "{}")
    with pytest.raises(FileNotFoundError):
        ex._load_xai_export(z)


def test_xai_limit(archive_home) -> None:
    init_db()
    d = archive_home / "xai_many" / "export_data" / "u"
    d.mkdir(parents=True)
    payload = {"conversations": _xai_payload("xa1")["conversations"] + _xai_payload("xa2")["conversations"]}
    (d / "prod-grok-backend.json").write_text(json.dumps(payload), encoding="utf-8")
    result = ex.import_xai_export(archive_home / "xai_many", limit=1)
    assert result.processed == 1


def test_xai_parse_time_variants() -> None:
    # Mongo extended-JSON {$date: {$numberLong: ...}}
    dt = ex._xai_parse_time({"$date": {"$numberLong": "1700000000000"}})
    assert dt is not None and dt.tzinfo is not None
    # {$date: <epoch-ms number>}
    assert ex._xai_parse_time({"$date": 1700000000000}) is not None
    # {$date: None} → None
    assert ex._xai_parse_time({"$date": None}) is None
    # invalid inner → None (int() raises)
    assert ex._xai_parse_time({"$date": "not-a-number"}) is None
    # bool → None (before the int/float branch)
    assert ex._xai_parse_time(True) is None
    # epoch-ms number (> 1e11) and epoch-seconds number
    assert ex._xai_parse_time(1700000000000) is not None
    assert ex._xai_parse_time(1700000000) is not None
    # ISO string
    assert ex._xai_parse_time("2026-01-01T10:00:00Z") is not None
    # unhandled type → None
    assert ex._xai_parse_time(["list"]) is None


def test_import_one_skips_when_no_source_id(archive_home) -> None:
    init_db()
    res = ex.ExportImportResult()
    out = ex._import_one(
        res, parser_messages=lambda: [{"role": "user", "content_text": "x"}],
        source="claude", source_id="", title="T", source_metadata={},
        builder=ex.DefaultEventBuilder(), force=False,
    )
    assert out.skipped == 1


def test_import_one_new_thread_with_zero_events_discarded(archive_home, monkeypatch) -> None:
    """A brand-new conversation whose assemble yields zero events must not leave a
    ghost thread — the row + staged truth record are discarded."""
    init_db()
    monkeypatch.setattr(ex, "assemble_events", lambda *a, **k: (0, None))
    res = ex.ExportImportResult()
    out = ex._import_one(
        res, parser_messages=lambda: [{"role": "user", "content_text": "x"}],
        source="claude", source_id="ghost-1", title="Ghost", source_metadata={},
        builder=ex.DefaultEventBuilder(), force=False,
    )
    assert out.skipped == 1
    with get_session() as s:
        assert s.execute(select(Thread).where(Thread.source_id == "ghost-1")).first() is None


def test_import_one_stub_also_fails_counts_skipped(archive_home, monkeypatch) -> None:
    """If the real assemble raises AND the stub's assemble also raises, the
    conversation is counted skipped (never crashes the export)."""
    init_db()

    def _always_boom(*a, **k):
        raise RuntimeError("everything is on fire")

    monkeypatch.setattr(ex, "assemble_events", _always_boom)
    res = ex.ExportImportResult()
    out = ex._import_one(
        res, parser_messages=lambda: [{"role": "user", "content_text": "x"}],
        source="claude", source_id="doomed-1", title="Doomed", source_metadata={},
        builder=ex.DefaultEventBuilder(), force=False, raw={"k": "v"},
    )
    assert out.skipped == 1 and out.errored == 0


def test_import_export_dispatch(archive_home) -> None:
    init_db()
    # claude dir
    cdir = archive_home / "d_claude"
    cdir.mkdir()
    (cdir / "conversations.json").write_text(json.dumps([_claude_conv("disp-c")]), encoding="utf-8")
    (cdir / "users.json").write_text(json.dumps([{"uuid": "u"}]), encoding="utf-8")
    assert ex.import_export(cdir).imported == 1

    # chatgpt dir
    gdir = archive_home / "d_gpt"
    gdir.mkdir()
    (gdir / "conversations.json").write_text(json.dumps([dict(_CHATGPT_CONV, id="disp-g")]), encoding="utf-8")
    (gdir / "user.json").write_text(json.dumps({"id": "u"}), encoding="utf-8")
    assert ex.import_export(gdir).imported == 1

    # xai dir
    xdir = archive_home / "d_xai" / "export_data" / "u"
    xdir.mkdir(parents=True)
    (xdir / "prod-grok-backend.json").write_text(json.dumps(_xai_payload("disp-x")), encoding="utf-8")
    assert ex.import_export(archive_home / "d_xai").imported == 1


def test_import_export_unrecognized_raises(tmp_path) -> None:
    d = tmp_path / "mystery"
    d.mkdir()
    (d / "random.bin").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        ex.import_export(d)


def test_classify_plain_file_is_none(tmp_path) -> None:
    # A path that is neither a directory nor a zip classifies as nothing.
    f = tmp_path / "plain.txt"
    f.write_text("hi", encoding="utf-8")
    assert ex.classify_export(f) is None


def test_classify_zip_claude_by_siblings(tmp_path) -> None:
    z = tmp_path / "claude.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json", json.dumps([_claude_conv("cz")]))
        zf.writestr("users.json", json.dumps([{"uuid": "u"}]))
    assert ex.classify_export(z) == "claude"


def test_claude_dir_memories_non_conforming_defaults(tmp_path) -> None:
    # memories.json is a list of non-dicts → neither the list[0]-dict nor the dict
    # branch applies, so memories stays the empty default.
    d = tmp_path / "claude_dir3"
    d.mkdir()
    (d / "conversations.json").write_text(json.dumps([_claude_conv("dc3")]), encoding="utf-8")
    (d / "memories.json").write_text(json.dumps(["not", "dicts"]), encoding="utf-8")
    bundle = ex._load_claude_export(d)
    assert bundle["memories"] == {}


def test_chatgpt_zip_import(archive_home) -> None:
    init_db()
    z = archive_home / "gpt.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json", json.dumps([dict(_CHATGPT_CONV, id="zip-g")]))
    result = ex.import_chatgpt_export(z)
    assert result.imported == 1


def test_xai_textless_message_direct() -> None:
    # A response with no message text is preserved (not dropped) with its raw payload.
    bundle = {
        "conversation": {"id": "c1", "create_time": "2026-01-01T10:00:00Z"},
        "responses": [
            {"response": {"_id": "r2", "sender": "ASSISTANT", "message": "",
                          "model": "grok-image", "create_time": "2026-01-01T10:00:05Z",
                          "generated_image_urls": ["https://x/pic.png"]}},
        ],
    }
    msgs = ex._xai_conversation_messages(bundle)
    assert len(msgs) == 1
    assert msgs[0]["role"] not in ("user", "assistant", "system")
    assert "pic.png" in json.dumps(msgs[0])


def test_import_one_skips_empty_messages(archive_home) -> None:
    init_db()
    res = ex.ExportImportResult()
    out = ex._import_one(
        res, parser_messages=lambda: [], source="claude", source_id="nomsg-1",
        title="Empty", source_metadata={}, builder=ex.DefaultEventBuilder(), force=False,
    )
    assert out.skipped == 1


def test_import_one_skips_existing_without_force(archive_home) -> None:
    init_db()
    d = archive_home / "claude_reimport"
    d.mkdir()
    (d / "conversations.json").write_text(json.dumps([_claude_conv("ri1", "Reimport")]), encoding="utf-8")
    assert ex.import_claude_ai_export(d).imported == 1
    # Second run with no force → the existing conversation is skipped.
    again = ex.import_claude_ai_export(d)
    assert again.imported == 0 and again.skipped == 1


def test_import_claude_ai_force_reuses_thread(archive_home) -> None:
    """force=True on an already-imported conversation reuses the existing thread
    (a grown conversation lands new events; an unchanged one collapses to skipped)."""
    init_db()
    d = archive_home / "claude_force"
    d.mkdir()
    conv = _claude_conv("cf1", "Forced")
    path = d / "conversations.json"
    path.write_text(json.dumps([conv]), encoding="utf-8")
    assert ex.import_claude_ai_export(d).imported == 1

    # The conversation grows; a forced re-import lands the new turn in the SAME thread.
    conv["chat_messages"].append(
        {"uuid": "m3", "sender": "human", "text": "another",
         "content": [{"type": "text", "text": "another"}], "created_at": "2026-01-01T10:00:20Z"}
    )
    path.write_text(json.dumps([conv]), encoding="utf-8")
    grown = ex.import_claude_ai_export(d, force=True)
    assert grown.imported == 1 and grown.errored == 0
    with get_session() as s:
        threads = s.execute(select(Thread).where(Thread.source == "claude")).scalars().all()
    assert len(threads) == 1  # reused, not duplicated

    # Forcing an unchanged conversation collapses entirely (existing thread, 0 new).
    unchanged = ex.import_claude_ai_export(d, force=True)
    assert unchanged.imported == 0 and unchanged.skipped == 1 and unchanged.errored == 0


def test_import_one_failed_conversation_surfaces_stub(archive_home, monkeypatch) -> None:
    """Real assemble fails but the stub's assemble succeeds: the conversation is
    counted `errored` and a stub thread preserving the error is created."""
    init_db()
    real_assemble = ex.assemble_events
    calls = {"n": 0}

    def _fail_first(session, thread_id, messages, builder, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("assemble exploded")
        return real_assemble(session, thread_id, messages, builder, **kw)

    monkeypatch.setattr(ex, "assemble_events", _fail_first)
    res = ex.ExportImportResult()
    out = ex._import_one(
        res, parser_messages=lambda: [{"role": "user", "content_text": "hi",
                                       "content_blocks": [], "provider_message_id": "r1",
                                       "provider_data": {"provider": "claude"}}],
        source="claude", source_id="stub-1", title="Boom", source_metadata={"provider": "claude"},
        builder=ex.DefaultEventBuilder(), force=False, raw={"orig": "payload"},
    )
    assert out.errored == 1
    with get_session() as s:
        stub = s.execute(select(Thread).where(Thread.source_id == "stub-1:import-error")).scalar_one()
    assert stub.title.endswith("(import error)")
    # Re-running the stub creation reuses the existing stub thread (idempotent).
    ex._import_conversation_stub(
        "claude", "stub-1", "Boom", {"provider": "claude"},
        ex.DefaultEventBuilder(), {"orig": "payload"}, RuntimeError("again"),
    )
    with get_session() as s:
        stubs = s.execute(select(Thread).where(Thread.source_id == "stub-1:import-error")).scalars().all()
    assert len(stubs) == 1


# ══════════════════════════════════════════════════════════════════════════════
# opencode.py — DB scan + pure segment/normalize helpers
# ══════════════════════════════════════════════════════════════════════════════

_STALE_TS = 1700000000000  # 2023, always > 24h stale


def _make_oc_db(path, *, time_updated=_STALE_TS, messages, drop_tables=False) -> None:
    conn = sqlite3.connect(path)
    if drop_tables:
        conn.execute("CREATE TABLE session (id TEXT, time_updated INTEGER)")
    else:
        conn.execute(
            "CREATE TABLE session (id TEXT, project_id TEXT, parent_id TEXT, title TEXT, "
            "directory TEXT, time_created INTEGER, time_updated INTEGER, agent TEXT)"
        )
        conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        sid = "s1"
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
            (sid, "proj", None, "Sess", "/proj", 1700000000000, time_updated, "build"),
        )
        for i, (msg_id, msg_data, parts) in enumerate(messages):
            raw = msg_data if isinstance(msg_data, str) else json.dumps(msg_data)
            conn.execute("INSERT INTO message VALUES (?,?,?,?)", (msg_id, sid, 1700000000000 + i, raw))
            for j, (pid, pdata) in enumerate(parts):
                praw = pdata if isinstance(pdata, str) else json.dumps(pdata)
                conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                             (pid, msg_id, sid, 1700000000000 + i * 100 + j, praw))
    conn.commit()
    conn.close()


def test_opencode_db_missing_tables_returns_empty(archive_home) -> None:
    init_db()
    db = archive_home / "notopencode.db"
    _make_oc_db(db, messages=[], drop_tables=True)
    summary = import_opencode_db(db)
    assert summary.processed == 0 and summary.imported == 0


def test_opencode_db_session_failure_counted(archive_home, monkeypatch) -> None:
    """A session whose import raises is counted as failed and carried out with an
    error line (not silently swallowed)."""
    init_db()
    db = archive_home / "opencode.db"
    _make_oc_db(db, messages=[
        ("u1", {"role": "user", "time": {"created": 1700000000000}}, [("p0", {"type": "text", "text": "hi"})]),
    ])
    monkeypatch.setattr(oc, "import_opencode_from_payload", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    summary = import_opencode_db(db)
    assert summary.failed == 1 and summary.errors


def test_opencode_from_payload_with_session_and_long_title(archive_home) -> None:
    """import_opencode_from_payload with an explicit session imports directly; a
    >100-char title is truncated on the new thread."""
    init_db()
    long_title = "T" * 150
    with get_session() as s:
        res = import_opencode_from_payload(
            session_id="sess-x",
            session_data={"title": long_title, "time_created": 1700000000000},
            messages=[("u1", {"role": "user", "time": {"created": 1700000000000}})],
            parts_by_message={"u1": [{"type": "text", "text": "hi via payload"}]},
            session=s,
        )
        s.commit()
    assert res.events_created > 0 and res.is_new_thread
    with get_session() as s:
        t = s.get(Thread, res.thread_id)
        assert t.title.endswith("...") and len(t.title) == 100


def test_opencode_from_payload_empty_messages_noop(archive_home) -> None:
    init_db()
    res = import_opencode_from_payload(
        session_id="empty", session_data={"time_created": 1700000000000},
        messages=[], parts_by_message={},
    )
    assert res.events_created == 0 and res.thread_id == ""


def test_opencode_reimport_after_touch_no_new_messages(archive_home) -> None:
    """A session touched (time_updated bumped) but with no new messages re-resolves
    the existing thread via the watermark and imports nothing new."""
    init_db()
    db = archive_home / "opencode.db"
    _make_oc_db(db, messages=[
        ("u1", {"role": "user", "time": {"created": 1700000000000}}, [("p0", {"type": "text", "text": "hi"})]),
        ("a1", {"role": "assistant", "modelID": "m", "providerID": "p",
                "time": {"created": 1700000001000, "completed": 1700000002000}},
         [("p1", {"type": "text", "text": "done"})]),
    ])
    first = import_opencode_db(db)
    assert first.imported == 1
    # Bump time_updated far into the future so the session reads as "changed"...
    future = int(datetime.now(timezone.utc).timestamp() * 1000) + 3_600_000
    conn = sqlite3.connect(db)
    conn.execute("UPDATE session SET time_updated = ?", (future,))
    conn.commit()
    conn.close()
    # ...but there are no new messages, so nothing new lands.
    second = import_opencode_db(db)
    assert second.imported == 0 and second.events_created == 0


def test_opencode_reimport_unchanged_is_noop(archive_home) -> None:
    """A byte-identical re-scan of an unchanged session (same time_updated) skips it
    wholesale via the import_state cursor."""
    init_db()
    db = archive_home / "opencode.db"
    _make_oc_db(db, messages=[
        ("u1", {"role": "user", "time": {"created": 1700000000000}}, [("p0", {"type": "text", "text": "hi"})]),
        ("a1", {"role": "assistant", "modelID": "m", "providerID": "p",
                "time": {"created": 1700000001000, "completed": 1700000002000}},
         [("p1", {"type": "text", "text": "done"})]),
    ])
    assert import_opencode_db(db).imported == 1
    second = import_opencode_db(db)
    assert second.imported == 0 and second.events_created == 0


def test_opencode_resolve_existing_thread_without_watermark(archive_home) -> None:
    """After a reindex loses the import_state cursor, a re-import must resolve the
    still-present thread by source rather than create a duplicate."""
    init_db()
    db = archive_home / "opencode.db"
    _make_oc_db(db, messages=[
        ("u1", {"role": "user", "time": {"created": 1700000000000}}, [("p0", {"type": "text", "text": "hi"})]),
        ("a1", {"role": "assistant", "modelID": "m", "providerID": "p",
                "time": {"created": 1700000001000, "completed": 1700000002000}},
         [("p1", {"type": "text", "text": "done"})]),
    ])
    import_opencode_db(db)
    with get_session() as s:
        s.execute(delete(ImportState).where(ImportState.source == "opencode"))
        s.commit()
    # Re-import: watermark gone, existing thread found by source → no new thread.
    import_opencode_db(db)
    with get_session() as s:
        threads = s.execute(select(Thread).where(Thread.source == "opencode")).scalars().all()
    assert len(threads) == 1


def test_opencode_build_messages_skips_non_dict() -> None:
    # A message whose data isn't a dict is skipped.
    assert oc._build_opencode_messages([("m1", ["not", "a", "dict"])], {}) == []


def test_opencode_session_stale() -> None:
    now = datetime.now(timezone.utc).timestamp() * 1000
    assert oc._opencode_session_stale(_STALE_TS, now) is True
    assert oc._opencode_session_stale(now, now) is False
    assert oc._opencode_session_stale("not-a-number", now) is False


def test_opencode_raw_text() -> None:
    assert oc._opencode_raw_text("literal") == "literal"
    assert oc._opencode_raw_text({"k": 1}) == repr({"k": 1})


def test_opencode_tool_segment() -> None:
    seg = oc._opencode_tool_segment({
        "callID": "c1", "tool": "bash",
        "state": {"input": {"cmd": "ls"}, "output": "files", "status": "error",
                  "time": {"start": 1, "end": 2}},
    })
    assert seg == {"kind": "tool", "call_id": "c1", "name": "bash",
                   "input": {"cmd": "ls"}, "output": "files", "is_error": True,
                   "ts": 1, "end_ts": 2}


def test_opencode_assistant_segments_all_kinds() -> None:
    parts = [
        {"type": "reasoning", "text": "thinking...", "time": {"start": 1}},
        {"type": "text", "text": "answer", "time": {"start": 2}},
        {"type": "text", "text": ""},  # empty → seg None, dropped
        {"type": "tool", "callID": "c", "tool": "grep", "state": {}},
        {"type": "snapshot", "snapshot": "snap", "time": {"start": 3}},  # unknown → raw
    ]
    segs = oc._opencode_assistant_segments(parts)
    kinds = [s["kind"] for s in segs]
    assert kinds == ["thinking", "text", "tool", "raw"]
    assert segs[-1]["part_type"] == "snapshot"


def test_opencode_model() -> None:
    assert oc._opencode_model({"modelID": "gpt", "providerID": "oai"}) == "oai/gpt"
    assert oc._opencode_model({"modelID": "gpt"}) == "gpt"
    assert oc._opencode_model({}) == "opencode"


def test_opencode_stringify() -> None:
    assert oc._opencode_stringify("s") == "s"
    assert oc._opencode_stringify(None) == ""
    assert oc._opencode_stringify({"a": 1}) == json.dumps({"a": 1})


def test_opencode_to_normalized_assistant_blocks() -> None:
    msg = {
        "role": "assistant", "started_at": 1700000000000, "model": "m",
        "segments": [
            {"kind": "thinking", "text": "hmm", "ts": 1700000000000},
            {"kind": "tool", "call_id": "c1", "name": "bash",
             "input": {"x": 1}, "output": "out", "is_error": False,
             "ts": 1700000000000, "end_ts": 1700000001000},
            {"kind": "raw", "part_type": "snapshot", "raw": {"s": 1}, "ts": 1700000000000},
        ],
        "incomplete": True,
    }
    norm = oc._opencode_to_normalized(msg)
    types = [b["type"] for b in norm["content_blocks"]]
    assert "thinking" in types
    assert "tool_use" in types and "tool_result" in types
    assert "snapshot" in types
    assert norm["provider_data"]["stop_reason"] == "incomplete"


# ══════════════════════════════════════════════════════════════════════════════
# claude_science.py — frame synthesis, title derivation, DB scan
# ══════════════════════════════════════════════════════════════════════════════

_CS_FRAME_COLS = (
    "id", "parent_frame_id", "root_frame_id", "agent_name", "conversation_type",
    "name", "task_summary", "model", "project_id", "created_at",
)


def _make_cs_db(path, *, with_frames=True):
    conn = sqlite3.connect(path)
    if with_frames:
        cols = ", ".join(f"{c} TEXT" if c != "created_at" else "created_at INTEGER" for c in _CS_FRAME_COLS)
        conn.execute(f"CREATE TABLE frames ({cols})")
        conn.execute("CREATE TABLE frame_messages (frame_id TEXT, idx INTEGER, msg_json TEXT, "
                     "PRIMARY KEY(frame_id, idx))")
    else:
        conn.execute("CREATE TABLE other (id TEXT)")
    return conn


def _add_cs_frame(conn, **kw):
    row = {c: kw.get(c) for c in _CS_FRAME_COLS}
    ph = ", ".join("?" for _ in _CS_FRAME_COLS)
    conn.execute(f"INSERT INTO frames ({', '.join(_CS_FRAME_COLS)}) VALUES ({ph})",
                 [row[c] for c in _CS_FRAME_COLS])


def _add_cs_messages(conn, frame_id, raw_rows):
    """raw_rows: list of already-serialized msg_json strings."""
    conn.executemany(
        "INSERT INTO frame_messages (frame_id, idx, msg_json) VALUES (?, ?, ?)",
        [(frame_id, i, raw) for i, raw in enumerate(raw_rows)],
    )


def _user_msg(text, uuid):
    return json.dumps({"role": "user", "content": [{"type": "text", "text": text}], "_uuid": uuid})


def _assistant_msg(text, uuid):
    return json.dumps({"role": "assistant", "content": [{"type": "text", "text": text}], "_uuid": uuid})


def test_cs_synthesize_line_skips_non_conversational() -> None:
    # non user/assistant role → None
    assert cs._synthesize_line("f", 0, {"role": "system", "content": []}, None, 0) is None
    # content None → None
    assert cs._synthesize_line("f", 0, {"role": "user", "content": None}, None, 0) is None
    # a real user line synthesizes an envelope
    line = cs._synthesize_line("f", 2, {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
                               "claude-opus", 1700000000000)
    assert line["type"] == "assistant" and line["sessionId"] == "f"
    assert line["message"]["model"] == "claude-opus"


def test_cs_frame_title_from_name_and_task_summary() -> None:
    assert cs._frame_title({"name": "Named Frame"}, []) == "Named Frame"
    assert cs._frame_title({"name": "", "task_summary": "Do the thing"}, []) == "Do the thing"


def test_cs_frame_title_derived_from_lines() -> None:
    lines = [cs._synthesize_line("f", 0, {"role": "user", "content": [{"type": "text", "text": "Explore the genome"}]},
                                 None, 1700000000000)]
    assert cs._frame_title({"name": "", "task_summary": ""}, lines) == "Explore the genome"


def test_cs_frame_title_agent_fallback() -> None:
    # No name/summary and no derivable title → agent-named label.
    assert cs._frame_title({"name": "", "task_summary": "", "agent_name": "reviewer_bot"}, []) == "Reviewer Bot Session"
    # No agent either → generic label.
    assert cs._frame_title({"name": "", "task_summary": ""}, []) == "Claude Science Session"


def test_cs_frame_title_subagent_prefix_and_truncation() -> None:
    long = "N" * 150
    out = cs._frame_title({"name": long, "parent_frame_id": "root"}, [])
    assert out.startswith("🤖 ") and len(out) <= 100


def test_cs_import_frame_with_session_direct(archive_home) -> None:
    init_db()
    frame = {"id": "f1", "parent_frame_id": None, "root_frame_id": None, "agent_name": "OPERON",
             "conversation_type": "agent", "name": "Direct Frame", "task_summary": None,
             "model": "claude-opus", "project_id": "proj_real", "created_at": 1700000000000}
    rows = [(0, _user_msg("hi", "u1")), (1, _assistant_msg("yo", "a1"))]
    with get_session() as s:
        res = import_claude_science_frame("orgX", frame, rows, session=s)
        s.commit()
    assert res.events_created > 0 and res.is_new_thread


def test_cs_db_imports_subagent_frame_metadata(archive_home, tmp_path) -> None:
    """A child (subagent) frame imports as a hidden `system` thread with 🤖 title and
    subagent metadata (parent/root frame ids)."""
    init_db()
    db = tmp_path / "cs_sub.db"
    conn = _make_cs_db(db)
    _add_cs_frame(conn, id="root1", conversation_type="agent", name="Root",
                  model="claude-opus", project_id="proj_real", created_at=1700000000000)
    _add_cs_messages(conn, "root1", [_user_msg("q", "u1"), _assistant_msg("a", "a1")])
    _add_cs_frame(conn, id="child1", parent_frame_id="root1", root_frame_id="root1",
                  agent_name="REVIEWER", conversation_type="agent", task_summary="Review",
                  model="claude-opus", project_id="proj_real", created_at=1700000100000)
    _add_cs_messages(conn, "child1", [_user_msg("review", "u2"), _assistant_msg("ok", "a2")])
    conn.commit()
    conn.close()
    import_claude_science_db(db, "org")
    with get_session() as s:
        child = s.execute(
            select(Thread).where(Thread.source_id == "org:child1")
        ).scalar_one()
    assert child.thread_type == "system"
    assert child.title.startswith("🤖")
    assert child.source_metadata["is_subagent"] is True
    assert child.source_metadata["parent_frame_id"] == "root1"


def test_cs_db_reimport_is_noop(archive_home, tmp_path) -> None:
    """A re-scan with the watermark intact and no new messages imports nothing
    (the start-index >= total early return)."""
    init_db()
    db = tmp_path / "cs_reimport.db"
    conn = _make_cs_db(db)
    _add_cs_frame(conn, id="f1", conversation_type="agent", name="Chat",
                  model="claude-opus", project_id="proj_real", created_at=1700000000000)
    _add_cs_messages(conn, "f1", [_user_msg("q", "u1"), _assistant_msg("a", "a1")])
    conn.commit()
    conn.close()
    assert import_claude_science_db(db, "org").imported == 1
    assert import_claude_science_db(db, "org").events_created == 0


def test_cs_db_incremental_append(archive_home, tmp_path) -> None:
    """A frame that grows imports only the new turn into the existing thread."""
    init_db()
    db = tmp_path / "cs_incr.db"
    conn = _make_cs_db(db)
    _add_cs_frame(conn, id="f1", conversation_type="agent", name="Chat",
                  model="claude-opus", project_id="proj_real", created_at=1700000000000)
    _add_cs_messages(conn, "f1", [_user_msg("q1", "u1"), _assistant_msg("a1", "a1")])
    conn.commit()
    conn.close()
    import_claude_science_db(db, "org")
    with get_session() as s:
        n1 = len(s.execute(select(Event.id)).scalars().all())
    # The frame grows by a turn.
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO frame_messages (frame_id, idx, msg_json) VALUES (?, ?, ?)",
        [("f1", 2, _user_msg("q2", "u2")), ("f1", 3, _assistant_msg("a2", "a2"))],
    )
    conn.commit()
    conn.close()
    scan = import_claude_science_db(db, "org")
    assert scan.events_created > 0
    with get_session() as s:
        contents = s.execute(select(Event.payload)).scalars().all()
        threads = s.execute(select(Thread).where(Thread.source == "claude-science")).scalars().all()
        n2 = len(s.execute(select(Event.id)).scalars().all())
    assert any(p.get("content") == "q2" for p in contents)
    assert len(threads) == 1  # imported into the SAME thread, no duplicate
    assert n2 == n1 + scan.events_created  # pure append: nothing re-imported or dropped


def test_cs_db_no_frames_table_returns_empty(archive_home, tmp_path) -> None:
    init_db()
    db = tmp_path / "notcs.db"
    conn = _make_cs_db(db, with_frames=False)
    conn.commit()
    conn.close()
    summary = import_claude_science_db(db, "org")
    assert summary.processed == 0 and summary.imported == 0


def test_cs_db_frame_without_messages_skipped(archive_home, tmp_path) -> None:
    init_db()
    db = tmp_path / "cs_nomsg.db"
    conn = _make_cs_db(db)
    _add_cs_frame(conn, id="empty1", conversation_type="agent", name="No messages",
                  project_id="proj_real", created_at=1700000000000)
    conn.commit()
    conn.close()
    summary = import_claude_science_db(db, "org")
    assert summary.processed == 0  # frame with no message rows is skipped before processing


def test_cs_db_bad_and_nonconversational_rows_preserved_prefix(archive_home, tmp_path) -> None:
    """A frame with a bad-JSON row, a JSON-null row, and a system-role message (all
    unsynthesizable) still imports its real user/assistant turns."""
    init_db()
    db = tmp_path / "cs_mixed.db"
    conn = _make_cs_db(db)
    _add_cs_frame(conn, id="mixed1", conversation_type="agent", name="Mixed",
                  model="claude-opus", project_id="proj_real", created_at=1700000000000)
    _add_cs_messages(conn, "mixed1", [
        "{not valid json",                                             # parse error → skipped
        "null",                                                        # not a dict → skipped
        json.dumps({"role": "system", "content": [{"type": "text", "text": "sys"}]}),  # line None
        _user_msg("real question", "u1"),
        _assistant_msg("real answer", "a1"),
    ])
    conn.commit()
    conn.close()
    summary = import_claude_science_db(db, "org")
    assert summary.imported == 1 and summary.events_created > 0
    with get_session() as s:
        contents = s.execute(select(Event.payload)).scalars().all()
    assert any(p.get("content") == "real question" for p in contents)


def test_cs_db_all_nonconversational_discards_thread(archive_home, tmp_path) -> None:
    """A frame that synthesizes zero importable lines creates no lingering thread."""
    init_db()
    db = tmp_path / "cs_allsys.db"
    conn = _make_cs_db(db)
    _add_cs_frame(conn, id="sysonly", conversation_type="agent", name="Sys only",
                  project_id="proj_real", created_at=1700000000000)
    _add_cs_messages(conn, "sysonly", [
        json.dumps({"role": "system", "content": [{"type": "text", "text": "a"}]}),
        json.dumps({"role": "system", "content": [{"type": "text", "text": "b"}]}),
    ])
    conn.commit()
    conn.close()
    summary = import_claude_science_db(db, "org")
    assert summary.imported == 0
    with get_session() as s:
        assert s.execute(select(Thread).where(Thread.source == "claude-science")).first() is None


def test_cs_db_adopts_unwatermarked_thread_on_reindex(archive_home, tmp_path) -> None:
    """A thread with events but no import_state (post reindex) is adopted at EOF,
    not re-imported from scratch."""
    init_db()
    db = tmp_path / "cs_adopt.db"
    conn = _make_cs_db(db)
    _add_cs_frame(conn, id="adopt1", conversation_type="agent", name="Adopt me",
                  model="claude-opus", project_id="proj_real", created_at=1700000000000)
    _add_cs_messages(conn, "adopt1", [_user_msg("q", "u1"), _assistant_msg("a", "a1")])
    conn.commit()
    conn.close()
    import_claude_science_db(db, "org")
    with get_session() as s:
        n1 = len(s.execute(select(Event.id)).scalars().all())
        s.execute(delete(ImportState).where(ImportState.source == "claude-science"))
        s.commit()
    second = import_claude_science_db(db, "org")
    assert second.events_created == 0
    with get_session() as s:
        assert len(s.execute(select(Event.id)).scalars().all()) == n1


def test_cs_db_frame_failure_counted(archive_home, tmp_path, monkeypatch) -> None:
    init_db()
    db = tmp_path / "cs_fail.db"
    conn = _make_cs_db(db)
    _add_cs_frame(conn, id="boom1", conversation_type="agent", name="Boom",
                  model="claude-opus", project_id="proj_real", created_at=1700000000000)
    _add_cs_messages(conn, "boom1", [_user_msg("q", "u1"), _assistant_msg("a", "a1")])
    conn.commit()
    conn.close()
    monkeypatch.setattr(cs, "import_claude_science_frame",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("frame boom")))
    summary = import_claude_science_db(db, "org")
    assert summary.failed == 1 and summary.errors


# ══════════════════════════════════════════════════════════════════════════════
# antigravity.py — kind/content/model/title helpers + message assembly
# ══════════════════════════════════════════════════════════════════════════════


def test_ag_kind_variants() -> None:
    assert ag._antigravity_kind({"source": "USER_EXPLICIT"}) == "user"
    assert ag._antigravity_kind({"type": "USER_INPUT"}) == "user"
    assert ag._antigravity_kind({"source": "MODEL", "type": "PLANNER_RESPONSE"}) == "assistant"
    assert ag._antigravity_kind({"source": "MODEL", "type": "TOOL_RESULT"}) == "tool_result"
    assert ag._antigravity_kind({"type": "ERROR_MESSAGE"}) == "error"
    assert ag._antigravity_kind({"source": "OTHER"}) is None


def test_ag_content_user_request_extraction() -> None:
    line = {"source": "USER_EXPLICIT", "type": "USER_INPUT",
            "content": "<USER_REQUEST>do the thing</USER_REQUEST>"}
    assert ag._antigravity_content(line) == "do the thing"
    # A user line without the wrapper returns its stripped content.
    assert ag._antigravity_content({"source": "USER_EXPLICIT", "content": "  bare  "}) == "bare"
    # Non-string / empty content → "".
    assert ag._antigravity_content({"content": None}) == ""
    assert ag._antigravity_content({"content": "   "}) == ""


def test_ag_tool_args_unwrap() -> None:
    assert ag._antigravity_tool_args("not a dict") == {}
    out = ag._antigravity_tool_args({
        "obj": json.dumps({"a": 1}),   # str → json-decoded
        "raw": "plain string",          # str that isn't JSON → kept verbatim
        "num": 5,                        # non-str → passthrough
    })
    assert out == {"obj": {"a": 1}, "raw": "plain string", "num": 5}


def test_ag_model_extraction() -> None:
    lines = [
        {"content": None},  # non-str skipped
        {"content": "changed setting `Model Selection` from A to gemini-3-pro. now"},
    ]
    assert ag._antigravity_model(lines) == "gemini-3-pro"
    assert ag._antigravity_model([{"content": "nothing"}]) == "gemini"


def test_ag_has_importable_content_tool_only_assistant() -> None:
    lines = [{"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "",
              "tool_calls": [{"name": "run"}]}]
    assert ag._antigravity_has_importable_content(lines) is True
    assert ag._antigravity_has_importable_content([{"source": "MODEL", "type": "PLANNER_RESPONSE"}]) is False


def test_ag_title_variants() -> None:
    user = {"source": "USER_EXPLICIT", "type": "USER_INPUT",
            "content": "<USER_REQUEST>first line\nsecond</USER_REQUEST>"}
    assert ag._antigravity_title([user]) == "first line"
    # Long first line is truncated.
    longline = {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "z" * 150}
    assert ag._antigravity_title([longline]) == "z" * 97 + "..."
    # A user turn with empty content is skipped; the next user turn titles it.
    empty_then_real = [
        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": ""},
        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "the ask"},
    ]
    assert ag._antigravity_title(empty_then_real) == "the ask"
    # No user turn at all → default label (also exercises the non-user skip branch).
    assert ag._antigravity_title([{"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "x"}]) == "Antigravity Session"


def test_ag_build_messages_orphan_outcome_and_skips() -> None:
    """_build_antigravity_messages: a non-dict line is skipped, an empty unmodeled
    dict is dropped, an unmodeled step is preserved, a user turn with no content is
    skipped, and a tool outcome with no preceding call is kept as an orphan result."""
    # ``all_lines`` must be all dicts (``_antigravity_model`` scans them unguarded);
    # the non-dict lives only in ``new_lines``, exercising the main loop's guard.
    dict_lines = [
        {},  # kind None + falsy dict → dropped (the `if line:` false branch)
        {"source": "SYSTEM", "type": "SETTINGS_CHANGE", "content": "some system note",
         "created_at": "2026-01-01T10:00:00Z"},  # unmodeled → preserved as role=unknown
        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": ""},  # user, no content → skipped
        {"source": "USER_EXPLICIT", "type": "USER_INPUT",
         "content": "<USER_REQUEST>go</USER_REQUEST>", "created_at": "2026-01-01T10:00:01Z"},
        # A tool outcome with no preceding tool_call → orphan-paired.
        {"source": "MODEL", "type": "TOOL_RESULT", "content": "orphan output",
         "created_at": "2026-01-01T10:00:02Z"},
    ]
    new_lines = ["not-a-dict", *dict_lines]
    msgs = ag._build_antigravity_messages(new_lines, dict_lines, "ag-src")
    roles = [m["role"] for m in msgs]
    assert "user" in roles
    assert "unknown" in roles  # the unmodeled SETTINGS_CHANGE was preserved
    blob = json.dumps(msgs)
    assert "some system note" in blob
    # The orphan outcome landed as a tool_result with a synthetic orphan id.
    assert "orphan output" in blob and "agorphan-" in blob


def test_ag_build_messages_empty_assistant_dropped() -> None:
    """An assistant step with neither content nor tool_calls produces no turn."""
    lines = [{"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "",
              "created_at": "2026-01-01T10:00:00Z"}]
    assert ag._build_antigravity_messages(lines, lines, "ag-src") == []


def test_ag_build_messages_toolcall_without_text() -> None:
    """An assistant step with tool_calls but no natural-language content emits only
    the tool_use block (no empty text block)."""
    lines = [{"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "",
              "created_at": "2026-01-01T10:00:00Z",
              "tool_calls": [{"name": "search", "args": {"q": "x"}}]}]
    msgs = ag._build_antigravity_messages(lines, lines, "ag-src")
    assert len(msgs) == 1
    block_types = [b["type"] for b in msgs[0]["content_blocks"]]
    assert block_types == ["tool_use"]  # no text block since content was empty


def test_ag_error_step_imports_as_tool_result(archive_home) -> None:
    """An ERROR_MESSAGE step is mapped to a tool outcome flagged is_error."""
    init_db()
    f = archive_home / "transcript.jsonl"
    lines = [
        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "2026-01-01T10:00:00Z",
         "content": "<USER_REQUEST>run it</USER_REQUEST>"},
        {"source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": "2026-01-01T10:00:01Z",
         "content": "trying", "tool_calls": [{"name": "shell", "args": {}}]},
        {"type": "ERROR_MESSAGE", "created_at": "2026-01-01T10:00:02Z", "content": "it failed"},
    ]
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    res = import_antigravity_session_incremental(f, "ag-error")
    assert res.events_created > 0
    # An is_error tool outcome is emitted as a `tool_execution_error` event, paired
    # to the preceding call, carrying the error text.
    with get_session() as s:
        payloads = s.execute(
            select(Event.payload).where(Event.event_type == "tool_execution_error")
        ).scalars().all()
    assert payloads and payloads[0]["error"] == "it failed"
    assert payloads[0]["tool_call_id"] == "agtool-0"
