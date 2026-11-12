# syntax=docker/dockerfile:1
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
# INNR auto-off service. Multi-arch (linux/arm64 for the Mac, linux/amd64 for the Synology NAS).
# Base image pinned by multi-arch index digest (python 3.13.18-slim-bookworm, resolved 2026-11-11).
# Rebuild every 1 to 2 months with a refreshed digest to pick up OS security fixes.
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

# ---------------------------------------------------------------- build: pinned, hash-checked deps
FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt /build/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps -r /build/requirements.txt
COPY src/ /opt/app/src/
RUN /opt/venv/bin/python -m compileall -q /opt/app/src

# ---------------------------------------------------------------- test: runs the suite inside Linux
FROM build AS test
COPY requirements-dev.txt pyproject.toml /opt/app/
COPY tests/ /opt/app/tests/
COPY deploy/config.example.toml deploy/merge-client-profile.sh /opt/app/deploy/
RUN /opt/venv/bin/pip install --no-deps -r /opt/app/requirements-dev.txt
WORKDIR /opt/app
RUN /opt/venv/bin/python -m pytest -p no:cacheprovider

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/thoge191/innr_home_automation" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.title="innr-autooff" \
      org.opencontainers.image.description="Auto-off timers for INNR smart plugs (local state, cloud commands)"
RUN groupadd --gid 10001 innr \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin innr \
 && mkdir -p /data /config \
 && chown 10001:10001 /data \
 && printf '#!/bin/sh\nexec python -m innr_autooff "$@"\n' > /usr/local/bin/innr-autooff \
 && chmod 755 /usr/local/bin/innr-autooff
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/app/src /opt/app/src
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/opt/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    INNR_CONFIG=/config/config.toml \
    INNR_DATA_DIR=/data \
    INNR_PASSWORD_FILE=/run/secrets/innr_password
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8480
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 CMD ["innr-autooff", "healthcheck"]
ENTRYPOINT ["innr-autooff"]
CMD ["run"]
