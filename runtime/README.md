# Session runtime

This package contains the Rust `multiagent` binary, including the session
runtime, supervisor, role confinement, workflow state, and coding-agent backend
adapters.

Build and test it from the repository root through the Cargo workspace:

```bash
cargo build --locked --package multiagent
cargo test --locked --package multiagent
```

The runtime intentionally consumes portable framework assets from the
repository-level `prompts/`, `contracts/`, and `runbooks/` directories. Concrete
deployment configuration and credentials remain outside this package.
