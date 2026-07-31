"""The manual as data: what ships, how a page is addressed, and what reads it.

``docs/public/*.md`` is package data (the wheel renames the tree to
``thread_archive/_docs``), so the pages have two readers that must agree — the
``thread-archive docs`` verb and the viewer's ``/docs`` — and one resolver
under them. Pinned here: the resolver's contract, the public/internal boundary
both readers draw, and the CLI's use of both. The endpoints are exercised in
``tests/test_web.py``, and what the artifact actually carries in
``tests/test_package_artifact.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from thread_archive import _docs
from thread_archive.cli import build_parser, main

#: The maintainer's half — ``docs/*.md``, which ships nowhere.
REPO_INTERNAL = Path(__file__).resolve().parent.parent / "docs"
#: The manual proper — the pages the wheel carries and both readers serve.
REPO_DOCS = REPO_INTERNAL / "public"


def test_this_installation_resolves_a_manual() -> None:
    # Which location answers is the shape of the installation, not a fact this
    # test gets to pin: an editable checkout resolves the package out of src/ and
    # reads the repo's tree, while the Docker install lane runs this suite from a
    # repo copy against an installed wheel and reads the packaged one. Both are
    # correct; carrying no manual at all is not.
    assert _docs.docs_dir() in _docs.LOCATIONS


def test_every_markdown_file_is_a_page() -> None:
    assert {p.slug for p in _docs.pages()} == {p.stem for p in REPO_DOCS.glob("*.md")}


def test_the_internal_half_of_the_manual_is_not_served() -> None:
    """``docs/*.md`` is the maintainer's, and neither reader offers it.

    Those pages name branches, release gates and a dev-panel server no install
    has. The split is the directory and nothing else — no list to keep in sync —
    so this asserts the mechanism: whatever sits beside ``public/`` today is
    absent from the index and unreachable by slug.
    """
    internal = sorted(p.stem for p in REPO_INTERNAL.glob("*.md"))
    assert internal, "docs/*.md is empty — the split has nothing behind it"
    served = {p.slug for p in _docs.pages()}
    assert served.isdisjoint(internal), served & set(internal)
    for slug in internal:
        assert _docs.find(slug) is None, slug


def test_no_reference_reaches_into_the_internal_half() -> None:
    # The slug arrives from a URL path segment and a CLI argument, so the
    # spellings that name a real file one level up must not resolve.
    for ref in ("../devweb", "../devweb.md", "..", "../README.md"):
        assert _docs.find(ref) is None, ref


def test_the_reading_order_names_only_pages_that_exist() -> None:
    """A slug in ORDER with no page behind it orders a manual that changed shape.

    The other direction is deliberately allowed — a new page lists (appended,
    alphabetically) without being placed — so adding a doc can never hide it.
    """
    missing = [slug for slug in _docs.ORDER if not (REPO_DOCS / f"{slug}.md").is_file()]
    assert not missing, f"ORDER names pages that no longer exist: {missing}"


def _manual(directory: Path, **files: str) -> tuple[Path, ...]:
    """A real manual on disk, as a search path to hand the resolver.

    The seam is the search path (``_docs.LOCATIONS``), so a test points the real
    resolver at real markdown rather than patching where it looks — which is also
    how the install-shaped and no-manual cases get driven from a checkout that is
    neither.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / f"{name}.md").write_text(text, encoding="utf-8")
    return (directory,)


def test_pages_come_back_in_reading_order_with_the_unplaced_appended(tmp_path) -> None:
    here = _manual(tmp_path, cli="# CLI\n", install="# Install\n", **{"zzz-new": "# New\n"})
    assert [p.slug for p in _docs.pages(here)] == ["install", "cli", "zzz-new"]


def test_a_page_carries_its_title_and_opening_paragraph() -> None:
    page = _docs.find("cli")
    assert page is not None
    assert page.title == "CLI"
    assert page.summary.startswith("One namespaced command.")
    # Plain text: the index renders it as a string, so no inline markup survives.
    assert "**" not in page.summary and "`" not in page.summary
    assert page.read().startswith("# CLI")


def test_summaries_stay_short_enough_to_list() -> None:
    for page in _docs.pages():
        assert len(page.summary) <= _docs.SUMMARY_CHARS + 1, page.slug  # +1 for the ellipsis


def test_a_page_that_opens_on_a_list_gets_no_summary_rather_than_a_fragment(tmp_path) -> None:
    (page,) = _docs.pages(_manual(tmp_path, listy="# Listy\n\n- one\n- two\n"))
    assert (page.title, page.summary) == ("Listy", "")


def test_bold_prose_is_prose(tmp_path) -> None:
    # web-viewer.md opens on **bold**, which is a paragraph, not a list item.
    (page,) = _docs.pages(_manual(tmp_path, bold="# Bold\n\n**The viewer is dev-only** — really.\n"))
    assert page.summary == "The viewer is dev-only — really."


def test_a_titleless_page_still_lists_under_its_slug(tmp_path) -> None:
    (page,) = _docs.pages(_manual(tmp_path, bare="no heading here\n"))
    assert page.title == "bare"


@pytest.mark.parametrize("ref", ["cli", "cli.md"])
def test_a_page_resolves_by_slug_or_filename(ref: str) -> None:
    page = _docs.find(ref)
    assert page is not None and page.slug == "cli"


@pytest.mark.parametrize(
    "ref",
    ["nope", "", "../pyproject.toml", "../../etc/passwd", "sub/cli", "CLI", ".."],
)
def test_no_reference_reaches_outside_the_manual(ref: str) -> None:
    # The slug arrives from a URL path segment and a CLI argument, so a caller's
    # string must never name a file the manual does not hold.
    assert _docs.find(ref) is None


def test_an_installation_with_no_manual_answers_empty(tmp_path) -> None:
    # A stripped install: neither location has one. Empty is the answer, not a
    # crash — the CLI says so and the viewer's index renders it.
    nowhere = (tmp_path / "packaged", tmp_path / "checkout")
    assert _docs.docs_dir(nowhere) is None
    assert _docs.pages(nowhere) == []
    assert _docs.find("cli", nowhere) is None


def test_a_directory_holding_no_markdown_is_not_a_manual(tmp_path) -> None:
    # The packaged directory always exists — it holds this module. Empty of
    # pages, it must fall through to the next location rather than answer.
    empty = tmp_path / "packaged"
    empty.mkdir()
    (empty / "__init__.py").write_text("", encoding="utf-8")
    checkout = _manual(tmp_path / "checkout", cli="# CLI\n")
    assert _docs.docs_dir((empty, *checkout)) == checkout[0]


def test_the_packaged_copy_wins_over_a_directory_the_install_sits_near(tmp_path) -> None:
    packaged = _manual(tmp_path / "packaged", cli="# Packaged\n")
    checkout = _manual(tmp_path / "checkout", cli="# Checkout\n")
    page = _docs.find("cli", (*packaged, *checkout))
    assert page is not None and page.title == "Packaged"


# ── the CLI verb ─────────────────────────────────────────────────────────────

def test_docs_verb_lists_every_page(capsys) -> None:
    assert main(["docs"]) == 0
    out = capsys.readouterr().out
    for page in _docs.pages():
        assert page.slug in out
    for internal in REPO_INTERNAL.glob("*.md"):
        assert internal.stem not in out, internal.stem


def test_docs_verb_refuses_an_internal_page(capsys) -> None:
    assert main(["docs", "releasing"]) == 1
    assert "no manual page named 'releasing'" in capsys.readouterr().err


def test_docs_verb_prints_one_page_verbatim(capsys) -> None:
    assert main(["docs", "mcp"]) == 0
    assert capsys.readouterr().out.rstrip("\n") == _docs.find("mcp").read().rstrip("\n")


def test_docs_verb_names_the_file_on_disk(capsys) -> None:
    assert main(["docs", "cli", "--path"]) == 0
    assert capsys.readouterr().out.strip() == str(_docs.find("cli").path)
    assert main(["docs", "--path"]) == 0
    assert capsys.readouterr().out.strip() == str(_docs.docs_dir())


def test_docs_verb_on_an_unknown_page_says_what_there_is(capsys) -> None:
    assert main(["docs", "nope"]) == 1
    err = capsys.readouterr().err
    assert "no manual page named 'nope'" in err and "install" in err


def test_the_docs_verb_is_registered_unconditionally() -> None:
    # Unlike `web`, whose viewer ships in no wheel: the manual does ship, so an
    # install tree carries this verb too.
    parser = build_parser(has_viewer=False)
    sub = next(a for a in parser._actions if hasattr(a, "choices") and "status" in (a.choices or {}))
    assert "docs" in sub.choices


# ── cross-page integrity ─────────────────────────────────────────────────────
# The manual is a product surface now: it ships in the wheel, `thread-archive
# docs` prints it offline, and the viewer serves it. A dangling link or a stale
# quoted number is a shipped defect, not a repo blemish.

REPO_ROOT = REPO_INTERNAL.parent
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _markdown_sources() -> list[Path]:
    return [
        *sorted(REPO_DOCS.glob("*.md")),
        *sorted(REPO_INTERNAL.glob("*.md")),
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "SECURITY.md",
    ]


def test_every_relative_link_in_the_manual_resolves() -> None:
    """A reader following a cross-link must land on a file that exists.

    Absolute URLs are somebody else's to keep alive; a relative one is ours, and
    it is resolved the same way by GitHub, by the viewer's link rewriter, and by
    anyone reading the raw markdown a `docs` verb printed.
    """
    broken: list[str] = []
    for source in _markdown_sources():
        for target in _LINK.findall(source.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            if not (source.parent / target.split("#")[0]).exists():
                broken.append(f"{source.relative_to(REPO_ROOT)} -> {target}")
    assert not broken, f"dangling relative links: {broken}"


def test_the_benchmark_table_quotes_the_accepted_numbers() -> None:
    """search-quality.md's table is the release bar, so it has to *be* the bar.

    The numbers live in ``search_lab/quality-baseline.json`` — checked in,
    already integrity-tested by ``tests/test_search_gate.py``. Quoting them by
    hand in a shipped page is how a manual drifts behind the bench, so every
    figure in the table is matched back to the row it came from.
    """
    baseline = REPO_ROOT / "search_lab" / "quality-baseline.json"
    if not baseline.is_file():  # pragma: no cover - the lab ships in no install
        pytest.skip("no quality baseline in this tree")
    rows = json.loads(baseline.read_text(encoding="utf-8"))["rows"]
    accepted = {
        f"{measure}:{value:.3f}"
        for row in rows.values()
        for measure, value in row["measures"].items()
    }

    page = _docs.find("search-quality")
    assert page is not None
    table = [ln for ln in page.read().splitlines() if ln.startswith("| ") and "|" in ln[2:]]
    quoted = [
        cell
        for line in table
        for cell in (c.strip() for c in line.strip("|").split("|"))
        if re.fullmatch(r"0\.\d{3}", cell)
    ]
    assert quoted, "the benchmark table quotes no measured numbers"

    unaccepted = [
        value for value in quoted
        if not any(entry.endswith(f":{value}") for entry in accepted)
    ]
    assert not unaccepted, (
        f"search-quality.md quotes numbers no baseline row carries: {unaccepted}. "
        "Re-read search_lab/quality-baseline.json, or run `search_lab gate --update`."
    )
