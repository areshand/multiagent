# Three-Minute Local Demo

The local demo proves the repository's orchestration and acceptance mechanics
without starting tmux, invoking Codex or Claude, using credentials, or spending
model tokens. It normally finishes in seconds; the three-minute budget includes
reading the transitions and inspecting the resulting evidence.

## Run It

From the repository root:

```bash
./scripts/demo.sh
```

Requirements are Rust 1.75/Cargo, Bash, Git, and Python 3.8 or newer. Set
`MULTIAGENT_DEMO_KEEP=1` to preserve the scratch target and state directory for
inspection:

```bash
MULTIAGENT_DEMO_KEEP=1 ./scripts/demo.sh
```

## What It Demonstrates

The script creates a temporary Git repository with a public behavior check.
The check fails until `answer.txt` contains the required value. It then drives
the production `multiagent subagent` control plane through these states:

| Time | Transition | Meaning |
| ---: | --- | --- |
| 0:00 | `finding-create` and `todo-create` | A verifier finding becomes actionable blocking work. |
| 0:30 | first `gate-check` rejects | Narrative completion cannot bypass an open blocking todo. |
| 1:00 | worker repair and `resolution-create` | The worker records the changed path, command result, and exact diff hash. |
| 1:30 | deterministic verifier recheck | A separate local step reruns the public check and reviews the changed-file set. |
| 2:00 | `todo-close` and `gate-check` accept | Closure names the source finding, covers the worker command, and matches the current diff. |
| 2:30 | post-verification mutation rejects | Previously valid evidence cannot authorize a different patch. |
| 3:00 | verified diff restored and accepted | Acceptance is reproducible for the exact reviewed bytes. |

The deterministic verifier is deliberately simple and is not presented as an
agent-quality benchmark. It replaces only the model judgment for this demo;
the finding store, todo lifecycle, snapshot implementation, verifier artifact
format, closure validation, and final gate are the repository's real local
functionality.

## Expected Evidence

The exact temporary path and SHA-256 vary, but a successful run contains these
key lines:

```text
reject  open-blocking-todo  finding=demo-behavior  todo=demo-repair  status=assigned
final-diff-sha256=<64 hexadecimal characters>
todo closed  demo-repair  verifier-local
accepted  final-gate
reject  closed-todo-final-diff-hash-mismatch  todo=demo-repair
accepted  final-gate
demo: PASS - real orchestration state and hash-bound gate flow verified with no model/API use
```

Tabs separate fields in the actual control-plane output. The important result
is the sequence `reject -> accept -> reject changed diff -> accept restored
diff`, not the generated hash value.

With `MULTIAGENT_DEMO_KEEP=1`, inspect:

```bash
find /path/printed/by/demo/state -type f -maxdepth 4 -print
git -C /path/printed/by/demo/target diff --binary
```

The state directory contains the finding, todo, worker resolution, verifier
message, and closure JSON. All paths are temporary; the demo does not modify
the project checkout.
