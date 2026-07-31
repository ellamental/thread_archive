# Internal docs

`docs/` is the maintainer's half of the manual. These pages name branches, gates
and instruments no install has: the release process, the benchmark landscape,
and the dev panels served by `python -m devweb`.

- [releasing.md](releasing.md) — cutting a version, the preflight gates
- [benchmarks.md](benchmarks.md) — the bench landscape and what each harness measures
- [devweb.md](devweb.md) — the dev panels, run from a clone
- [performance.md](performance.md) — the retrieval latency situation and the history of what has been tried

They stay in the repo. The wheel carries `docs/public/` and nothing else under
`docs/`, and the two readers over the manual — `thread-archive docs` and the
viewer's `/docs` — list [public/](public/) and nothing here. That is the whole
split: which directory a page sits in. A page written for someone running
thread-archive belongs in `docs/public/`; a page written for someone working on
it belongs here, which is the default — publishing is the deliberate move.
