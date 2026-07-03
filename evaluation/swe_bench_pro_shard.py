"""SWE Bench Pro official-order dataset sharding hooks for EvalScope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def instance_aliases(instance_id: str) -> set[str]:
    aliases = {instance_id}
    if "-v" in instance_id:
        aliases.add(instance_id.rsplit("-v", 1)[0])
    return aliases


@dataclass(frozen=True)
class SampleShard:
    offset: int
    count: int | None
    instances: list[dict[str, Any]]

    @property
    def enabled(self) -> bool:
        return self.offset > 0 or self.count is not None

    @property
    def selected_instances(self) -> list[dict[str, Any]]:
        end = None if self.count is None else self.offset + self.count
        return self.instances[self.offset:end]

    @property
    def selected_instance_ids(self) -> set[str]:
        ids: set[str] = set()
        for item in self.selected_instances:
            ids.update(instance_aliases(str(item["instance_id"])))
        return ids

    @property
    def selected_instance_by_alias(self) -> dict[str, dict[str, Any]]:
        mapping: dict[str, dict[str, Any]] = {}
        for item in self.selected_instances:
            for alias in instance_aliases(str(item["instance_id"])):
                mapping[alias] = item
        return mapping

    def summary(self) -> dict[str, Any]:
        selected = self.selected_instances
        return {
            "enabled": self.enabled,
            "offset": self.offset,
            "count": self.count,
            "selected_count": len(selected),
            "selected_instances": [
                {
                    "official_index": self.offset + index,
                    "instance_id": item["instance_id"],
                    "repo": item.get("repo"),
                    "image": item.get("image"),
                }
                for index, item in enumerate(selected)
            ],
        }


def build_sample_shard(*, offset: int, count: int | None, instances: list[dict[str, Any]]) -> SampleShard:
    if offset < 0:
        raise ValueError("--sample-offset must be >= 0")
    if count is not None and count < 1:
        raise ValueError("--sample-count must be >= 1 when provided")
    if offset > len(instances):
        raise ValueError(f"--sample-offset {offset} is beyond dataset size {len(instances)}")
    shard = SampleShard(offset=offset, count=count, instances=instances)
    if shard.enabled and not shard.selected_instances:
        raise ValueError("sample shard selected zero instances")
    if count is not None and len(shard.selected_instances) != count:
        raise ValueError(
            f"sample shard requested {count} instances at offset {offset}, "
            f"but only {len(shard.selected_instances)} are available"
        )
    return shard


def install_sample_shard_hooks(shard: SampleShard) -> None:
    """Patch EvalScope's SWE Bench Pro adapter class for this Python process."""
    from evalscope.benchmarks.swe_bench_pro.swe_bench_pro_agentic_adapter import SWEBenchProAgenticAdapter

    SWEBenchProAgenticAdapter._codex_sample_shard = shard
    if getattr(SWEBenchProAgenticAdapter, "_codex_sample_shard_hooks", False):
        return

    original_record_to_sample = SWEBenchProAgenticAdapter.record_to_sample

    def record_to_sample(self, record):  # type: ignore[no-untyped-def]
        active_shard = self.__class__._codex_sample_shard
        record = dict(record)
        record_id = str(record.get("instance_id"))
        if active_shard.enabled and record_id not in active_shard.selected_instance_ids:
            return []
        selected = active_shard.selected_instance_by_alias.get(record_id)
        if selected is not None and selected.get("instance_id") and selected.get("instance_id") != record_id:
            record["instance_id"] = selected["instance_id"]
            record["repo"] = selected.get("repo") or record.get("repo")
            record["base_commit"] = selected.get("base_commit") or record.get("base_commit")
        if "fail_to_pass" not in record and "FAIL_TO_PASS" in record:
            record["fail_to_pass"] = record["FAIL_TO_PASS"]
        if "pass_to_pass" not in record and "PASS_TO_PASS" in record:
            record["pass_to_pass"] = record["PASS_TO_PASS"]
        return original_record_to_sample(self, record)

    SWEBenchProAgenticAdapter.record_to_sample = record_to_sample
    SWEBenchProAgenticAdapter._codex_sample_shard_hooks = True
