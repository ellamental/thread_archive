# Isolated install test for thread-archive.
#
# A clean image proves the package installs and works from nothing — no host venv, no
# ambient state. Build context is the repo root (see run_install_test.sh):
#   docker build -f tests/install/Dockerfile -t thread-archive-install-test .
#
# It builds the wheel and installs THAT (never `pip install -e .` — an editable install
# runs from the checkout and can't catch a schema, web asset, or entry point missing
# from the artifact), runs the full unit suite against the installed package, then the
# end-to-end install check (import every provider -> reindex -> search) on a synthetic
# corpus (or an obfuscated real one mounted at /work/fixtures).
FROM python:3.14-slim

# build-essential covers any source build of the Leiden C extensions (igraph/leidenalg
# normally resolve to manylinux wheels, but this keeps the build robust on any arch).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip build \
    && /opt/venv/bin/python -m build --wheel --outdir /tmp/dist /app \
    && /opt/venv/bin/pip install "$(ls /tmp/dist/*.whl)" \
    && /opt/venv/bin/pip install pytest pytest-cov ruff

ENV PATH="/opt/venv/bin:$PATH" \
    THREAD_ARCHIVE_HOME=/root/.thread/archive

CMD ["bash", "tests/install/run_in_container.sh"]
