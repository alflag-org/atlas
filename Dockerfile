FROM python:3.14.7-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ATLAS_HOME=/opt/atlas \
    ATLAS_ETC_DIR=/etc/atlas \
    ATLAS_VAR_DIR=/var/lib/atlas \
    ATLAS_RUNTIMES_DIR=/opt/atlas/runtimes \
    ATLAS_VENVS_DIR=/opt/atlas/venvs \
    PATH=/opt/atlas/shims:$PATH

WORKDIR /workspace

FROM base AS dev

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip==26.2 \
    && python -m pip install -e '.[dev]'

COPY . .
RUN mkdir -p "$ATLAS_ETC_DIR" "$ATLAS_VAR_DIR" \
    && cp docker/atlas/config.yml "$ATLAS_ETC_DIR/config.yml" \
    && cp docker/atlas/host.yml "$ATLAS_ETC_DIR/host.yml" \
    && atlas runtime install \
    && atlas venv create basic

CMD ["sh", "-c", "ruff check src tests && pytest -q && python -m build"]

FROM dev AS wheel
RUN python -m build

FROM dev AS runtime

RUN groupadd --system atlas \
    && useradd --system --gid atlas --home-dir /opt/atlas --shell /usr/sbin/nologin atlas \
    && chown -R atlas:atlas /opt/atlas /etc/atlas /var/lib/atlas /workspace

USER atlas

CMD ["sh", "-c", "atlas status && atlas command list && atlas run hello docker && atlas run native-echo docker"]
