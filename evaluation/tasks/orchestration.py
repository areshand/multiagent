"""Deterministic orchestration planning evaluation tasks.

These tasks score an orchestrator-generated plan instead of implementation code.
The evaluation asks the agent to write plan.json with exact node IDs from a
scenario. The scorer checks dependency truth, safe ownership, fan-out, and
explicit consolidation against a small oracle.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any


Score = dict[str, Any]


@dataclass(frozen=True)
class Scenario:
    required: tuple[str, ...]
    dependencies: dict[str, tuple[str, ...]]
    owned_paths: dict[str, tuple[str, ...]]
    consolidation: str
    fanout_min: int
    prompt: str
    good: str
    bad: str


def _update_id(index: int) -> str:
    return f"update-{index:03d}"


def _update_path(index: int) -> str:
    return f"updates/{index:03d}/"


def _validation_id(index: int) -> str:
    return f"validate-{index:03d}"


def _validation_path(index: int) -> str:
    return f"validation/{index:03d}.md"


def _plan_json(workers: list[dict[str, Any]], first_wave: list[str], commands: list[str]) -> str:
    return json.dumps(
        {
            "first_wave": first_wave,
            "first_wave_commands": commands,
            "workers": workers,
        },
        indent=2,
    )


def _assignment_command(node_id: str, owned: str) -> str:
    return (
        f"bin/subagent.sh assignment-create worker-{node_id} "
        f"--assignment-id UPDATE-{node_id} --branch worker/{node_id} --owned {owned} "
        f"&& tmux new-window -d -t \"$MULTIAGENT_SESSION\" -n worker-{node_id} \"$WORKER_COMMAND\""
    )


def make_large_update_scenario(count: int = 300) -> Scenario:
    group_size = 15
    update_ids = tuple(_update_id(index) for index in range(1, count + 1))
    validation_count = count // group_size
    validation_ids = tuple(_validation_id(index) for index in range(1, validation_count + 1))
    consolidation = "consolidate"
    required = (*update_ids, *validation_ids, consolidation)
    dependencies = {node_id: () for node_id in update_ids}
    for group_index, validation_id in enumerate(validation_ids, start=1):
        start = (group_index - 1) * group_size + 1
        end = start + group_size
        dependencies[validation_id] = tuple(_update_id(index) for index in range(start, end))
    dependencies[consolidation] = validation_ids
    owned_paths = {node_id: (_update_path(index),) for index, node_id in enumerate(update_ids, start=1)}
    owned_paths.update(
        {
            node_id: (_validation_path(index),)
            for index, node_id in enumerate(validation_ids, start=1)
        }
    )
    owned_paths[consolidation] = ("updates/summary.md",)

    good_workers = [
        {
            "id": node_id,
            "role": "update",
            "owned_paths": [_update_path(index)],
            "depends_on": [],
        }
        for index, node_id in enumerate(update_ids, start=1)
    ]
    for index, node_id in enumerate(validation_ids, start=1):
        good_workers.append(
            {
                "id": node_id,
                "role": "validation",
                "owned_paths": [_validation_path(index)],
                "depends_on": list(dependencies[node_id]),
            }
        )
    good_workers.append(
        {
            "id": consolidation,
            "role": "integration",
            "owned_paths": ["updates/summary.md"],
            "depends_on": list(validation_ids),
        }
    )
    good_commands = [
        _assignment_command(node_id, _update_path(index))
        for index, node_id in enumerate(update_ids, start=1)
    ]

    bad_workers = []
    previous = ""
    for index, node_id in enumerate(update_ids, start=1):
        bad_workers.append(
            {
                "id": node_id,
                "role": "update",
                "owned_paths": [_update_path(index)],
                "depends_on": [previous] if previous else [],
            }
        )
        previous = node_id
    for index, node_id in enumerate(validation_ids, start=1):
        bad_workers.append(
            {
                "id": node_id,
                "role": "validation",
                "owned_paths": [_validation_path(index)],
                "depends_on": [previous],
            }
        )
        previous = node_id
    bad_workers.append(
        {
            "id": consolidation,
            "role": "integration",
            "owned_paths": ["updates/summary.md"],
            "depends_on": [validation_ids[-1]],
        }
    )

    return Scenario(
        required=required,
        dependencies=dependencies,
        owned_paths=owned_paths,
        consolidation=consolidation,
        fanout_min=count,
        prompt=f"""\
You are planning a repository-wide update that has {count} independent shards.
Every update shard can start immediately; none depends on any other update
shard. After the update wave, there are {validation_count} validation workers.
Each validation worker owns one chunk of {group_size} consecutive update shards:
validate-001 checks update-001 through update-015, validate-002 checks
update-016 through update-030, and so on through validate-{validation_count:03d}.
The only final dependency is that consolidate runs after all validation workers
finish.

Use exactly these worker IDs:
update-001 through update-{count:03d}, validate-001 through
validate-{validation_count:03d}, plus consolidate.

Owned paths follow this exact mapping:
- update-NNN owns updates/NNN/
- validate-NNN owns validation/NNN.md
- consolidate owns updates/summary.md

Important traps:
- Do not serialize update workers by numeric order.
- Do not make update workers depend on validation workers.
- Do not make consolidate depend directly on update workers; it depends on the
  validation layer.
- Do not merge validation into consolidate.

Write only plan.json. It must contain all {count + validation_count + 1} workers in
{{"workers": [...]}} where each worker has id, role, owned_paths, depends_on.
Also include first_wave with all {count} update worker IDs, and
first_wave_commands with repo-native assignment/spawn command templates for all
{count} update workers.
""",
        good=_plan_json(good_workers, list(update_ids), good_commands),
        bad=_plan_json(bad_workers, [update_ids[0]], [_assignment_command(update_ids[0], _update_path(1))]),
    )


def _fail(reason: str, **extra: Any) -> Score:
    return {"correct": 0, "safe": 0, "reason": reason, **extra}


def _summarize(items: list[str], limit: int = 12) -> str:
    if len(items) <= limit:
        return ",".join(items)
    shown = ",".join(items[:limit])
    return f"{shown},...(+{len(items) - limit} more)"


def _node_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_nodes = plan.get("workers") or plan.get("nodes") or []
    nodes: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_nodes, list):
        return nodes
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        node_id = raw.get("id") or raw.get("node_id") or raw.get("name")
        if isinstance(node_id, str) and node_id:
            nodes[node_id] = raw
    return nodes


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _strings_deep(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            items.extend(_strings_deep(item))
        return items
    if isinstance(value, dict):
        items: list[str] = []
        for item in value.values():
            items.extend(_strings_deep(item))
        return items
    return []


def _waves(nodes: dict[str, dict[str, Any]]) -> dict[str, int]:
    memo: dict[str, int] = {}

    def wave(node_id: str, stack: set[str]) -> int:
        if node_id in memo:
            return memo[node_id]
        if node_id in stack:
            memo[node_id] = 999
            return memo[node_id]
        deps = [dep for dep in _string_list(nodes[node_id].get("depends_on")) if dep in nodes]
        if not deps:
            memo[node_id] = 0
            return 0
        memo[node_id] = 1 + max(wave(dep, stack | {node_id}) for dep in deps)
        return memo[node_id]

    return {node_id: wave(node_id, set()) for node_id in nodes}


def _has_owned_overlap(nodes: dict[str, dict[str, Any]]) -> bool:
    seen: dict[str, str] = {}
    for node_id, node in nodes.items():
        for path in _string_list(node.get("owned_paths") or node.get("owned")):
            normalized = path.rstrip("/")
            if not normalized:
                continue
            owner = seen.get(normalized)
            if owner and owner != node_id:
                return True
            seen[normalized] = node_id
    return False


def score_plan(workdir: Path, scenario: Scenario) -> Score:
    path = workdir / "plan.json"
    if not path.exists():
        return _fail("plan.json missing")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _fail(f"plan.json is not valid JSON: {exc}")

    nodes = _node_map(plan)
    missing = [node_id for node_id in scenario.required if node_id not in nodes]
    if missing:
        return _fail(f"missing required nodes: {', '.join(missing)}")

    extra_deps: list[str] = []
    missing_deps: list[str] = []
    owned_mismatches: list[str] = []
    for node_id, expected in scenario.dependencies.items():
        actual = set(_string_list(nodes[node_id].get("depends_on")))
        expected_set = set(expected)
        for dep in sorted(actual - expected_set):
            extra_deps.append(f"{node_id}->{dep}")
        for dep in sorted(expected_set - actual):
            missing_deps.append(f"{node_id}->{dep}")
    for node_id, expected in scenario.owned_paths.items():
        actual = set(_string_list(nodes[node_id].get("owned_paths") or nodes[node_id].get("owned")))
        expected_set = set(expected)
        if actual != expected_set:
            owned_mismatches.append(node_id)

    overlap = _has_owned_overlap(nodes)
    wave_by_node = _waves(nodes)
    first_wave = [node_id for node_id in scenario.required if wave_by_node.get(node_id) == 0]
    worker_waves = [
        wave_by_node[node_id]
        for node_id in scenario.required
        if node_id != scenario.consolidation and node_id in wave_by_node
    ]
    wave_counts = {wave: worker_waves.count(wave) for wave in sorted(set(worker_waves))}
    max_concurrent_agents = max(wave_counts.values()) if wave_counts else 0
    avg_concurrent_agents = round(statistics.mean(wave_counts.values()), 3) if wave_counts else 0
    concurrency_ratio = (
        round(max_concurrent_agents / len(worker_waves), 3) if worker_waves else 0
    )
    declared_first_wave = set(
        _string_list(plan.get("first_wave") or plan.get("ready_workers") or plan.get("spawn_first"))
    )
    first_wave_declared = set(first_wave).issubset(declared_first_wave)
    first_wave_commands = _strings_deep(
        plan.get("first_wave_commands") or plan.get("spawn_commands") or plan.get("commands")
    )
    command_text = "\n".join(first_wave_commands)
    commands_cover_first_wave = all(node_id in command_text for node_id in first_wave)
    uses_repo_spawn_commands = "bin/subagent.sh assignment-create" in command_text and (
        "tmux new-window" in command_text or "bin/subagent.sh spawn" in command_text
    )
    consolidation_deps = set(_string_list(nodes[scenario.consolidation].get("depends_on")))
    required_before_consolidation = set(scenario.dependencies[scenario.consolidation])
    consolidates = consolidation_deps == required_before_consolidation

    correct = not missing_deps and not extra_deps and not owned_mismatches and not overlap
    safe = (
        correct
        and len(first_wave) >= scenario.fanout_min
        and first_wave_declared
        and commands_cover_first_wave
        and uses_repo_spawn_commands
        and consolidates
    )

    reason_parts = []
    if missing_deps:
        reason_parts.append("missing deps " + _summarize(missing_deps))
    if extra_deps:
        reason_parts.append("false deps " + _summarize(extra_deps))
    if overlap:
        reason_parts.append("overlapping owned paths")
    if owned_mismatches:
        reason_parts.append("owned path mismatch " + _summarize(owned_mismatches))
    if len(first_wave) < scenario.fanout_min:
        reason_parts.append(f"fanout {len(first_wave)} < {scenario.fanout_min}")
    if not first_wave_declared:
        reason_parts.append("missing explicit first_wave")
    if not commands_cover_first_wave:
        reason_parts.append("first_wave_commands do not cover ready workers")
    if not uses_repo_spawn_commands:
        reason_parts.append("missing repo-native spawn commands")
    if not consolidates:
        reason_parts.append("missing final consolidation gate")
    return {
        "correct": int(correct),
        "safe": int(safe),
        "reason": "; ".join(reason_parts) if reason_parts else "ok",
        "fanout": len(first_wave),
        "first_wave_agents": len(first_wave),
        "max_concurrent_agents": max_concurrent_agents,
        "avg_concurrent_agents": avg_concurrent_agents,
        "concurrency_ratio": concurrency_ratio,
        "max_wave": max(wave_by_node.values()) if wave_by_node else 0,
        "nodes": len(nodes),
        "first_wave_declared": int(first_wave_declared),
        "repo_spawn_commands": int(uses_repo_spawn_commands),
    }


SCENARIOS = {
    "large-update-300": make_large_update_scenario(300),
}
