"""Benchmark adapter registry."""

from __future__ import annotations

from evaluation.core import Adapter


def load_adapter(name: str) -> Adapter:
    if name in ("trace", "combined-trace"):
        from evaluation.adapters.trace import ADAPTER

        return ADAPTER
    if name in ("ponytail", "ponytail-safety"):
        from evaluation.adapters.ponytail import ADAPTER

        return ADAPTER
    if name in ("orchestration", "planning"):
        from evaluation.adapters.orchestration import ADAPTER

        return ADAPTER
    if name in ("ops-trace", "multiagent-ops"):
        from evaluation.adapters.ops_trace import ADAPTER

        return ADAPTER
    if name in ("conversation-trace", "shortcut-trace"):
        from evaluation.adapters.conversation_trace import ADAPTER

        return ADAPTER
    if name in ("bowu_bench", "bowu-bench"):
        from evaluation.adapters.trace import BOWU_BENCH_ADAPTER

        return BOWU_BENCH_ADAPTER
    raise KeyError(name)


def adapter_names() -> list[str]:
    return [
        "trace",
        "bowu_bench",
        "conversation-trace",
        "ops-trace",
        "orchestration",
        "ponytail",
    ]
