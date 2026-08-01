"""Every built-in provider, held to the contract a plugin is held to.

The provider API's claim is that the built-ins go through it — "a seam they need
and a plugin can't reach is a bug in the API rather than a private convenience"
(:mod:`thread_archive._providers.builtins`). This file is where that stops being
a claim about the descriptor's *shape* and becomes one about its behaviour: the
sweep runs the shipped
:func:`thread_archive.provider.testing.assert_provider_contract`, so a regression
in the kit reds here before a plugin author ever meets it. (Its sibling,
``assert_reimport_adds_nothing``, is dogfooded in tests/test_importer_providers.py,
over the fixtures those importers already have.)

What the parameterization buys over the same assertions written per provider: the
sweep is over ``builtin_providers()``, so a provider added tomorrow is covered the
moment it is registered rather than when someone remembers to write its test.

These run in the suite's throwaway ``$HOME``, so **no harness store exists** —
which is the state this file most wants. A machine without Cursor installed is
the normal case for the cursor watcher, and a watcher that misbehaves there
(raises, or claims a store it hasn't got) is a cost every poll pays and a line in
every health record, on a machine whose operator never installed that harness.
"""

from __future__ import annotations

import pytest

from thread_archive._importers import import_session_incremental
from thread_archive._providers.builtins import builtin_providers
from thread_archive.provider.testing import (
    assert_provider_contract,
    assert_reimport_adds_nothing,
    init_archive,
    write_jsonl,
)

BUILTINS = builtin_providers()
BY_NAME = {p.name: p for p in BUILTINS}


@pytest.mark.parametrize("provider", BUILTINS, ids=lambda p: p.name)
def test_builtin_provider_conforms(provider) -> None:
    """Descriptor + watcher, on a machine that has none of these harnesses."""
    assert_provider_contract(provider)


def test_the_sweep_covers_the_whole_registry() -> None:
    """A guard on the parameterization itself: an empty or truncated
    ``builtin_providers()`` would make every test above pass by vacuum."""
    assert len(BUILTINS) >= 10
    assert {"claude-code", "codex", "cursor", "opencode"} <= set(BY_NAME)


def test_provider_names_are_unique() -> None:
    """The name is identity. Two descriptors sharing one would have the later
    silently shadow the earlier in the registry's name-keyed build — the source
    would still list, still filter, and hold half its conversations."""
    names = [p.name for p in BUILTINS]
    assert len(names) == len(set(names))


def test_a_following_source_polls_after_the_one_it_follows() -> None:
    """A recovery pass over another source's store has to run second, so the
    primary import establishes its dedup keys first and the recovery writes only
    what is genuinely lost. Declared as two independent fields (``follows`` and
    ``order``), so nothing but this keeps them agreeing."""
    for p in BUILTINS:
        if p.follows:
            assert p.order > BY_NAME[p.follows].order, (
                f"{p.name} follows {p.follows} but polls before it, so it would "
                f"recover content the primary import is about to write anyway"
            )


CLAUDE_CODE = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
     "cwd": "/proj", "message": {"role": "user", "content": "hello"}},
    {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z", "sessionId": "s1",
     "message": {"role": "assistant", "model": "claude-opus-4",
                 "content": [{"type": "text", "text": "hi"}]}},
]


def test_claude_code_reimport_adds_nothing(archive_home) -> None:
    """The first-class source, through the same helper as the rest.

    Its own suite (tests/test_importer_claude_code.py) covers resumption from a
    watermark in detail; what this adds is the plain invariant stated the way a
    plugin author reads it — import twice, gain nothing, and leave a watermark.
    """
    init_archive()
    path = archive_home / "session.jsonl"
    write_jsonl(path, CLAUDE_CODE)

    first, second = assert_reimport_adds_nothing(
        lambda: import_session_incremental(path, "proj:s1"), source="claude-code"
    )
    assert first.is_new_thread and first.events_created > 0
    assert second.events_created == 0
