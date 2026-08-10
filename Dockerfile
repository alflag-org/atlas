FROM python:3.15.0rc1-slim-bookworm@sha256:6e3246a49a188d62360dcd248aafbc1834db4d86eff6b28f40ba13269c1bcc57 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYENV_ROOT=/opt/pyenv \
    ATLAS_HOME=/opt/atlas \
    ATLAS_ETC_DIR=/etc/atlas \
    ATLAS_VAR_DIR=/var/lib/atlas \
    ATLAS_RUNTIME_DIR=/opt/atlas/runtime \
    PATH=/opt/atlas/bin:/opt/atlas/shims:/opt/pyenv/bin:$PATH

WORKDIR /workspace


FROM base AS build-deps

ARG ATLAS_RUNTIME_PYTHON_VERSION=3.14.6
ARG PYENV_VERSION=v2.8.1

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
    && git clone --branch "$PYENV_VERSION" --depth 1 \
        https://github.com/pyenv/pyenv.git "$PYENV_ROOT" \
    && pyenv install -s "$ATLAS_RUNTIME_PYTHON_VERSION"


FROM build-deps AS dev

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip==26.2 \
    && python -m pip install -e '.[dev]'

COPY . .
RUN mkdir -p "$ATLAS_ETC_DIR" "$ATLAS_VAR_DIR" \
    && cp docker/atlas/config.yml "$ATLAS_ETC_DIR/config.yml" \
    && cp docker/atlas/host.yml "$ATLAS_ETC_DIR/host.yml" \
    && atlas release install /workspace/examples/basic-release \
    && atlas release install /workspace/operations

CMD ["sh", "-c", "ruff check src operations tests && pytest -q && python -m build"]


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
RUN python -m pip install --upgrade pip==26.2 \
    && python -m pip install --no-cache-dir /tmp/*.whl \
    && rm -f /tmp/*.whl \
    && chown -R atlas:atlas "$ATLAS_HOME" "$ATLAS_ETC_DIR" "$ATLAS_VAR_DIR" "$PYENV_ROOT" /workspace

USER atlas

CMD ["sh", "-c", "atlas status && atlas release list && atlas command list && hostctl --help >/dev/null && imagectl --help >/dev/null && atlas run sample hello --name=docker"]
