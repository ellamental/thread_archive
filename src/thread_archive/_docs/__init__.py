"""The manual this installation carries: the ``docs/public/`` pages, as data.

The manual ships. The wheel carries ``docs/public/*.md`` here as package data
(``pyproject.toml``), so ``pip install thread-archive`` carries the pages the
repo does — install, cli, mcp, format, providers, the rest — and an install can
answer "what is this and how do I drive it" with no network and no clone. Two
readers resolve through this module and share its ordering: the
``thread-archive docs`` verb at a terminal, and the viewer's ``/docs`` pages
(``_web/server.py``).

**Pages under ``public/`` only, and that is the whole public/internal split.**
``docs/*.md`` — the release process, the bench landscape, the dev panels — is
written for whoever works on this repo: it names branches, gates and instruments
no install has. Those pages neither ship (the wheel includes ``docs/public``
and nothing else under ``docs/``) nor list here, because the glob below reads
one directory and that directory is ``public/``. So the boundary is which
directory a page sits in, drawn once, with nothing to keep in sync: moving a
file across it moves what installs get and what both readers show. Publishing a
page is a deliberate move into ``public/``, never an oversight.

Two locations answer, in this order:

* ``thread_archive/_docs/*.md`` — the packaged copy. What an install has.
* ``<repo>/docs/public/*.md`` — the tree the packaged copy is made from. A
  checkout (and an editable install, which resolves the package out of ``src/``)
  has no packaged copy and reads this one, so editing a page lands in the viewer
  on the next request with nothing to rebuild.

The packaged copy wins where it exists, because an install must never read a
directory it happens to sit near. Neither location is guaranteed — a stripped
install has no manual — so the callers treat an empty list as an answer rather
than an error, the same shape :mod:`thread_archive._viewer` uses for the viewer.

A page is addressed by its **slug**, its filename without ``.md``, which is what
the docs' own cross-links (``[stability.md](stability.md)``) already name — one
vocabulary for the file, the URL, and the CLI argument.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_PACKAGED = Path(__file__).resolve().parent
#: ``src/thread_archive/_docs`` → ``<repo>/docs/public``.
_CHECKOUT = _PACKAGED.parents[2] / "docs" / "public"

#: Where a manual is looked for, in order. The default for every function here,
#: and a seam rather than a knob: this tree always resolves the checkout copy, so
#: the install-shaped answer — and the no-manual one — are unreachable from here
#: without passing a search path in, and the code that handles them would go
#: untested against the shape that actually ships.
LOCATIONS: tuple[Path, ...] = (_PACKAGED, _CHECKOUT)

#: Reading order for the index — install it, drive it, understand it, then the
#: pages a reader reaches for once (extending it, what it measures, what it will
#: not do). Alphabetical would open the manual on the architecture page, which is
#: nobody's first question. A page missing from here still lists (appended,
#: alphabetically), so adding a doc never hides it; what is not allowed is a name
#: here with no page behind it, which would be an ordering for a manual that no
#: longer has that shape — ``tests/test_docs.py`` holds both ends.
ORDER: tuple[str, ...] = (
    "install",
    "cli",
    "mcp",
    "retrieval",
    "architecture",
    "format",
    "scope",
    "stability",
    "providers",
    "import-drift",
    "web-viewer",
    "search-quality",
    "related",
)

#: How much of the opening paragraph the index card carries. Long enough to say
#: what the page is about, short enough that the whole manual stays a list.
SUMMARY_CHARS = 200

_SLUG_OK = re.compile(r"\A[a-z0-9][a-z0-9._-]*\Z")

#: Line openers that start something other than a paragraph — see _title_and_summary.
_NOT_PROSE = re.compile(r"(?:[-*+]\s|\d+\.\s|>|#|\||```)")

# Inline markup, unwrapped for the summary: the index renders plain text, so a
# card must not show `**bold**` or a link's target. Structural markup (lists,
# fences) never reaches here — the summary is the first paragraph only.
_INLINE: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),
    (re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)"), r"\1"),
)


@dataclass(frozen=True)
class Page:
    """One manual page: how it is addressed, what it says it is, and where it is."""

    slug: str
    title: str
    summary: str
    path: Path

    def read(self) -> str:
        """The page's markdown source, as written."""
        return self.path.read_text(encoding="utf-8")


def docs_dir(locations: Sequence[Path] = LOCATIONS) -> Optional[Path]:
    """Where this installation's manual is, or ``None`` if it carries none."""
    for candidate in locations:
        if candidate.is_dir() and any(candidate.glob("*.md")):
            return candidate
    return None


def _title_and_summary(text: str, slug: str) -> tuple[str, str]:
    """A page's ``# heading`` and the opening paragraph under it, as plain text.

    Falls back to the slug for a page with no heading: a manual page that lost
    its title is still a page, and an index row reading ``install`` is better
    than one reading nothing.
    """
    title, seen_title = "", False
    summary_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not seen_title:
            if stripped.startswith("# "):
                title, seen_title = stripped[2:].strip(), True
            continue
        if not stripped:
            if summary_lines:
                break
            continue
        # The opening paragraph only: a page that leads with a list, a table, a
        # fence or a subheading (rather than prose) gets no summary rather than a
        # fragment of one. Bold prose is prose — `**The viewer is dev-only**` opens
        # a paragraph, and only `* ` opens a list.
        if not summary_lines and _NOT_PROSE.match(stripped):
            break
        summary_lines.append(stripped)

    summary = " ".join(summary_lines)
    for pattern, repl in _INLINE:
        summary = pattern.sub(repl, summary)
    if len(summary) > SUMMARY_CHARS:
        summary = summary[:SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    return title or slug, summary


def _page(path: Path) -> Page:
    slug = path.stem
    title, summary = _title_and_summary(path.read_text(encoding="utf-8"), slug)
    return Page(slug=slug, title=title, summary=summary, path=path)


def pages(locations: Sequence[Path] = LOCATIONS) -> list[Page]:
    """Every manual page this installation carries, in reading order.

    Empty where there is no manual — a caller renders that as "this install
    ships none" rather than failing.
    """
    directory = docs_dir(locations)
    if directory is None:
        return []
    # One directory deep, deliberately: `docs/*.md` — the directory above this
    # one in a checkout — is the maintainer's half of the manual and is not a
    # reader's to find (see the module docstring).
    found = {p.stem: p for p in sorted(directory.glob("*.md"))}
    ordered = [found.pop(slug) for slug in ORDER if slug in found]
    return [_page(p) for p in ordered + sorted(found.values())]


def find(slug: str, locations: Sequence[Path] = LOCATIONS) -> Optional[Page]:
    """The page addressed by ``slug``, or ``None``.

    ``cli`` and ``cli.md`` both resolve — the second is what a cross-link or a
    copied filename says. The slug is matched against the shape a filename can
    have and then resolved *inside* the docs directory, so a caller's string
    (a URL path segment, a CLI argument) can never walk out of it.
    """
    directory = docs_dir(locations)
    if directory is None:
        return None
    slug = slug.removesuffix(".md")
    if not _SLUG_OK.match(slug):
        return None
    path = directory / f"{slug}.md"
    if not path.is_file() or path.resolve().parent != directory:
        return None
    return _page(path)
