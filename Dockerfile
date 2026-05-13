FROM python:3.12-slim

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

COPY . .
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
    && git clone --depth 1 https://github.com/pyenv/pyenv.git "$PYENV_ROOT" \
    && python -m pip install --upgrade pip \
    && python -m pip install -e '.[dev]' \
    && python -m pip install build \
    && mkdir -p /etc/atlas /var/lib/atlas \
    && cp docker/atlas/config.yml /etc/atlas/config.yml \
    && cp docker/atlas/host.yml /etc/atlas/host.yml \
    && atlas runtime install \
    && atlas scripts install /workspace/examples/scripts-release

CMD ["sh", "-c", "atlas status && atlas scripts list && atlas run sample hello --name=docker"]
