# syntax=docker/dockerfile:1

# The app is built here and only here: one `next build`, inside the image, so the
# build's memory is the image build's and nothing is built on the host.
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS build
ENV NEXT_TELEMETRY_DISABLED=1 \
    # A build is the heaviest thing this app does. The cap keeps it from growing
    # into the memory of everything else on a small shared machine.
    NODE_OPTIONS=--max-old-space-size=1280
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
# Where the API is for the web container. Next bakes rewrites into the build.
ARG API_ORIGIN=http://api:8000
ENV API_ORIGIN=$API_ORIGIN
RUN npm run build

# Only the standalone server and its static files; no dev dependencies, no source.
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
RUN addgroup -g 10007 -S web && adduser -u 10007 -S -G web -H -s /sbin/nologin web
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
# Appends the connecting address to X-Forwarded-For, which Next.js's /api/* rewrite
# would otherwise pass through as the client wrote it. Preloaded by NODE_OPTIONS in compose.yaml.
COPY --from=build /app/server/forwarded-for.mjs /app/server/preload.mjs ./server/
USER 10007:10007
EXPOSE 3000
CMD ["node", "server.js"]
