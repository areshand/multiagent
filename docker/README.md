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
