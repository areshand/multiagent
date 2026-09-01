FROM node:24-bookworm-slim

WORKDIR /opt/multiagent
COPY audit-log/package*.json audit-log/
RUN cd audit-log && npm ci --omit=dev \
 && groupadd --gid 10020 audit-log \
 && useradd --no-create-home --uid 10020 --gid 10020 audit-log \
 && mkdir -p /var/lib/audit-log \
 && chown 10020:10020 /var/lib/audit-log
COPY audit-log audit-log

USER 10020:10020
ENV AUDIT_LOG_DATABASE=/var/lib/audit-log/ledger.sqlite \
    PORT=8090
EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:8090/readyz').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"]
ENTRYPOINT ["node", "/opt/multiagent/audit-log/src/server.mjs"]
