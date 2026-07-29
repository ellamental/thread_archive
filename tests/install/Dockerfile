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
# arch). git is product runtime, not a build tool: a from-source install is a
# clone and self-update drives git directly, so the suite legitimately
# exercises it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m tester

WORKDIR /app
COPY --chown=tester:tester . /app

# The test tools come from the wheel's own [dev] extra — a hand-pinned list
# here drifts from pyproject the moment the suite grows a plugin dependency
# (pytest's --strict-markers then kills collection inside the container while
# the host suite stays green).
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip build \
    && /opt/venv/bin/python -m build --wheel --outdir /tmp/dist /app \
    && /opt/venv/bin/pip install "$(ls /tmp/dist/*.whl)[dev]"

# The suite runs unprivileged: root's CAP_DAC_OVERRIDE ignores permission
# bits, so the chmod-based failure-injection tests (read-only dirs, unreadable
# files) can never fire as root.
USER tester
ENV HOME=/home/tester \
    PATH="/opt/venv/bin:$PATH" \
    THREAD_ARCHIVE_HOME=/home/tester/.thread/archive

CMD ["bash", "tests/install/run_in_container.sh"]
