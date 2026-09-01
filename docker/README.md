# Container images

Component image definitions and their container-specific entrypoints live in
this directory. Build the current session-runtime/control-gateway image from the
repository root with:

```bash
docker build -f docker/runtime/Dockerfile -t multiagent:local .
```

The repository root remains the build context so the image can consume the
runtime package, control server, portable framework assets, and shared
contracts. Environment-specific deployment configuration is intentionally not
part of these image definitions.

Build the independently isolated Logger image from the same repository
root with:

```bash
docker build -f docker/logger/Dockerfile -t multiagent-logger:local .
```

The Logger image contains only the Rust `logger` package. It runs as UID
10020, uses a dedicated volume at `/var/lib/logger`, and expects its
Ed25519 signing key and producer-client authorization file to be mounted by the
deployment. It contains neither the session runtime nor the control server.
