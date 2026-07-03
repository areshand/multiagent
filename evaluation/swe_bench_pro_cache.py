"""Persistent cache hooks for SWE Bench Pro EvalScope sandboxes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


DEFAULT_CACHE_ROOT = Path("/private/tmp/swe-bench-pro-persistent-cache")


class PersistentCacheManager:
    """Build per-image Docker cache mounts that do not touch the task workspace."""

    def __init__(self, *, cache_root: Path, platform: str, mode: str = "rw") -> None:
        if mode not in {"rw", "ro"}:
            raise ValueError("cache mount mode must be rw or ro")
        self.cache_root = cache_root
        self.platform = platform
        self.mode = mode

    def cache_key(self, image: str) -> str:
        digest = hashlib.sha256(f"{self.platform}\n{image}".encode("utf-8")).hexdigest()[:24]
        safe = "".join(ch if ch.isalnum() else "-" for ch in image.lower())[:80].strip("-")
        return f"{safe}-{digest}" if safe else digest

    def overlay(self, image: str) -> dict[str, Any]:
        root = self.cache_root / self.cache_key(image)
        paths = {
            "go-build": "/var/cache/swebench-pro/go-build",
            "go-mod": "/var/cache/swebench-pro/go-mod",
            "npm": "/var/cache/swebench-pro/npm",
            "yarn": "/var/cache/swebench-pro/yarn",
            "pnpm": "/var/cache/swebench-pro/pnpm",
            "pip": "/var/cache/swebench-pro/pip",
            "cargo": "/var/cache/swebench-pro/cargo",
            "gradle": "/var/cache/swebench-pro/gradle",
            "maven": "/var/cache/swebench-pro/maven",
        }
        volumes: dict[str, dict[str, str]] = {}
        for name, container_path in paths.items():
            host_path = root / name
            host_path.mkdir(parents=True, exist_ok=True)
            volumes[str(host_path)] = {"bind": container_path, "mode": self.mode}
        manifest = {
            "image": image,
            "platform": self.platform,
            "cache_key": root.name,
            "container_paths": paths,
        }
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return {
            "volumes": volumes,
            "env_vars": {
                "GOCACHE": paths["go-build"],
                "GOMODCACHE": paths["go-mod"],
                "npm_config_cache": paths["npm"],
                "YARN_CACHE_FOLDER": paths["yarn"],
                "PNPM_HOME": paths["pnpm"],
                "PIP_CACHE_DIR": paths["pip"],
                "CARGO_HOME": paths["cargo"],
                "GRADLE_USER_HOME": paths["gradle"],
                "MAVEN_OPTS": f"-Dmaven.repo.local={paths['maven']}",
            },
        }


def install_persistent_cache_hooks(manager: PersistentCacheManager) -> None:
    """Patch EvalScope's SWE Bench Pro adapter for per-image cache mounts."""
    from evalscope.api.sandbox import merge_sandbox_config_dicts
    from evalscope.benchmarks.swe_bench_pro.swe_bench_pro_agentic_adapter import SWEBenchProAgenticAdapter

    SWEBenchProAgenticAdapter._codex_persistent_cache_manager = manager
    if getattr(SWEBenchProAgenticAdapter, "_codex_persistent_cache_hooks", False):
        return

    original_user_sandbox_config = SWEBenchProAgenticAdapter._user_sandbox_config
    original_build_environment = SWEBenchProAgenticAdapter.build_environment
    original_match_score = SWEBenchProAgenticAdapter.match_score

    def _user_sandbox_config(self):  # type: ignore[no-untyped-def]
        cfg = original_user_sandbox_config(self)
        image = getattr(self, "_codex_persistent_cache_image", "")
        active_manager = self.__class__._codex_persistent_cache_manager
        if image:
            return merge_sandbox_config_dicts(cfg, active_manager.overlay(str(image)))
        return cfg

    def build_environment(self, sample):  # type: ignore[no-untyped-def]
        image = sample.metadata.get("docker_image")
        self._codex_persistent_cache_image = str(image or "")
        try:
            return original_build_environment(self, sample)
        finally:
            self._codex_persistent_cache_image = ""

    def match_score(self, original_prediction, filtered_prediction, reference, task_state):  # type: ignore[no-untyped-def]
        image = task_state.metadata.get("docker_image")
        self._codex_persistent_cache_image = str(image or "")
        try:
            return original_match_score(self, original_prediction, filtered_prediction, reference, task_state)
        finally:
            self._codex_persistent_cache_image = ""

    SWEBenchProAgenticAdapter._user_sandbox_config = _user_sandbox_config
    SWEBenchProAgenticAdapter.build_environment = build_environment
    SWEBenchProAgenticAdapter.match_score = match_score
    SWEBenchProAgenticAdapter._codex_persistent_cache_hooks = True
