# SWE Bench Pro Production Multi-Agent First 50 Summary

Date: 2026-07-03

Scope: first 50 official-order SWE Bench Pro rows, evaluated with the
production-container native multi-agent path.

Result: 31/50 rows passed with official verifier evidence.

Passing official indices:

```text
0, 1, 3, 4, 6, 7, 9, 10, 11, 13, 19, 21, 22, 23, 24, 25, 26, 29, 30,
31, 33, 34, 35, 36, 39, 40, 43, 45, 46, 47, 49
```

Missing official indices:

```text
2, 5, 8, 12, 14, 15, 16, 17, 18, 20, 27, 28, 32, 37, 38, 41, 42, 44, 48
```

The final increment from 30/50 to 31/50 came from row 39:

- Instance: `instance_future-architect__vuls-86b60e1478e44d28b1aff6b9ac7e95ceb05bc5fc`
- Repository: `future-architect/vuls`
- Failing official test: `TestHosts`
- Final focused run prefix:
  `swe-bench-pro-prod-multiagent-v135-vuls-hosts-official-testpatch-offset39-count1`
- Focused run score: `1.0`
- Official verifier evidence: `true`

Key correction for row 39: the public-contract probe now covers literal IP
ignore semantics, including:

```text
hosts("127.0.0.1", []string{"127.0.0.1"}) -> []
```

This fixed the previous official failure where the solver returned
`["127.0.0.1"]` for that hidden contract case.

Important caveat: this score is only meaningful for the production native
multi-agent path because the solver repo is baked into the task image and Codex
auth is mounted at runtime. Earlier scaffold or single-runner results were
infrastructure checks, not clean measurements of production multi-agent
capability.
