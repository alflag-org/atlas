FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYENV_ROOT=/opt/pyenv \
    ATLAS_HOME=/opt/atlas \
    ATLAS_ETC_DIR=/etc/atlas \
    ATLAS_VAR_DIR=/var/lib/atlas \
    ATLAS_RUNTIME_DIR=/opt/atlas/runtime \
    ATLAS_SCRIPTS_DIR=/opt/atlas/scripts/current \
    PATH=/opt/atlas/bin:/opt/atlas/shims:/opt/pyenv/bin:$PATH

WORKDIR /workspace


FROM base AS build-deps

ARG ATLAS_RUNTIME_PYTHON_VERSION=3.12.3

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        libbz2-dev \
        libffi-dev \
        liblzma-dev \
        libreadline-dev \
        libsqlite3-dev \
        libssl-dev \
        make \
        tk-dev \
        xz-utils \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 https://github.com/pyenv/pyenv.git "$PYENV_ROOT" \
    && pyenv install -s "$ATLAS_RUNTIME_PYTHON_VERSION"


FROM build-deps AS dev

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip \
    && python -m pip install -e '.[dev]'

COPY . .
RUN mkdir -p "$ATLAS_ETC_DIR" "$ATLAS_VAR_DIR" \
    && cp docker/atlas/config.yml "$ATLAS_ETC_DIR/config.yml" \
    && cp docker/atlas/host.yml "$ATLAS_ETC_DIR/host.yml" \
    && atlas scripts install /workspace/examples/basic-scripts-release \
    && atlas runtime install

CMD ["sh", "-c", "ruff check src tests && pytest -q && python -m build"]


FROM dev AS wheel

RUN python -m build


FROM base AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        libbz2-1.0 \
        libffi8 \
        liblzma5 \
        libreadline8 \
        libsqlite3-0 \
        libssl3 \
        tk \
        xz-utils \
        zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system atlas \
    && useradd --system --gid atlas --home-dir /opt/atlas --shell /usr/sbin/nologin atlas \
    && mkdir -p "$ATLAS_HOME" "$ATLAS_ETC_DIR" "$ATLAS_VAR_DIR" /workspace

COPY --from=dev /opt/pyenv /opt/pyenv
COPY --from=dev /opt/atlas /opt/atlas
COPY --from=dev /etc/atlas /etc/atlas
COPY --from=wheel /workspace/dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl \
    && rm -f /tmp/*.whl \
    && chown -R atlas:atlas "$ATLAS_HOME" "$ATLAS_ETC_DIR" "$ATLAS_VAR_DIR" "$PYENV_ROOT" /workspace

USER atlas

CMD ["sh", "-c", "atlas status && atlas scripts list && atlas run sample hello --name=docker"]
