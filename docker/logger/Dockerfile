FROM rust:1.98-bookworm AS builder
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY runtime runtime
COPY logger logger
RUN cargo build --locked --release -p multiagent-logger

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10020 logger \
 && useradd --no-create-home --uid 10020 --gid 10020 logger \
 && mkdir -p /var/lib/logger \
 && chown 10020:10020 /var/lib/logger
COPY --from=builder /src/target/release/logger /usr/local/bin/logger
USER 10020:10020
ENV LOGGER_LEDGER_FILE=/var/lib/logger/ledger.jsonl PORT=8090
EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["curl", "--fail", "--silent", "http://127.0.0.1:8090/readyz"]
ENTRYPOINT ["/usr/local/bin/logger", "serve"]
