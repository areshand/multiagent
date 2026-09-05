FROM node:22-bookworm-slim

WORKDIR /opt/wiki-service
COPY wiki-service/package.json ./
COPY wiki-service/bin/wiki-query.mjs wiki-service/bin/wiki-seed.mjs wiki-service/bin/wiki-steward.mjs ./bin/
COPY wiki-service/src ./src

RUN groupadd --gid 10030 wiki \
 && useradd --no-create-home --uid 10030 --gid 10030 wiki \
 && mkdir -p /var/lib/wiki \
 && chown 10030:10030 /var/lib/wiki

USER 10030:10030
ENV HOST=0.0.0.0 \
    PORT=8080 \
    WIKI_PROFILE=organization \
    WIKI_ROOT=/var/lib/wiki
EXPOSE 8080
CMD ["node", "src/server.mjs"]
