"""Generated queries against the never-raise contract.

A query string is the one input to search that nothing validates: it arrives as
whatever an agent typed, and the pipeline's answer to a malformed one has to be
*results or no results* — never a raw ``fts5: syntax error`` out of MATCH. That
contract is asserted by name in ``test_search.py`` over a list of shapes someone
sat down and thought of. This file asserts it over the shapes nobody thought of.

The subject is wider than one function. ``to_match_query`` is one of **four**
MATCH expressions the lexical arm builds — :func:`~._classify.classify_query`
routes a query to the boolean/natural builder, the pipe-OR join, the code-mode
phrase, or the identifier-token AND/OR fallbacks — so the properties run through
``search_events``, where the classifier does the picking, rather than through any
single builder. A generator pointed at ``to_match_query`` alone would leave three
expression shapes unfuzzed.

Both halves are **executed, never inspected**: a builder that returns a string
without raising has proven nothing, because the failure mode is FTS5 rejecting
that string. Every property here ends in real SQL.

Deterministic by construction (``derandomize``): the same examples every run, so
a red is caused by the change under test rather than by the dice. Widen by hand
with ``--hypothesis-seed=<n>`` when deliberately hunting.
"""

from __future__ import annotations

import sqlite3

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text as sa_text

from thread_archive import _api as ta
from thread_archive._retrieval import search, search_events
from thread_archive._retrieval.fts import (
    _like_prefix,
    _like_substring,
    _quote_all_tokens,
    _quote_phrase,
    to_match_query,
)
from thread_archive._store import get_session

from .helpers import cc_assistant, cc_user, write_jsonl

# The corpus is seeded once per test and every property below only reads it, so
# sharing it across a test's examples is correct — and rebuilding an archive per
# example would price the file out of the per-commit suite entirely.
_BASE = settings(
    derandomize=True,
    deadline=None,  # a DB round-trip under `-n 4` blows the 200ms default; a
                    # timing flake here would poison trust in the whole file
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# ── the query generator ──────────────────────────────────────────────────────
# Structured tokens, because raw text almost never produces the shapes that
# actually break a query builder: a balanced quote, an adjacent operator pair, a
# pipe beside an identifier. Each group is a class of thing that is special to
# something downstream, so a shrunk counterexample names which one.

_WORDS = st.sampled_from(["authentication", "login", "session", "p4", "café", "日本語"])
_IDENTIFIERS = st.sampled_from(["get_session", "a.b.c", "std::vector", "__init__"])
_OPERATORS = st.sampled_from(["AND", "OR", "NOT"])
_FTS_SYNTAX = st.sampled_from(['"', "*", "^", ":", "(", ")", "-", "+", "NEAR", "{", "}"])
_LIKE_SYNTAX = st.sampled_from(["%", "_", "\\"])
_PIPE = st.just("|")
_FREE = st.text(alphabet=st.characters(codec="utf-8"), max_size=8)

_TOKEN = st.one_of(_WORDS, _IDENTIFIERS, _OPERATORS, _FTS_SYNTAX, _LIKE_SYNTAX, _PIPE, _FREE)

#: Grammar-shaped queries, plus unstructured text for what the grammar can't
#: imagine. Both reach every branch of ``classify_query``.
QUERIES = st.one_of(
    st.lists(_TOKEN, max_size=8).map(" ".join),
    st.text(alphabet=st.characters(codec="utf-8"), max_size=60),
)


@pytest.fixture
def corpus(archive_home):
    """A small real archive — enough that a MATCH has an index to run against and
    a hit to find, which is what makes "did not raise" mean anything."""
    ta.open_archive()
    for name, content in (
        ("auth", "how does authentication work in the login flow"),
        ("code", "does get_session own the a.b.c pool"),
        ("misc", "the p4 rollout converted it to mp4 last night"),
    ):
        f = archive_home / f"{name}.jsonl"
        write_jsonl(f, [cc_user(name, content), cc_assistant(name, f"about {content}")])
        ta.import_path(f)
    return archive_home


def _match_is_accepted(expression: str) -> None:
    """Run ``expression`` through a real FTS5 MATCH. Raises if FTS5 rejects it —
    which is the whole failure this file exists to catch."""
    with get_session() as s:
        s.execute(sa_text("SELECT event_id FROM event_search WHERE event_search MATCH :q LIMIT 1"),
                  {"q": expression}).fetchall()


# ── the never-raise contract ─────────────────────────────────────────────────


@settings(parent=_BASE, max_examples=300)
@given(query=QUERIES)
def test_no_query_shape_raises_out_of_the_lexical_arm(corpus, query) -> None:
    """The contract at the arm that owns every MATCH expression. Routed through
    ``search_events`` rather than a builder so the classifier picks the mode, and
    all four expression shapes get generated input."""
    assert isinstance(search_events(query, limit=5), list)


@settings(parent=_BASE, max_examples=120)
@given(query=QUERIES)
def test_no_query_shape_raises_out_of_the_full_pipeline(corpus, query) -> None:
    """The same contract one layer out, where fusion, ranking, grouping and the
    match-quality verdict also touch the query text. Fewer examples: this is the
    expensive path, and the arm above is where the expressions are built."""
    assert isinstance(list(search(query, limit=5)), list)


@settings(parent=_BASE, max_examples=300)
@given(query=QUERIES)
def test_every_builder_emits_an_expression_fts5_accepts(corpus, query) -> None:
    """Each builder pinned by name, so a counterexample says which one produced
    the expression FTS5 refused rather than only that some search failed."""
    _match_is_accepted(to_match_query(query))
    _match_is_accepted(_quote_all_tokens(query))
    _match_is_accepted(_quote_phrase(query))


@settings(parent=_BASE, max_examples=300)
@given(query=QUERIES)
def test_the_demotion_target_is_syntax_free(query) -> None:
    """``_quote_all_tokens`` is where every malformed shape lands *and* the retry
    form for a residual syntax error — so it is the one builder that must be safe
    by construction rather than by validation. Its output is quoted phrases and
    separating spaces, nothing else: no bare operator, no metacharacter outside a
    phrase, and never empty."""
    out = _quote_all_tokens(query)
    assert out
    outside = "".join(out.split('"')[::2])  # the spans between quoted phrases
    assert outside.strip() == ""


# ── LIKE escaping: the scans match literally ─────────────────────────────────
# ASCII-lowercase alphabet on purpose. SQLite's LIKE folds case for ASCII only,
# so a Unicode alphabet would fail on the fold rather than on the escaping under
# test — while the wildcards that escaping exists for (`%`, `_`, `\`) are all
# ASCII and fully reachable here.

_LIKE_TEXT = st.text(alphabet="abc_%\\ .", max_size=8)


@pytest.fixture(scope="module")
def sqlite_like():
    """SQLite's own LIKE, evaluated on bound literals — the same operator and the
    same ``ESCAPE`` the scans run, with no archive in the way."""
    conn = sqlite3.connect(":memory:")
    yield lambda haystack, pattern: bool(
        conn.execute("SELECT :h LIKE :p ESCAPE '\\'", {"h": haystack, "p": pattern}).fetchone()[0])
    conn.close()


@settings(parent=_BASE, max_examples=300)
@given(needle=_LIKE_TEXT, haystack=_LIKE_TEXT)
def test_a_substring_scan_matches_exactly_the_text_containing_it(sqlite_like, needle, haystack) -> None:
    """The full specification of "matches literally", both directions at once: the
    scan finds a document iff the document really contains the text. The reverse
    direction is the one that bites — an unescaped ``_`` makes ``get_session``
    match ``getXsession``, which reads as a hit rather than as a bug."""
    assert sqlite_like(haystack, _like_substring(needle)) == (needle in haystack)


@settings(parent=_BASE, max_examples=300)
@given(prefix=_LIKE_TEXT, haystack=_LIKE_TEXT)
def test_a_prefix_scan_matches_exactly_the_text_starting_with_it(sqlite_like, prefix, haystack) -> None:
    """``startswith`` is a structural scan an agent points at raw content, so its
    pattern carries user text into LIKE with the same escaping obligation."""
    assert sqlite_like(haystack, _like_prefix(prefix)) == haystack.startswith(prefix)


# ── what SQLite cannot carry ─────────────────────────────────────────────────


@settings(parent=_BASE, max_examples=100)
@given(query=QUERIES)
def test_unencodable_code_points_never_reach_the_driver(corpus, query) -> None:
    """A NUL terminates a bound string mid-token (so even the fully-quoted
    demotion form raises on one) and a lone surrogate cannot be encoded to UTF-8
    at all — and both ride in freely over MCP, where a JSON query string may hold
    ``\\u0000`` or an unpaired ``\\ud800``. Injected into otherwise ordinary
    queries, since the interesting case is one hiding inside real text."""
    for hostile in (f"{query}\x00tail", f"lead\x00{query}", f"{query}\ud800", f"\udfff{query}"):
        assert isinstance(search_events(hostile, limit=5), list)
        _match_is_accepted(to_match_query(hostile))
