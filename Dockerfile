FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ATLAS_HOME=/opt/atlas \
    ATLAS_ETC_DIR=/etc/atlas \
    ATLAS_VAR_DIR=/var/lib/atlas \
    ATLAS_RUNTIMES_DIR=/opt/atlas/runtimes \
    ATLAS_ENVIRONMENTS_DIR=/opt/atlas/environments \
    PATH=/opt/atlas/shims:$PATH

WORKDIR /workspace

FROM base AS dev

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential libperl-dev liblocal-lib-perl \
    && rm -rf /var/lib/apt/lists/*

COPY src/python ./src/python
RUN python -m pip install --upgrade pip==26.2 \
    && python -m pip install -e './src/python[dev]'

COPY . .
RUN mkdir -p "$ATLAS_ETC_DIR" "$ATLAS_VAR_DIR" \
    && cp docker/atlas/config.yml "$ATLAS_ETC_DIR/config.yml" \
    && cp docker/atlas/host.yml "$ATLAS_ETC_DIR/host.yml" \
    && atlas runtime install \
    && atlas env create basic \
    && /opt/atlas/environments/python/basic/bin/python -m pip install ./src/python \
    && atlas env create inventory \
    && cd src/perl \
    && perl Makefile.PL INSTALL_BASE=/opt/atlas/environments/perl/inventory \
    && make pure_install

CMD ["sh", "-c", "cd src/python && ruff check atlas atlas_core tests examples && python -m coverage run -m pytest -q && python -m coverage report && python -m build && cd ../perl && perl Makefile.PL && make test dist && prove -v integration/atlas.t"]

FROM dev AS wheel
RUN python -m build src/python

FROM dev AS runtime

RUN groupadd --system atlas \
    && useradd --system --gid atlas --home-dir /opt/atlas --shell /usr/sbin/nologin atlas \
    && chown -R atlas:atlas /opt/atlas /etc/atlas /var/lib/atlas /workspace

USER atlas

CMD ["sh", "-c", "atlas status && atlas command list && atlas run hello docker && atlas run native-echo docker && atlas run perl-context"]
