# mac control plane — production container.
#
# Multi-stage build: install deps into a slim image, run as non-root with a
# pinned working directory.
#
# MAC_SECRET_KEY and MAC_DB MUST both be provided at runtime. MAC_DB must be a
# postgres:// or postgresql:// DSN -- SQLite support was removed in #261, and
# this image used to default MAC_DB to /var/lib/mac/mac.db, so every container
# exited at startup with "unsupported control-plane DSN". Shipping no default
# is deliberate: a missing DSN now fails with a clear message about what to
# supply, rather than with a confident-looking path that cannot work.

FROM ghcr.io/astral-sh/uv@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 AS uv

FROM docker.io/library/python@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/mac-venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
      --extra postgres --extra k8s --extra hermes-gateway \
    && /opt/mac-venv/bin/python -c \
      "import cryptography, fastapi, kubernetes, psycopg, uvicorn, yaml"


FROM docker.io/library/python@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH=/opt/mac-venv/bin:/usr/local/bin:/usr/bin:/bin

RUN printf '%s\n' 'mac:x:10001:' >> /etc/group && \
    printf '%s\n' 'mac:x:10001:10001:MAC service:/var/lib/mac:/usr/sbin/nologin' >> /etc/passwd && \
    mkdir -p /var/lib/mac && \
    chown -R 10001:10001 /var/lib/mac

COPY --from=builder /opt/mac-venv /opt/mac-venv
COPY --chmod=0755 deploy/mac-crash-observer.py /usr/local/bin/mac-crash-observer
# The copied environment is resolved exclusively from uv.lock, including the
# postgres, k8s, and hermes-gateway extras needed by the shared deployment
# image. Runtime backend selection still comes from environment configuration.

USER mac
WORKDIR /var/lib/mac
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        r = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); \
        sys.exit(0 if r.status == 200 else 1)" || exit 1

CMD ["uvicorn", "mac.api:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--log-level", "info"]
