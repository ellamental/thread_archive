"""The pull-request strand of the code axis: parse, fold, look up.

Four layers, tested apart, mirroring ``test_code_axis.py``: the pure extractor and
ref parser (a payload / a typed string → the PR it names), the parser that turns a
harness's ``pr-link`` line into a modeled event, the cursor fold that projects those
into ``event_prs``, and :func:`blame_pr` — what the ``pr=`` scope on ``thread_search``
is built on.

The distinguishing property from its commit sibling: a PR association is *stated*,
not inferred, so the tests here are about not losing or duplicating testimony rather
than about bounding an inference.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from thread_archive import _api as ta
from thread_archive._retrieval import code
from thread_archive._retrieval._paths import extract_pr, parse_pr_ref
from thread_archive._store import Event, Thread, get_session
from thread_archive._thread_import.event_builder import compute_dedup_key
from thread_archive._thread_import.parsers.claude_code import ClaudeCodeParser

_EVENTS_LOGGER = "thread_archive._thread_import"


def _pr_line(number=4, repo="ellamental/thread_archive", ts="2026-07-29T17:23:23.532Z",
             **extra):
    line = {
        "type": "pr-link",
        "sessionId": "s1",
        "prNumber": number,
        "prUrl": f"https://github.com/{repo}/pull/{number}" if repo else None,
        "prRepository": repo,
        "timestamp": ts,
    }
    line.update(extra)
    return line


# ── ref parsing ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ref, expected", [
    ("4", (None, "4")),
    ("#4", (None, "4")),
    ("ellamental/thread_archive#4", ("ellamental/thread_archive", "4")),
    ("https://github.com/ellamental/thread_archive/pull/4",
     ("ellamental/thread_archive", "4")),
    ("https://github.com/ellamental/thread_archive/pull/4/", ("ellamental/thread_archive", "4")),
    # GitLab spells a merge request differently and nests it under /-/.
    ("https://gitlab.com/group/proj/-/merge_requests/12", ("group/proj", "12")),
])
def test_pr_ref_shapes_a_caller_actually_has_to_hand(ref, expected):
    assert parse_pr_ref(ref) == expected


@pytest.mark.parametrize("ref", ["", "   ", "rank.py", "abc", "#", "owner/name#",
                                 "https://github.com/owner/name/issues/4"])
def test_non_pr_refs_are_rejected_rather_than_coerced(ref):
    """An issue URL is the sharp one: same host, same shape, a different object."""
    assert parse_pr_ref(ref) is None


def test_extract_pr_normalizes_the_number_to_text():
    """The harness sends an int and a caller types a string; the projection must not
    make the lookup turn on which."""
    assert extract_pr({"number": 4, "repo": "o/n", "url": "u"}) == ("4", "o/n", "u")
    assert extract_pr({"number": "4", "repo": "o/n", "url": "u"}) == ("4", "o/n", "u")


@pytest.mark.parametrize("payload", [
    {}, {"number": None}, {"number": ""}, {"number": "abc"}, {"number": "4.5"},
])
def test_a_payload_naming_no_pr_number_extracts_nothing(payload):
    assert extract_pr(payload) is None


def test_a_repo_that_is_not_a_forge_repo_is_dropped_not_stored():
    """``repo`` has one spelling — ``owner/name``. Anything else (a filesystem path
    that happens to have a slash) would poison the repo-narrowed lookup."""
    assert extract_pr({"number": "4", "repo": "/Users/ella/dev/thread"})[1] is None
    assert extract_pr({"number": "4", "repo": "not a repo"})[1] is None
    assert extract_pr({"number": "4", "repo": "o/n"})[1] == "o/n"


# ── the parser ───────────────────────────────────────────────────────────────


def test_pr_link_line_parses_to_a_modeled_block():
    msgs = ClaudeCodeParser().parse_export({"lines": [_pr_line()], "session_id": "s1"})
    [msg] = [m for m in msgs if m["provider_data"].get("line_type") == "pr-link"]
    [block] = msg["content_blocks"]
    assert block["type"] == "pr_link"
    assert block["repo"] == "ellamental/thread_archive"
    assert block["number"] == 4
    assert block["url"] == "https://github.com/ellamental/thread_archive/pull/4"
    assert msg["content_text"] == "[pull request ellamental/thread_archive#4]"
    # Bookkeeping, not conversation: it must not read as a turn someone took.
    assert msg["role"] == "system" and msg["is_visually_hidden"] is True


def test_the_same_pr_announced_every_turn_is_one_association():
    """Claude Code re-emits the marker each turn the link is live — 22 times in the
    session that produced this feature. Identity is the PR, so they collapse."""
    lines = [_pr_line(ts=f"2026-07-29T17:{m:02d}:00.000Z") for m in range(23, 45)]
    msgs = ClaudeCodeParser().parse_export({"lines": lines, "session_id": "s1"})
    prs = [m for m in msgs if m["provider_data"].get("line_type") == "pr-link"]
    assert len(prs) == 22  # every line is parsed...

    keys = {
        compute_dedup_key(m["provider_message_id"], "pr_link",
                          {"repo": m["content_blocks"][0]["repo"],
                           "number": m["content_blocks"][0]["number"],
                           "url": m["content_blocks"][0]["url"]})
        for m in prs
    }
    assert len(keys) == 1  # ...and all 22 land on one event


def test_two_different_prs_in_one_session_stay_two():
    lines = [_pr_line(number=4), _pr_line(number=9)]
    msgs = ClaudeCodeParser().parse_export({"lines": lines, "session_id": "s1"})
    prs = [m for m in msgs if m["provider_data"].get("line_type") == "pr-link"]
    assert {m["content_blocks"][0]["number"] for m in prs} == {4, 9}
    assert len({m["provider_message_id"] for m in prs}) == 2


def test_a_pr_link_naming_no_number_is_preserved_verbatim_not_dropped():
    """The fallback contract still holds under a modeled line type: a shape the
    parser can't read is kept whole rather than thrown away."""
    line = _pr_line()
    del line["prNumber"]
    msgs = ClaudeCodeParser().parse_export({"lines": [line], "session_id": "s1"})
    [msg] = [m for m in msgs if m["content_blocks"][0]["type"] == "unknown_line"]
    assert msg["content_blocks"][0]["raw"] == line


def test_a_pr_link_without_a_repo_still_records_the_number():
    msgs = ClaudeCodeParser().parse_export(
        {"lines": [_pr_line(repo=None)], "session_id": "s1"}
    )
    [msg] = [m for m in msgs if m["provider_data"].get("line_type") == "pr-link"]
    assert msg["content_blocks"][0]["number"] == 4
    assert msg["content_text"] == "[pull request #4]"


def test_pr_link_is_no_longer_import_drift(caplog, archive_home):
    """The regression this whole strand exists for: an unmodeled ``pr-link`` drove
    claude-code to a ``validation_drift`` degradation verdict off one session."""
    from thread_archive._importers._events import log_parse_validation

    msgs = ClaudeCodeParser().parse_export({"lines": [_pr_line()], "session_id": "s1"})
    with caplog.at_level(logging.WARNING, logger=_EVENTS_LOGGER):
        log_parse_validation(msgs, provider="claude-code", conversation_id="s1",
                             batch_safe=True)
    findings = [r.getMessage() for r in caplog.records if "parse-validation" in r.getMessage()]
    assert findings == []


# ── the fold ─────────────────────────────────────────────────────────────────


def _seed_pr(thread_title, number="4", repo="ellamental/thread_archive", day=1,
             url=None):
    with get_session() as s:
        t = Thread(name=f"n:{thread_title}", title=thread_title, source="claude-code",
                   source_id=thread_title, source_metadata={"cwd": "/proj"})
        s.add(t)
        s.flush()
        s.add(Event(
            thread_id=t.id, stream_id="s", event_type="pr_link",
            payload={"repo": repo, "number": number,
                     "url": url or (f"https://github.com/{repo}/pull/{number}"
                                    if repo else None)},
            occurred_at=datetime(2026, 7, day, 10, 0, 0, tzinfo=timezone.utc),
        ))
        tid = t.id
        s.commit()
    return tid


def test_fold_populates_the_pr_projection(archive_home):
    ta.open_archive()
    _seed_pr("a")
    result = code.refresh_code_index()
    assert result["prs"] == 1
    status = code.code_index_status()
    assert status["current"] and status["prs"] == 1 and status["distinct_prs"] == 1


def test_fold_is_idempotent_over_prs(archive_home):
    ta.open_archive()
    _seed_pr("a")
    assert code.refresh_code_index()["prs"] == 1
    assert code.refresh_code_index()["prs"] == 0


def test_a_pr_event_naming_no_number_folds_to_nothing(archive_home):
    """Belt and braces over the parser's own guard: a malformed event that reached
    the log must not become a row keyed on nothing."""
    ta.open_archive()
    with get_session() as s:
        t = Thread(name="n:bad", title="bad", source="claude-code", source_id="bad")
        s.add(t)
        s.flush()
        s.add(Event(thread_id=t.id, stream_id="s", event_type="pr_link",
                    payload={"repo": "o/n", "number": None, "url": None},
                    occurred_at=datetime(2026, 7, 1, tzinfo=timezone.utc)))
        s.commit()
    assert code.refresh_code_index()["prs"] == 0


def test_rebuild_re_derives_the_pr_projection(archive_home):
    ta.open_archive()
    _seed_pr("a")
    code.refresh_code_index()
    assert code.rebuild_code_index()["prs"] == 1
    assert code.code_index_status()["prs"] == 1


# ── blame_pr ─────────────────────────────────────────────────────────────────


def test_blame_pr_names_the_sessions_that_declared_it(archive_home):
    ta.open_archive()
    one = _seed_pr("first", day=1)
    two = _seed_pr("second", day=2)
    code.refresh_code_index()

    verdict = code.blame_pr("4")
    assert verdict["resolution"] == "sessions"
    assert verdict["total_threads"] == 2
    assert {t["thread_id"] for t in verdict["threads"]} == {one, two}
    assert verdict["ref"] == "ellamental/thread_archive#4"
    # Most recently active first, as every other code-axis answer orders.
    assert verdict["threads"][0]["thread_id"] == two


@pytest.mark.parametrize("ref", [
    "4", "#4", "ellamental/thread_archive#4",
    "https://github.com/ellamental/thread_archive/pull/4",
])
def test_every_ref_shape_reaches_the_same_pr(archive_home, ref):
    ta.open_archive()
    tid = _seed_pr("a")
    code.refresh_code_index()
    assert [t["thread_id"] for t in code.blame_pr(ref)["threads"]] == [tid]


def test_a_pr_nobody_recorded_is_unknown_not_empty(archive_home):
    """The distinction the note has to carry: "no session recorded this" is a
    different fact from "this PR had no work behind it"."""
    ta.open_archive()
    _seed_pr("a")
    code.refresh_code_index()
    verdict = code.blame_pr("999")
    assert verdict["resolution"] == "unknown" and verdict["threads"] == []
    assert "no session in this archive recorded" in verdict["note"]


def test_a_ref_that_is_not_a_pr_says_so(archive_home):
    ta.open_archive()
    verdict = code.blame_pr("rank.py")
    assert verdict["resolution"] == "invalid"
    assert verdict["threads"] == []


def test_a_bare_number_across_repos_returns_all_and_names_them(archive_home):
    """Silently picking one repository would answer a question nobody asked."""
    ta.open_archive()
    mine = _seed_pr("mine", repo="ellamental/thread_archive")
    theirs = _seed_pr("theirs", repo="someone/other")
    code.refresh_code_index()

    verdict = code.blame_pr("4")
    assert verdict["total_threads"] == 2
    assert {t["thread_id"] for t in verdict["threads"]} == {mine, theirs}
    assert verdict["repos"] == ["ellamental/thread_archive", "someone/other"]
    assert "matched 2 repositories" in verdict["note"]
    assert verdict["ref"] == "#4"


def test_repo_narrows_a_bare_number_by_suffix(archive_home):
    ta.open_archive()
    mine = _seed_pr("mine", repo="ellamental/thread_archive")
    _seed_pr("theirs", repo="someone/other")
    code.refresh_code_index()

    # The owner is optional — a bare repository name is what someone remembers.
    assert [t["thread_id"] for t in code.blame_pr("4", repo="thread_archive")["threads"]] \
        == [mine]
    assert [t["thread_id"] for t in
            code.blame_pr("4", repo="ellamental/thread_archive")["threads"]] == [mine]


def test_an_underscore_in_a_repo_name_is_not_a_like_wildcard(archive_home):
    """``thread_archive`` and ``thread-archive`` are different repositories with
    different PR #4s, and ``_`` is a LIKE wildcard."""
    ta.open_archive()
    underscore = _seed_pr("underscore", repo="ellamental/thread_archive")
    _seed_pr("hyphen", repo="ellamental/thread-archive")
    code.refresh_code_index()

    hits = code.blame_pr("4", repo="thread_archive")["threads"]
    assert [t["thread_id"] for t in hits] == [underscore]


def test_a_repo_qualified_ref_for_an_unrecorded_repo_is_unknown(archive_home):
    ta.open_archive()
    _seed_pr("a", repo="ellamental/thread_archive")
    code.refresh_code_index()
    assert code.blame_pr("someone/other#4")["resolution"] == "unknown"


def test_the_pr_url_rides_along_for_the_caller_to_link_out(archive_home):
    ta.open_archive()
    _seed_pr("a")
    code.refresh_code_index()
    verdict = code.blame_pr("4")
    assert verdict["url"] == "https://github.com/ellamental/thread_archive/pull/4"


# ── the search scope ─────────────────────────────────────────────────────────


def test_thread_search_pr_scope_notes_the_sessions(archive_home):
    from thread_archive import _tools

    ta.open_archive()
    _seed_pr("a")
    code.refresh_code_index()
    out = _tools.thread_search("", pr="4")
    assert "pull request ellamental/thread_archive#4 — 1 session(s)" in out
    assert "https://github.com/ellamental/thread_archive/pull/4" in out


def test_thread_search_pr_scope_explains_a_miss(archive_home):
    from thread_archive import _tools

    ta.open_archive()
    _seed_pr("a")
    code.refresh_code_index()
    out = _tools.thread_search("", pr="999")
    assert "no session in this archive recorded" in out
    assert "Only sessions whose harness records the link" in out


def test_thread_search_rejects_a_non_pr_ref_by_name(archive_home):
    from thread_archive import _tools

    ta.open_archive()
    out = _tools.thread_search("", pr="rank.py")
    assert "'rank.py' is not a pull request" in out


def test_blame_takes_exactly_one_direction(archive_home):
    ta.open_archive()
    with pytest.raises(ValueError, match="exactly one"):
        ta.blame(pr="4", commit="abc1234")
