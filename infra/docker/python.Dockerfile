# syntax=docker/dockerfile:1.7
# One image for every Python service (api, worker, scheduler, migrate); the compose `command` selects the role.
# Base images pinned by digest (D-009, docs/vendor-notes/containers.md).
ARG PYTHON_IMAGE=mirror.gcr.io/library/python:3.12.15-slim-bookworm@sha256:5d9e6adc9abd558bc0ce05f9ab16cef71328916fc9728f6b81e337af86a737e2

FROM ${PYTHON_IMAGE} AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Optional extra CA bundle for builds behind a TLS-intercepting proxy; empty by default.
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca PIP_CERT=/run/secrets/build_ca; fi; \
    pip install --no-cache-dir uv==0.11.32
COPY pyproject.toml uv.lock .python-version ./
COPY packages ./packages
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY apps/scheduler ./apps/scheduler
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --frozen --no-dev --all-packages --no-editable

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH \
    CONFIG_DIR=/app/config
RUN groupadd --system --gid 10001 rdesk && useradd --system --uid 10001 --gid rdesk --home /app rdesk
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY config ./config
USER rdesk:rdesk
EXPOSE 8000
CMD ["uvicorn", "rd_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]
