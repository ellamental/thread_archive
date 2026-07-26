"""Field-level drift preservation: the residual of a modeled line survives.

The ledger's warning half (TypeValidator) and preservation half
(``parsers.residual`` → the import seam) must agree on what counts as
unmodeled, and the preserved value must actually land on a stored event — for
an ordinary user turn *and* for a tool-result-only line, whose turn emits no
``user_message_sent`` anchor. Plus the version tripwire: a first-seen harness
version writes one drift-ledger sighting, and only one.
"""

from __future__ import annotations

import json
import random

from sqlalchemy import text

from thread_archive import _api as ta
from thread_archive._store import get_session
from thread_archive._thread_import.parsers.config import get_provider_config
from thread_archive._thread_import.parsers.residual import (
    annotate_unmodeled_fields,
    unmodeled_residual,
)
from thread_archive._thread_import.parsers.validators import validate_messages

from .helpers import cc_assistant, cc_user, write_jsonl


def _generated_json_values() -> list[object]:
    """A deterministic corpus spanning nested JSON types and awkward text."""
    rng = random.Random(20260720)

    def value(depth: int) -> object:
        leaves: list[object] = [
            None,
            rng.choice([True, False]),
            rng.randint(-(2**31), 2**31),
            rng.uniform(-1_000_000, 1_000_000),
            "".join(rng.choice("abc XYZ\n\t☃🦆<>&") for _ in range(rng.randrange(20))),
        ]
        if depth == 0:
            return rng.choice(leaves)
        return rng.choice([
            *leaves,
            [value(depth - 1) for _ in range(rng.randrange(4))],
            {f"k{i}": value(depth - 1) for i in range(rng.randrange(4))},
        ])

    return [value(3) for _ in range(24)]


def _msg(role: str = "user", line: dict | None = None) -> dict:
    return {
        "role": role,
        "content_text": "hi",
        "content_blocks": [],
        "provider_data": {"line": line or {}},
    }


def _payloads(event_type: str) -> list[dict]:
    with get_session() as s:
        rows = s.execute(
            text("SELECT payload FROM events WHERE event_type = :t"), {"t": event_type}
        ).scalars()
        return [json.loads(p) if isinstance(p, str) else p for p in rows]


def test_residual_matches_validator_warnings() -> None:
    """One computation, two consumers: every field the validator warns on is in
    the residual, and only those."""
    config = get_provider_config("claude-code")
    line = {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "x",
                                                      "zzMsgField": 1},
            "zzLineField": {"deep": True}}
    msg = _msg(line=line)
    line_res, msg_res = unmodeled_residual(msg, config)
    assert line_res == {"zzLineField": {"deep": True}}
    assert msg_res == {"zzMsgField": 1}
    context = validate_messages([msg], "c1", "claude-code", batch_safe=True)
    warned = "\n".join(context.warnings)
    assert "user.zzLineField" in warned and "zzMsgField" in warned
    assert "preserved under the anchor event's annotations['unmodeled']" in warned


def test_annotate_writes_the_unmodeled_channel() -> None:
    msg = _msg(line={"zzNew": "v", "message": {"role": "user", "zzInner": 2}})
    assert annotate_unmodeled_fields([msg], "claude-code") == 1
    assert msg["provider_data"]["annotations"]["unmodeled"] == {
        "line": {"zzNew": "v"},
        "message": {"zzInner": 2},
    }
    # A clean message is left untouched.
    clean = _msg(line={"type": "user", "uuid": "u2"})
    assert annotate_unmodeled_fields([clean], "claude-code") == 0
    assert "annotations" not in clean["provider_data"]
    # An unledgered provider has no baseline to diff against.
    assert annotate_unmodeled_fields([_msg(line={"zz": 1})], "no-such-provider") == 0


def test_import_persists_residual_on_user_anchor(archive_home, tmp_path) -> None:
    user = cc_user("resid")
    user["zzFutureField"] = "kept"
    f = tmp_path / "resid.jsonl"
    write_jsonl(f, [user, cc_assistant("resid")])
    ta.import_path(f)
    payloads = _payloads("user_message_sent")
    assert len(payloads) == 1
    assert payloads[0]["annotations"]["unmodeled"]["line"] == {"zzFutureField": "kept"}


def test_import_persists_residual_on_tool_result_only_turn(archive_home, tmp_path) -> None:
    """A tool-result-only line emits no user_message_sent; its first event
    stands in as the annotations anchor."""
    user = cc_user("toolres")
    assistant = cc_assistant("toolres")
    tool_turn = {
        "type": "user", "uuid": "tr-1", "timestamp": "2026-01-01T10:00:06Z",
        "cwd": "/proj", "zzToolField": True,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
        ]},
    }
    f = tmp_path / "toolres.jsonl"
    write_jsonl(f, [user, assistant, tool_turn])
    ta.import_path(f)
    tool_payloads = _payloads("tool_execution_completed")
    assert len(tool_payloads) == 1
    assert tool_payloads[0]["annotations"]["unmodeled"]["line"] == {"zzToolField": True}
    # The real user turn kept its own anchor clean.
    assert "annotations" not in _payloads("user_message_sent")[0]


def test_generated_unknown_json_roundtrips_idempotently(archive_home, tmp_path) -> None:
    """Every JSON value survives import, duplicate import, and truth rebuild."""
    values = _generated_json_values()
    lines: list[dict] = []
    for i, value in enumerate(values):
        name = f"generated-{i}"
        user = cc_user(name, content=f"generated preservation turn {i}")
        user["zzGenerated"] = value
        lines.extend([user, cc_assistant(name)])

    source = tmp_path / "generated.jsonl"
    write_jsonl(source, lines)
    first = ta.import_path(source, source_id="generated-residuals")
    assert first.events_created > 0

    expected = sorted(json.dumps(value, sort_keys=True) for value in values)

    def preserved() -> list[str]:
        return sorted(
            json.dumps(payload["annotations"]["unmodeled"]["line"]["zzGenerated"], sort_keys=True)
            for payload in _payloads("user_message_sent")
        )

    assert preserved() == expected
    assert ta.import_path(source, source_id="generated-residuals").events_created == 0
    ta.reindex()
    assert preserved() == expected


def test_version_tripwire_records_first_sighting_once(archive_home, tmp_path) -> None:
    for name in ("va", "vb"):
        user = cc_user(name)
        user["version"] = "9.9.9-test"
        f = tmp_path / f"{name}.jsonl"
        write_jsonl(f, [user, cc_assistant(name)])
        ta.import_path(f)
    seen = json.loads((archive_home / "seen-versions.json").read_text())
    assert "9.9.9-test" in seen["claude-code"]
    ledger = (archive_home / "validation-drift.jsonl").read_text().splitlines()
    sightings = [
        line for line in ledger
        if "First sighting of claude-code version '9.9.9-test'" in line
    ]
    assert len(sightings) == 1
    # A release is a heads-up, not a finding — it must not count as drift volume.
    assert json.loads(sightings[0])["advisory"] is True
