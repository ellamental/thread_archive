# Contributing

This project doesn't accept code contributions. That is a deliberate policy: it
is one maintainer's name on software that holds people's entire conversation
history, and every merged line is something that name has to have read,
understood, and become responsible for. Reviewing a chunk of code that
presumably works costs more than writing it, so PRs — including agent-written
ones — won't be merged. The constraint is the trust surface, not the quality of
the work offered.

What does help:

- **Bug reports are genuinely welcome** — especially import drift (a provider
  changed its on-disk format and something degraded). Open an issue with the
  `thread-archive source coverage` / `thread-archive status` output if you have it.
- **Ideas and design suggestions**: open an issue and talk it through. If
  something substantial comes out of it, it gets written here, with the
  discussion as input.
- **Provider support is the sanctioned extension point.** A new or fixed
  provider doesn't need a PR at all: the plugin API
  ([docs/providers.md](docs/providers.md)) lets you write and maintain a
  provider in your own repo, and `thread-archive source fix` scaffolds a local
  repair when a built-in one drifts.
- **Fork it.** MIT license, no CLA, genuinely encouraged — if you want to take
  it somewhere we wouldn't, that's the right vehicle, not a patch queue.

## Security Reporting

Security reports go through [SECURITY.md](SECURITY.md), not the issue
tracker.
