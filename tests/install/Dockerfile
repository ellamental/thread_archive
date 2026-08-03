# Isolated install test for thread-archive.
#
# A clean image proves the package installs and works from nothing — no host venv, no
# ambient state. Build context is the repo root (see run_install_test.sh):
#   docker build -f tests/install/Dockerfile -t thread-archive-install-test .
#
# It builds the wheel and installs THAT (never `pip install -e .` — an editable install
# runs from the checkout and can't catch a schema, web asset, or entry point missing
# from the artifact), runs the full unit suite against the installed package, then the
# end-to-end install check (import every provider -> reindex -> search) on the committed
# synthetic corpus.
FROM python:3.12-slim

# build-essential covers any source build of a C-extension dependency (they
# normally resolve to manylinux wheels, but this keeps the build robust on any
# arch). git is product runtime, not a build tool: the code index shells out to
# read-only git in the operator's repos (commits, files, blame), so the suite
# legitimately exercises it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m tester

WORKDIR /app
COPY --chown=tester:tester . /app

# The wheel is installed alone; the test tools come from the `dev` dependency
# group, read out of the copied /app/pyproject.toml. Still pyproject-sourced, so
# a hand-pinned list here can't drift the moment the suite grows a plugin
# dependency (pytest's --strict-markers would then kill collection inside the
# container while the host suite stays green) — but sourced from the file rather
# than from the artifact, because a group is not published metadata and no wheel
# carries one. Two pip steps, not one: `--group` must not be handed the wheel, or
# the group's own pytest pin would resolve against it.
#
# The pip upgrade is load-bearing, not hygiene: `--group` is PEP 735 and needs
# pip >= 25.1, which is newer than the python:3.12-slim base ships.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip build \
    && /opt/venv/bin/python -m build --wheel --outdir /tmp/dist /app \
    && /opt/venv/bin/pip install "$(ls /tmp/dist/*.whl)" \
    && /opt/venv/bin/pip install --group /app/pyproject.toml:dev

# The suite runs unprivileged: root's CAP_DAC_OVERRIDE ignores permission
# bits, so the chmod-based failure-injection tests (read-only dirs, unreadable
# files) can never fire as root.
USER tester
ENV HOME=/home/tester \
    PATH="/opt/venv/bin:$PATH" \
    THREAD_ARCHIVE_HOME=/home/tester/.thread/archive

CMD ["bash", "tests/install/run_in_container.sh"]
