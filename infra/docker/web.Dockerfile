# syntax=mirror.gcr.io/docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Next.js standalone image. Base pinned by digest (D-009).
ARG NODE_IMAGE=mirror.gcr.io/library/node:22.22.0-bookworm-slim@sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94

FROM ${NODE_IMAGE} AS builder
ENV NEXT_TELEMETRY_DISABLED=1 CI=1
WORKDIR /repo
COPY config ./config
COPY apps/web/package.json apps/web/pnpm-lock.yaml ./apps/web/
WORKDIR /repo/apps/web
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; \
    corepack enable && pnpm install --frozen-lockfile  # corepack verifies the packageManager sha512
COPY apps/web ./
RUN pnpm build

FROM ${NODE_IMAGE} AS runtime
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0 CONFIG_DIR=/app/config
WORKDIR /app
RUN groupadd --system --gid 10001 rdesk && useradd --system --uid 10001 --gid rdesk --home /app rdesk
COPY --from=builder /repo/apps/web/.next/standalone ./
COPY --from=builder /repo/apps/web/.next/static ./.next/static
COPY --from=builder /repo/apps/web/public ./public
COPY config ./config
USER rdesk:rdesk
EXPOSE 3000
CMD ["node", "server.js"]
