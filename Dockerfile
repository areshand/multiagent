FROM rust:1-bookworm AS multiagent-builder

WORKDIR /src
COPY . .
RUN cargo build --release --locked

FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.145.0
ARG CLAUDE_CODE_VERSION=2.1.239

RUN apt-get update \
 && apt-get install -y --no-install-recommends awscli ca-certificates git python3 tmux \
 && rm -rf /var/lib/apt/lists/* \
 && npm install --global "@openai/codex@${CODEX_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

WORKDIR /opt/multiagent
COPY control-server/package*.json control-server/
RUN cd control-server && npm ci --omit=dev
COPY . .
COPY --from=multiagent-builder /src/target/release/multiagent /opt/multiagent/bin/multiagent
RUN chmod +x launch.sh bin/*.sh bin/*.mjs \
 && useradd --create-home --home-dir /var/lib/multiagent --uid 10001 multiagent \
 && mkdir -p /var/lib/multiagent/state /var/lib/multiagent/repositories \
 && chown -R multiagent:multiagent /var/lib/multiagent

USER 10001:10001
ENV HOME=/var/lib/multiagent/home \
    CODEX_HOME=/var/lib/multiagent/codex \
    CLAUDE_CONFIG_DIR=/var/lib/multiagent/claude \
    MULTIAGENT_LAUNCHER_ROOT=/opt/multiagent \
    MULTIAGENT_STATE_DIR=/var/lib/multiagent/state \
    MULTIAGENT_REPOSITORY_ROOT=/var/lib/multiagent/repositories \
    PORT=8080
EXPOSE 8080
ENTRYPOINT ["/opt/multiagent/bin/container-entrypoint.sh"]
