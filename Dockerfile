FROM rust:1.98-bookworm AS multiagent-builder

WORKDIR /src
COPY . .
RUN cargo build --release --locked

FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.145.0
ARG CLAUDE_CODE_VERSION=2.1.239

RUN apt-get update \
 && apt-get install -y --no-install-recommends awscli ca-certificates curl git python3 tmux \
 && rm -rf /var/lib/apt/lists/* \
 && npm install --global "@openai/codex@${CODEX_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

WORKDIR /opt/multiagent
COPY control-server/package*.json control-server/
RUN cd control-server && npm ci --omit=dev
COPY . .
COPY --from=multiagent-builder /src/target/release/multiagent /opt/multiagent/bin/multiagent
RUN chmod +x launch.sh bin/*.sh bin/*.mjs \
 && groupadd --gid 10000 multiagent-control \
 && groupadd --gid 10001 multiagent-role \
 && groupadd --gid 10004 multiagent-credentials \
 && useradd --no-create-home --uid 10000 --gid 10000 --groups 10004 multiagent-control \
 && useradd --no-create-home --uid 10001 --gid 10001 multiagent-orchestrator \
 && useradd --no-create-home --uid 10002 --gid 10001 multiagent-writer \
 && useradd --no-create-home --uid 10003 --gid 10001 multiagent-reader \
 && useradd --no-create-home --uid 10004 --gid 10001 --groups 10004 multiagent-supervisor \
 && useradd --no-create-home --uid 10005 --gid 10001 multiagent-ops \
 && chown root:multiagent-role /opt/multiagent/bin/multiagent \
 && chmod 4755 /opt/multiagent/bin/multiagent \
 && ln -s /opt/multiagent/bin/multiagent /usr/local/bin/multiagent \
 && mkdir -p /var/lib/multiagent/state /var/lib/multiagent/repositories \
    /var/lib/multiagent/role-homes/orchestrator \
    /var/lib/multiagent/role-homes/writer \
    /var/lib/multiagent/role-homes/reader \
    /var/lib/multiagent/role-homes/supervisor \
    /var/lib/multiagent/role-homes/ops \
 && chown -R 10001:10001 /var/lib/multiagent/repositories /var/lib/multiagent/role-homes/orchestrator \
 && chown -R 10002:10001 /var/lib/multiagent/role-homes/writer \
 && chown -R 10003:10001 /var/lib/multiagent/role-homes/reader \
 && chown -R 10004:10001 /var/lib/multiagent/role-homes/supervisor \
 && chown -R 10005:10001 /var/lib/multiagent/role-homes/ops \
 && chmod 0700 /var/lib/multiagent/role-homes/*

USER 10000:10000
ENV HOME=/var/lib/multiagent/control-home \
    CODEX_HOME=/var/lib/multiagent/role-homes/orchestrator/codex \
    CLAUDE_CONFIG_DIR=/var/lib/multiagent/role-homes/orchestrator/claude \
    MULTIAGENT_LAUNCHER_ROOT=/opt/multiagent \
    MULTIAGENT_STATE_DIR=/var/lib/multiagent/state \
    MULTIAGENT_REPOSITORY_ROOT=/var/lib/multiagent/repositories \
    MULTIAGENT_CODEX_HOME_ROOT=/var/lib/multiagent/role-homes \
    MULTIAGENT_UID_SANDBOX=1 \
    MULTIAGENT_CODEX_EXEC=1 \
    PORT=8080
EXPOSE 8080
ENTRYPOINT ["/opt/multiagent/bin/container-entrypoint.sh"]
