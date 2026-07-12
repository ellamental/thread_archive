"""The durability kit: backup, verify, the nightly pipeline, health records.

Private, like every underscore-prefixed package. :mod:`.._api` re-exports the
public entry points (``backup`` / ``restore_drill`` / ``verify`` / ``nightly``)
so the CLI and every other caller keep one coordination surface; the
implementations live here so the API layer stays a dispatch layer.

Modules: :mod:`.backup` (truth mirror + generations + restore drill),
:mod:`.verify` (the integrity tiers), :mod:`.nightly` (the scheduled pipeline),
:mod:`.health` (``<home>/health.json`` records + the pipeline verdict and
family-monitor heartbeat). No convenience re-exports here — several entry
points share their submodule's name, and shadowing the submodules would break
``from thread_archive._ops import backup``-style module access.
"""
