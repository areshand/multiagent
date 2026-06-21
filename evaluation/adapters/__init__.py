"""Benchmark adapter registry."""

from __future__ import annotations

from evaluation.core import Adapter


def load_adapter(name: str) -> Adapter:
    if name in ("ponytail", "ponytail-safety"):
        from evaluation.adapters.ponytail import ADAPTER

        return ADAPTER
    if name in ("orchestration", "planning"):
        from evaluation.adapters.orchestration import ADAPTER

        return ADAPTER
    raise KeyError(name)


def adapter_names() -> list[str]:
    return ["orchestration", "ponytail"]
