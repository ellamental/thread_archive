"""The reality-integrity incident harness (:mod:`thread_archive._evals.incidents`).

Three layers: catalogue loading refuses anything it can't trust (a typoed field
must fail loud, not silently weaken a guard), the per-tier pass conditions are
pinned against a scripted search injected through the public seam, and the
``archive incidents`` verb runs end-to-end over a real imported fixture archive
— catalogue on disk, production search, exit codes.
"""

from __future__ import annotations

import json

import pytest

from thread_archive import _api as api
from thread_archive._evals import incidents as inc
from thread_archive.cli import main

from .helpers import cc_assistant, cc_user, write_jsonl

# coverage tag: _evals


def _write_catalogue(path, *cases) -> str:
    path.write_text("\n".join(json.dumps(c) for c in cases) + "\n", encoding="utf-8")
    return str(path)


def _case(**over) -> dict:
    base = {"slug": "one", "tier": "recall", "query": "find me", "must_surface": [7]}
    base.update(over)
    return base


# ── load_catalogue: what parses ──────────────────────────────────────────────


def test_load_full_catalogue(tmp_path) -> None:
    cat = tmp_path / "cat.jsonl"
    cat.write_text(
        json.dumps({"slug": "a", "tier": "recall", "query": "q a", "must_surface": [1, 2],
                    "confidence": "certain", "original_failure": "flooded", "thread": 99})
        + "\n\n"  # blank lines are allowed
        + json.dumps({"slug": "b", "tier": "capability", "query": "q b", "content_type": "tool"})
        + "\n"
        + json.dumps({"slug": "c", "tier": "record", "query": "q c", "must_surface": [3]})
        + "\n"
        + json.dumps({"slug": "d", "tier": "unresolved", "query": "q d", "must_surface": [4],
                      "notes": "open until found or ruled headspace"})
        + "\n",
        encoding="utf-8",
    )
    cases = inc.load_catalogue(cat)
    assert [c.slug for c in cases] == ["a", "b", "c", "d"]
    a, b, c, d = cases
    assert a.must_surface == frozenset({1, 2})
    assert a.annotations == {"confidence": "certain", "original_failure": "flooded", "thread": 99}
    assert b.content_type == "tool" and b.must_surface == frozenset()
    assert c.tier == "record"
    assert d.annotations["notes"].startswith("open until")


@pytest.mark.parametrize(
    "bad, msg",
    [
        (_case(slug=""), "'slug' must be a non-empty string"),
        (_case(tier="regression"), "not one of"),
        (_case(query="  "), "'query' must be a non-empty string"),
        (_case(must_surfase=[7]), "unknown field"),
        (_case(must_surface=[]), "at least one"),
        (_case(must_surface=[7, True]), "list of integer thread ids"),
        (_case(must_surface="7"), "list of integer thread ids"),
        (_case(tier="record", must_surface=[1, 2]), "exactly one thread"),
        (_case(tier="unresolved", must_surface=[]), "exactly one thread"),
        (_case(content_type=""), "'content_type' must be a non-empty string"),
    ],
    ids=["empty-slug", "bad-tier", "blank-query", "typoed-field", "recall-no-ids",
         "bool-id", "string-ids", "record-two-ids", "unresolved-no-id", "empty-content-type"],
)
def test_load_refuses_bad_incident(tmp_path, bad, msg) -> None:
    cat = _write_catalogue(tmp_path / "cat.jsonl", bad)
    with pytest.raises(inc.CatalogueError, match=msg):
        inc.load_catalogue(cat)


def test_load_refuses_duplicate_slug(tmp_path) -> None:
    cat = _write_catalogue(tmp_path / "cat.jsonl", _case(), _case())
    with pytest.raises(inc.CatalogueError, match="duplicate slug 'one'"):
        inc.load_catalogue(cat)


def test_load_names_the_bad_line(tmp_path) -> None:
    cat = tmp_path / "cat.jsonl"
    cat.write_text(json.dumps(_case()) + "\nnot json\n", encoding="utf-8")
    with pytest.raises(inc.CatalogueError, match="cat.jsonl:2: not valid JSON"):
        inc.load_catalogue(cat)


def test_load_refuses_non_object_line(tmp_path) -> None:
    cat = tmp_path / "cat.jsonl"
    cat.write_text("[1, 2]\n", encoding="utf-8")
    with pytest.raises(inc.CatalogueError, match="must be a JSON object"):
        inc.load_catalogue(cat)


def test_load_refuses_empty_catalogue(tmp_path) -> None:
    cat = tmp_path / "cat.jsonl"
    cat.write_text("\n\n", encoding="utf-8")
    with pytest.raises(inc.CatalogueError, match="no incidents"):
        inc.load_catalogue(cat)


# ── evaluate: the per-tier pass conditions, against a scripted search ─────────


class ScriptedSearch:
    """A search stand-in injected through the harness's public seam. Serves the
    scripted hits and records each call so parameter threading is assertable."""

    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def __call__(self, query, *, content_type=None, thread_id=None, limit=inc.RECALL_LIMIT):
        self.calls.append(
            {"query": query, "content_type": content_type, "thread_id": thread_id, "limit": limit}
        )
        return self.hits


def _incident(**over) -> inc.Incident:
    fields = {"slug": "x", "tier": "recall", "query": "q", "must_surface": frozenset({7})}
    fields.update(over)
    return inc.Incident(**fields)


def test_recall_passes_on_any_required_thread() -> None:
    search = ScriptedSearch([{"thread_id": 7}, {"thread_id": 8}])
    ok, detail, _ = inc.evaluate(_incident(must_surface=frozenset({7, 99})), search)
    assert ok
    assert "got=[7, 8]" in detail
    assert search.calls == [{"query": "q", "content_type": None, "thread_id": None, "limit": 25}]


def test_recall_fails_when_buried() -> None:
    search = ScriptedSearch([{"thread_id": 1}, {"thread_id": 2}])
    ok, detail, _ = inc.evaluate(_incident(), search)
    assert not ok
    assert "must_surface(any of)=[7]" in detail


def test_capability_passes_on_any_hits_at_all() -> None:
    # The cliff is zero → many: hits pass even when the informational incident
    # threads don't rank (recency makes any one old thread's position flaky).
    search = ScriptedSearch([{"thread_id": 1}])
    case = _incident(tier="capability", content_type="tool", must_surface=frozenset({7}))
    ok, detail, _ = inc.evaluate(case, search)
    assert ok
    assert "incident_threads_surfaced=[]" in detail
    assert search.calls[0]["content_type"] == "tool"


def test_capability_fails_on_zero_hits() -> None:
    ok, detail, _ = inc.evaluate(_incident(tier="capability", must_surface=frozenset()), ScriptedSearch([]))
    assert not ok
    assert "hits=0" in detail


def test_record_searches_thread_scoped() -> None:
    search = ScriptedSearch([{"thread_id": 7}])
    ok, detail, _ = inc.evaluate(_incident(tier="record"), search)
    assert ok
    assert search.calls[0]["thread_id"] == 7
    assert "thread_scoped=7" in detail


def test_record_fails_when_phrase_gone() -> None:
    ok, _, _ = inc.evaluate(_incident(tier="record"), ScriptedSearch([]))
    assert not ok


def test_evaluate_threads_the_limit() -> None:
    search = ScriptedSearch([{"thread_id": 7}])
    inc.evaluate(_incident(), search, limit=3)
    assert search.calls[0]["limit"] == 3


def test_run_catalogue_summary_counts() -> None:
    cases = [
        _incident(slug="good"),
        _incident(slug="bad", must_surface=frozenset({99})),
        _incident(slug="open", tier="unresolved", must_surface=frozenset({7})),
    ]
    r = inc.run_catalogue(cases, ScriptedSearch([{"thread_id": 7}]))
    assert (r["ok"], r["passed"], r["failed"], r["open"]) == (False, 2, 1, 1)
    by_slug = {res["slug"]: res for res in r["results"]}
    assert by_slug["good"]["ok"] and by_slug["open"]["ok"] and not by_slug["bad"]["ok"]


# ── the verb, end-to-end over a real fixture archive ─────────────────────────


@pytest.fixture
def fixture_archive(tmp_path):
    """Two conversations + one tool call, imported for real; returns thread ids."""
    a = tmp_path / "sess-a.jsonl"
    write_jsonl(a, [cc_user("a", "the heron nested by the mill pond at dusk"),
                    cc_assistant("a", "noted: the heron by the mill pond")])
    b = tmp_path / "sess-b.jsonl"
    write_jsonl(b, [
        cc_user("b", "please mull over the quarterly plan"),
        {"type": "assistant", "uuid": "a-b", "timestamp": "2026-01-02T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4", "content": [
             {"type": "text", "text": "thinking it through"},
             {"type": "tool_use", "id": "tu-b", "name": "mull_over",
              "input": {"thought": "quarterly plan tradeoffs"}}]}},
    ])
    return {"a": api.import_path(a).thread_id, "b": api.import_path(b).thread_id}


def test_incidents_verb_green_catalogue(tmp_path, fixture_archive, capsys) -> None:
    cat = _write_catalogue(
        tmp_path / "cat.jsonl",
        {"slug": "heron", "tier": "recall", "query": "heron mill pond",
         "must_surface": [fixture_archive["a"]]},
        {"slug": "tools_visible", "tier": "capability", "query": "mull_over",
         "content_type": "tool", "must_surface": [fixture_archive["b"]]},
        {"slug": "plan_record", "tier": "record", "query": "quarterly plan",
         "must_surface": [fixture_archive["b"]]},
        {"slug": "open_case", "tier": "unresolved", "query": "mull over",
         "must_surface": [fixture_archive["b"]], "notes": "sought phrase unlocated"},
    )
    rc = main(["incidents", cat])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("PASS") == 4 and "FAIL" not in out
    assert "(open)" in out
    assert "4 passed, 0 failed, 1 open" in out


def test_incidents_verb_breach_exits_nonzero_with_stakes(tmp_path, fixture_archive, capsys) -> None:
    cat = _write_catalogue(
        tmp_path / "cat.jsonl",
        {"slug": "lost", "tier": "recall", "query": "zanzibar counterweight",
         "must_surface": [fixture_archive["a"]],
         "confidence": "certain — IK I had this conversation",
         "original_failure": "answered 'not found' against a thread that was right there"},
    )
    rc = main(["incidents", cat])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out and "0 passed, 1 failed" in out
    # A breach carries its human stakes, not just ids.
    assert "confidence: certain" in out
    assert "original_failure: answered 'not found'" in out


def test_incidents_verb_refuses_bad_catalogue(tmp_path, capsys) -> None:
    cat = _write_catalogue(tmp_path / "cat.jsonl", _case(tier="nope"))
    rc = main(["incidents", cat])
    assert rc == 2
    assert "archive incidents:" in capsys.readouterr().err


def test_incidents_verb_missing_file(tmp_path, capsys) -> None:
    rc = main(["incidents", str(tmp_path / "absent.jsonl")])
    assert rc == 2
    assert "no catalogue at" in capsys.readouterr().err


def test_run_incidents_api_threads_limit(tmp_path, fixture_archive) -> None:
    cat = _write_catalogue(
        tmp_path / "cat.jsonl",
        {"slug": "heron", "tier": "recall", "query": "heron mill pond",
         "must_surface": [fixture_archive["a"]]},
    )
    r = api.run_incidents(cat, limit=1)
    assert r["ok"] and r["passed"] == 1
