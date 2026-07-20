"""The coverage ratchet must cover every production package it measures."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "coverage_gate", REPO / "scripts" / "coverage_gate.py"
)
coverage_gate = importlib.util.module_from_spec(SPEC)
sys.modules["coverage_gate"] = coverage_gate
assert SPEC.loader is not None
SPEC.loader.exec_module(coverage_gate)


def test_every_source_package_has_a_floor() -> None:
    package_root = REPO / "src" / "thread_archive"
    measured = {
        path.name if path.is_dir() else path.stem
        for path in package_root.iterdir()
        if (path.is_dir() and any(path.rglob("*.py"))) or path.suffix == ".py"
    }
    missing = measured - set(coverage_gate.FLOORS) - coverage_gate.EXEMPT_PACKAGES
    assert not missing, f"production packages without coverage floors: {sorted(missing)}"


def test_report_with_an_unfloored_package_fails(tmp_path, capsys) -> None:
    files = {
        f"src/thread_archive/{name}/module.py": {
            "summary": {
                "covered_lines": 1,
                "num_statements": 1,
                "covered_branches": 0,
                "num_branches": 0,
            }
        }
        for name in coverage_gate.FLOORS
        if name != "TOTAL"
    }
    files["src/thread_archive/future_package/module.py"] = {
        "summary": {
            "covered_lines": 1,
            "num_statements": 1,
            "covered_branches": 0,
            "num_branches": 0,
        }
    }
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({
        "files": files,
        "totals": {
            "covered_lines": len(files),
            "num_statements": len(files),
            "covered_branches": 0,
            "num_branches": 0,
        },
    }))

    assert coverage_gate.main(["coverage_gate.py", str(report)]) == 1
    assert "unfloored production packages: future_package" in capsys.readouterr().out
