FROM node:22-bookworm-slim

WORKDIR /opt/multiagent-slack
COPY slack-ingress/package.json ./
COPY slack-ingress/src ./src

RUN groupadd --gid 10010 multiagent-slack \
 && useradd --no-create-home --uid 10010 --gid 10010 multiagent-slack \
 && mkdir -p /var/lib/multiagent-slack \
 && chown -R 10010:10010 /var/lib/multiagent-slack /opt/multiagent-slack

USER 10010:10010
ENV HOST=0.0.0.0 PORT=8080 SLACK_INGRESS_STATE_DIR=/var/lib/multiagent-slack
EXPOSE 8080
ENTRYPOINT ["node", "src/server.mjs"]
