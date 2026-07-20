"""Generate THIRD_PARTY_NOTICES.md for the committed web-viewer bundle.

Walks the production-dependency closure of frontend/package.json through
frontend/node_modules (the same set vite bundles into _web/static) and emits
each package's name, version, license identifier, and license text.
"""
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
nm = repo / "frontend" / "node_modules"
root_deps = json.load(open(repo / "frontend" / "package.json"))["dependencies"]

seen: dict[str, Path] = {}
stack = sorted(root_deps)
while stack:
    name = stack.pop()
    if name in seen:
        continue
    pdir = nm / name
    if not (pdir / "package.json").is_file():
        print(f"WARNING: {name} not found in node_modules", file=sys.stderr)
        continue
    seen[name] = pdir
    meta = json.load(open(pdir / "package.json"))
    stack.extend(sorted(set(meta.get("dependencies", {})) - set(seen)))

out = [
    "# Third-party notices — bundled web viewer",
    "",
    "The read-only web viewer ships as a pre-built JavaScript bundle",
    "(`_web/static/`). The bundle contains the following open-source packages,",
    "reproduced here with their license texts as their licenses require. This",
    "file covers only the bundled frontend; the Python package's dependencies",
    "are installed from PyPI under their own licenses and are not vendored.",
    "",
]
counts: dict[str, int] = {}
for name in sorted(seen):
    pdir = seen[name]
    meta = json.load(open(pdir / "package.json"))
    lic = meta.get("license") or "(see package)"
    if isinstance(lic, dict):
        lic = lic.get("type", "(see package)")
    counts[lic] = counts.get(lic, 0) + 1
    text = None
    for cand in ("LICENSE", "LICENSE.md", "LICENSE.txt", "license", "License.md",
                 "LICENCE", "LICENCE.md", "COPYING"):
        f = pdir / cand
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="replace").strip()
            break
    out.append(f"## {name}@{meta.get('version', '?')} — {lic}")
    out.append("")
    if text:
        out.append("```text")
        out.append(text)
        out.append("```")
    else:
        out.append(f"(no license file shipped in the package; declared license: {lic})")
    out.append("")

dest = repo / "src" / "thread_archive" / "_web" / "THIRD_PARTY_NOTICES.md"
dest.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"{len(seen)} packages -> {dest}")
print("licenses:", json.dumps(counts, indent=1))
