"""On-demand SWE Bench Pro image loading hooks for EvalScope.

EvalScope's SWE Bench Pro adapter asks ms-enclave to create a Docker sandbox
from the per-instance ``jefzda/sweap-images`` tag. In this environment direct
Docker pulls are unreliable and the full 731-image set is too large to keep
resident, so these hooks ensure the required image exists immediately before a
sample starts and optionally remove it after scoring.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from evaluation.swe_bench_pro_image_preload import (
    docker_image_present,
    free_disk_gib,
    preload_image_with_retries,
)


class OnDemandImageManager:
    def __init__(
        self,
        *,
        archive_dir: Path,
        status_path: Path,
        platform: str,
        image_timeout: int | None,
        retries: int,
        backoff_s: int,
        min_free_gb: float,
        prune_after_sample: bool,
        bake_native_solver: bool = False,
        native_solver_source: Path | None = None,
    ) -> None:
        self.archive_dir = archive_dir
        self.status_path = status_path
        self.platform = platform
        self.image_timeout = image_timeout
        self.retries = retries
        self.backoff_s = backoff_s
        self.min_free_gb = min_free_gb
        self.prune_after_sample = prune_after_sample
        self.bake_native_solver = bake_native_solver
        self.native_solver_source = native_solver_source or Path(__file__).resolve().parents[1]
        self.records: list[dict[str, Any]] = []
        self.counts = {
            "already_present": 0,
            "loaded": 0,
            "failed": 0,
            "stopped_low_disk": 0,
            "baked": 0,
            "bake_reused": 0,
            "bake_failed": 0,
            "pruned": 0,
            "prune_failed": 0,
        }

    def _write(self, status: str) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(
            json.dumps(
                {
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "status": status,
                    "archive_dir": str(self.archive_dir),
                    "platform": self.platform,
                    "min_free_gb": self.min_free_gb,
                    "prune_after_sample": self.prune_after_sample,
                    "bake_native_solver": self.bake_native_solver,
                    "native_solver_source": str(self.native_solver_source) if self.bake_native_solver else None,
                    "counts": self.counts,
                    "records": self.records,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def ensure_image(self, image: str, instance_id: str) -> str:
        present, inspect_error = docker_image_present(image)
        if present:
            self.counts["already_present"] += 1
            self.records.append({"instance_id": instance_id, "image": image, "status": "already_present"})
            self._write("running")
            return self._ensure_baked_image(image, instance_id)

        if self.min_free_gb > 0:
            free_gib = free_disk_gib(self.archive_dir)
            if free_gib < self.min_free_gb:
                self.counts["stopped_low_disk"] += 1
                self.records.append(
                    {
                        "instance_id": instance_id,
                        "image": image,
                        "status": "stopped_low_disk",
                        "free_gib": round(free_gib, 3),
                        "min_free_gb": self.min_free_gb,
                        "inspect_error": inspect_error,
                    }
                )
                self._write("stopped_low_disk")
                raise RuntimeError(
                    f"not enough free disk to preload {image}: "
                    f"{free_gib:.3f} GiB free < {self.min_free_gb:.3f} GiB required"
                )

        record = preload_image_with_retries(
            image,
            self.platform,
            self.archive_dir,
            keep_archive=False,
            image_timeout=self.image_timeout,
            retries=self.retries,
            backoff_s=self.backoff_s,
        )
        record["instance_id"] = instance_id
        self.records.append(record)
        if record.get("status") == "loaded":
            self.counts["loaded"] += 1
            self._write("running")
            return self._ensure_baked_image(image, instance_id)

        self.counts["failed"] += 1
        self._write("failed")
        raise RuntimeError(f"failed to preload {image}: {record.get('status')}")

    def _native_solver_tag(self, image: str) -> str:
        fingerprint = self._native_solver_fingerprint()
        safe = re.sub(r"[^a-z0-9_.-]+", "-", image.lower()).strip("-")
        safe = safe[:90].strip("-") or "image"
        return f"multiagent-native-swe:{safe}-{fingerprint}"

    def _native_solver_fingerprint(self) -> str:
        if self.native_solver_source.is_file():
            stat = self.native_solver_source.stat()
            return f"{stat.st_mtime_ns:x}{stat.st_size:x}"[-16:]
        parts: list[str] = []
        for path in sorted(self.native_solver_source.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.native_solver_source)
            if self._skip_repo_bake_path(rel):
                continue
            stat = path.stat()
            parts.append(f"{rel}:{stat.st_mtime_ns:x}:{stat.st_size:x}")
        import hashlib

        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _skip_repo_bake_path(path: Path) -> bool:
        parts = set(path.parts)
        if parts & {".git", ".multiagent", "__pycache__", ".pytest_cache", "node_modules"}:
            return True
        if path.parts and path.parts[0] == "tests":
            return True
        if path.parts and path.parts[0] == "docs":
            return True
        if len(path.parts) == 1 and path.suffix == ".md" and path.name != "orchestrator_prompt.md":
            return True
        if path.parts and path.parts[0] == "evaluation":
            if path == Path("evaluation"):
                return False
            if len(path.parts) < 2 or path.parts[1] != "native_solver":
                return True
            allowed_native_solver = {
                Path("evaluation/native_solver"),
                Path("evaluation/native_solver/solve_swe_prod.py"),
                Path("evaluation/native_solver/swe_prod_guardrails.py"),
                Path("evaluation/native_solver/templates"),
                Path("evaluation/native_solver/templates/swe_autonomous_appendix.md"),
                Path("evaluation/native_solver/templates/swe_autonomous_final_override.md"),
            }
            if path not in allowed_native_solver and not (
                len(path.parts) >= 3 and Path(*path.parts[:3]) == Path("evaluation/native_solver/templates")
            ):
                return True
        if len(path.parts) >= 2 and path.parts[0] == "evaluation" and path.parts[1] in {"reports", "runs"}:
            return True
        if path.name.endswith((".pyc", ".pyo", ".log")):
            return True
        return False

    def _copy_native_solver_source(self, context_dir: Path) -> tuple[list[str], str]:
        if self.native_solver_source.is_file():
            shutil.copyfile(self.native_solver_source, context_dir / "solve_swe.py")
            package_hint = self.native_solver_source.name
            return ["COPY --chmod=755 solve_swe.py /opt/multiagent/solve_swe.py"], package_hint

        source_root = self.native_solver_source.resolve()
        if not (source_root / "launch.sh").exists():
            raise RuntimeError(
                f"native solver source directory must be a multiagent repo containing launch.sh: {source_root}"
            )
        dest = context_dir / "multiagent"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(
            source_root,
            dest,
            ignore=lambda directory, names: [
                name
                for name in names
                if self._skip_repo_bake_path((Path(directory).relative_to(source_root) / name) if Path(directory) != source_root else Path(name))
            ],
        )
        prod_solver = dest / "evaluation" / "native_solver" / "solve_swe_prod.py"
        if not prod_solver.exists():
            raise RuntimeError(f"production native solver missing from repo source: {prod_solver}")
        return (
            [
                "COPY multiagent/ /opt/multiagent/",
                "RUN chmod +x /opt/multiagent/launch.sh /opt/multiagent/bin/*.sh "
                "/opt/multiagent/evaluation/native_solver/solve_swe_prod.py && "
                "ln -sf /opt/multiagent/evaluation/native_solver/solve_swe_prod.py /opt/multiagent/solve_swe.py",
            ],
            "solve_swe_prod.py",
        )

    def _ensure_baked_image(self, image: str, instance_id: str) -> str:
        if not self.bake_native_solver:
            return image
        if not self.native_solver_source.exists():
            self.counts["bake_failed"] += 1
            self.records.append(
                {
                    "instance_id": instance_id,
                    "image": image,
                    "status": "bake_failed",
                    "error": f"native solver source missing: {self.native_solver_source}",
                }
            )
            self._write("failed")
            raise FileNotFoundError(f"native solver source missing: {self.native_solver_source}")

        baked_image = self._native_solver_tag(image)
        present, _ = docker_image_present(baked_image)
        if present:
            self.counts["bake_reused"] += 1
            self.records.append(
                {
                    "instance_id": instance_id,
                    "image": image,
                    "baked_image": baked_image,
                    "status": "bake_reused",
                }
            )
            self._write("running")
            return baked_image

        context_dir = self.archive_dir / "native-solver-build" / re.sub(r"[^A-Za-z0-9_.-]+", "_", baked_image)
        context_dir.mkdir(parents=True, exist_ok=True)
        copy_lines, package_hint = self._copy_native_solver_source(context_dir)
        dockerfile = context_dir / "Dockerfile"
        dockerfile_lines = [f"FROM {image}"]
        if "tmux" in package_hint or "prod" in package_hint:
            dockerfile_lines.append(
                "RUN if ! command -v tmux >/dev/null 2>&1; then "
                "(apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y tmux procps && rm -rf /var/lib/apt/lists/*) || "
                "(apk add --no-cache tmux procps) || "
                "(yum install -y tmux procps) || true; "
                "fi"
            )
        if "prod" in package_hint:
            node_download = (
                "download_node() { "
                "url=\"$1\"; out=\"$2\"; "
                "if command -v curl >/dev/null 2>&1; then curl -fsSL \"$url\" -o \"$out\"; "
                "elif command -v wget >/dev/null 2>&1; then wget -qO \"$out\" \"$url\"; "
                "else python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \"$url\" \"$out\"; "
                "fi; "
                "}; "
            )
            dockerfile_lines.append(
                "RUN (apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "ca-certificates curl xz-utils && rm -rf /var/lib/apt/lists/*) || "
                "(apk add --no-cache ca-certificates curl xz) || "
                "(yum install -y ca-certificates curl xz) || true"
            )
            dockerfile_lines.append(
                "RUN set -eux; "
                "export PATH=/opt/node22/bin:$PATH; "
                "node_major=\"$(node -p 'process.versions.node.split(\".\")[0]' 2>/dev/null || printf 0)\"; "
                "if [ \"${node_major}\" -lt 20 ] || ! command -v npm >/dev/null 2>&1; then "
                "if [ -f /etc/alpine-release ]; then "
                "apk add --no-cache nodejs-current npm || apk add --no-cache nodejs npm; "
                "node_major=\"$(node -p 'process.versions.node.split(\".\")[0]' 2>/dev/null || printf 0)\"; "
                "if [ \"${node_major}\" -lt 20 ]; then "
                "apk add --no-cache --upgrade "
                "--repository=https://dl-cdn.alpinelinux.org/alpine/v3.20/main "
                "--repository=https://dl-cdn.alpinelinux.org/alpine/v3.20/community "
                "nodejs npm libstdc++ libgcc || true; "
                "node_major=\"$(node -p 'process.versions.node.split(\".\")[0]' 2>/dev/null || printf 0)\"; "
                "fi; "
                "if [ \"${node_major}\" -lt 20 ]; then "
                "apk add --no-cache --upgrade "
                "--repository=https://dl-cdn.alpinelinux.org/alpine/v3.20/main "
                "libstdc++ libgcc || true; "
                f"{node_download}"
                "download_node https://unofficial-builds.nodejs.org/download/release/v22.12.0/node-v22.12.0-linux-x64-musl.tar.xz /tmp/node.tar.xz; "
                "mkdir -p /opt/node22; "
                "tar -xJf /tmp/node.tar.xz -C /opt/node22 --strip-components=1; "
                "rm -f /tmp/node.tar.xz; "
                "fi; "
                "else "
                f"{node_download}"
                "download_node https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz /tmp/node.tar.xz; "
                "mkdir -p /opt/node22; "
                "tar -xJf /tmp/node.tar.xz -C /opt/node22 --strip-components=1; "
                "rm -f /tmp/node.tar.xz; "
                "fi; "
                "fi"
            )
            dockerfile_lines.append(
                "RUN set -eux; "
                "export PATH=/opt/node22/bin:$PATH; "
                "if ! command -v npm >/dev/null 2>&1; then "
                "(apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "nodejs npm && rm -rf /var/lib/apt/lists/*) || "
                "(apk add --no-cache nodejs-current npm || apk add --no-cache nodejs npm) || "
                "(yum install -y nodejs npm) || true; "
                "fi; "
                "node_major=\"$(node -p 'process.versions.node.split(\".\")[0]' 2>/dev/null || printf 0)\"; "
                "if [ \"${node_major}\" -lt 20 ]; then "
                "if [ -f /etc/alpine-release ]; then "
                f"{node_download}"
                "download_node https://unofficial-builds.nodejs.org/download/release/v22.12.0/node-v22.12.0-linux-x64-musl.tar.xz /tmp/node.tar.xz; "
                "else "
                f"{node_download}"
                "download_node https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz /tmp/node.tar.xz; "
                "fi; "
                "mkdir -p /opt/node22; "
                "tar -xJf /tmp/node.tar.xz -C /opt/node22 --strip-components=1; "
                "rm -f /tmp/node.tar.xz; "
                "fi; "
                "command -v node; "
                "command -v npm; "
                "node -p 'process.versions.node'; "
                "test \"$(node -p 'process.versions.node.split(\".\")[0]')\" -ge 20"
            )
            dockerfile_lines.append(
                "RUN set -eux; "
                "rm -rf /opt/codex-node /opt/node22; "
                "if [ -f /etc/alpine-release ]; then "
                f"{node_download}"
                "download_node https://unofficial-builds.nodejs.org/download/release/v22.12.0/node-v22.12.0-linux-x64-musl.tar.xz /tmp/codex-node.tar.xz; "
                "else "
                f"{node_download}"
                "download_node https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz /tmp/codex-node.tar.xz; "
                "fi; "
                "mkdir -p /opt/codex-node; "
                "tar -xJf /tmp/codex-node.tar.xz -C /opt/codex-node --strip-components=1; "
                "rm -f /tmp/codex-node.tar.xz; "
                "ln -s /opt/codex-node /opt/node22; "
                "export PATH=/opt/codex-node/bin:$PATH; "
                "/opt/codex-node/bin/npm install -g --prefix /opt/codex-node --no-fund --no-audit @openai/codex; "
                "/opt/codex-node/bin/node --version; "
                "/opt/codex-node/bin/codex --version"
            )
            dockerfile_lines.append(
                "ENV GOCACHE=/var/cache/swebench-pro/go-build "
                "GOMODCACHE=/var/cache/swebench-pro/go-mod "
                "GOFLAGS=-p=2 "
                "GOMAXPROCS=2 "
                "CGO_CFLAGS=\"-D_GNU_SOURCE -D_LARGEFILE64_SOURCE\""
            )
            dockerfile_lines.append(
                "RUN if [ -f /app/go.mod ] && ! command -v go >/dev/null 2>&1; then "
                "(apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "golang-go && rm -rf /var/lib/apt/lists/*) || "
                "(apk add --no-cache go) || "
                "(yum install -y golang) || "
                "(curl -fsSL https://go.dev/dl/go1.23.4.linux-amd64.tar.gz -o /tmp/go.tar.gz && "
                "rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go.tar.gz && rm -f /tmp/go.tar.gz); "
                "fi; "
                "if [ -x /usr/local/go/bin/go ]; then ln -sf /usr/local/go/bin/go /usr/local/bin/go || true; fi; "
                "if [ -x /usr/local/go/bin/gofmt ]; then ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt || true; fi; "
                "if [ -f /app/go.mod ]; then command -v go || true; command -v gofmt || true; fi"
            )
            dockerfile_lines.append(
                "RUN if [ -f /app/go.mod ]; then "
                "(apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "build-essential pkg-config libc6-dev linux-libc-dev libsqlite3-dev && rm -rf /var/lib/apt/lists/*) || "
                "(apk add --no-cache build-base linux-headers pkgconf musl-dev libc6-compat gcompat sqlite-dev) || "
                "(yum install -y gcc gcc-c++ make pkgconfig sqlite-devel kernel-headers) || true; "
                "if [ -f /etc/alpine-release ] && [ ! -f /usr/include/gnu/libc-version.h ]; then "
                "mkdir -p /usr/include/gnu; "
                "printf '%s\\n' '#pragma once' 'static inline const char *gnu_get_libc_version(void) { return \"musl\"; }' "
                "> /usr/include/gnu/libc-version.h; "
                "fi; "
                "if [ -x /usr/local/go/bin/go ] && [ ! -x /usr/local/go/bin/go-real ]; then "
                "mv /usr/local/go/bin/go /usr/local/go/bin/go-real; "
                "printf '%s\\n' "
                "'#!/usr/bin/env bash' "
                "'set -euo pipefail' "
                "'real_go=/usr/local/go/bin/go-real' "
                "'args=()' "
                "'add_purego() {' "
                "'  case \",$1,\" in *,purego,*) printf \"%s\" \"$1\" ;; *) printf \"%s,purego\" \"$1\" ;; esac' "
                "'}' "
                "'while [ \"$#\" -gt 0 ]; do' "
                "'  case \"$1\" in' "
                "'    -tags)' "
                "'      args+=(\"$1\")' "
                "'      shift' "
                "'      if [ \"$#\" -gt 0 ]; then args+=(\"$(add_purego \"$1\")\"); else break; fi' "
                "'      ;;' "
                "'    -tags=*)' "
                "'      value=\"${1#-tags=}\"' "
                "'      args+=(\"-tags=$(add_purego \"$value\")\")' "
                "'      ;;' "
                "'    *) args+=(\"$1\") ;;' "
                "'  esac' "
                "'  shift' "
                "'done' "
                "'exec \"$real_go\" \"${args[@]}\"' "
                "> /usr/local/go/bin/go; "
                "chmod +x /usr/local/go/bin/go; "
                "fi; "
                "mkdir -p /var/cache/swebench-pro/go-build /var/cache/swebench-pro/go-mod; "
                "chmod -R 777 /var/cache/swebench-pro; "
                "fi"
            )
        dockerfile_lines.extend(copy_lines)
        dockerfile_lines.append("")
        dockerfile.write_text("\n".join(dockerfile_lines), encoding="utf-8")
        cmd = ["docker", "build", "--platform", self.platform, "-t", baked_image, str(context_dir)]
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=600, check=False)
        record = {
            "instance_id": instance_id,
            "image": image,
            "baked_image": baked_image,
            "status": "baked" if result.returncode == 0 else "bake_failed",
            "returncode": result.returncode,
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
        }
        self.records.append(record)
        if result.returncode == 0:
            self.counts["baked"] += 1
            self._write("running")
            return baked_image
        self.counts["bake_failed"] += 1
        self._write("failed")
        raise RuntimeError(f"failed to bake native solver into {image}: {result.stderr[-2000:]}")

    def prune_image(self, image: str, instance_id: str) -> None:
        if not self.prune_after_sample:
            return
        result = subprocess.run(
            ["docker", "image", "rm", image],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        forced = False
        if result.returncode != 0 and "must be forced" in (result.stderr or ""):
            forced = True
            result = subprocess.run(
                ["docker", "image", "rm", "--force", image],
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
        record = {
            "instance_id": instance_id,
            "image": image,
            "status": "pruned" if result.returncode == 0 else "prune_failed",
            "returncode": result.returncode,
            "forced": forced,
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
        }
        self.records.append(record)
        if result.returncode == 0:
            self.counts["pruned"] += 1
        else:
            self.counts["prune_failed"] += 1
        self._write("running")

    def finalize(self, status: str) -> None:
        self._write(status)


def install_on_demand_image_hooks(manager: OnDemandImageManager) -> None:
    """Patch EvalScope's SWE Bench Pro adapter class for this Python process."""
    from evalscope.benchmarks.swe_bench_pro.swe_bench_pro_agentic_adapter import SWEBenchProAgenticAdapter

    SWEBenchProAgenticAdapter._codex_on_demand_image_manager = manager
    if getattr(SWEBenchProAgenticAdapter, "_codex_on_demand_image_hooks", False):
        return

    original_build_environment = SWEBenchProAgenticAdapter.build_environment
    original_match_score = SWEBenchProAgenticAdapter.match_score

    def build_environment(self, sample):  # type: ignore[no-untyped-def]
        active_manager = self.__class__._codex_on_demand_image_manager
        image = sample.metadata.get("docker_image")
        instance_id = sample.metadata.get("instance_id", "")
        if image:
            sample.metadata["docker_image"] = active_manager.ensure_image(str(image), str(instance_id))
        return original_build_environment(self, sample)

    def match_score(self, original_prediction, filtered_prediction, reference, task_state):  # type: ignore[no-untyped-def]
        active_manager = self.__class__._codex_on_demand_image_manager
        image = task_state.metadata.get("docker_image")
        instance_id = task_state.metadata.get("instance_id", "")
        try:
            return original_match_score(self, original_prediction, filtered_prediction, reference, task_state)
        finally:
            if image:
                active_manager.prune_image(str(image), str(instance_id))

    SWEBenchProAgenticAdapter.build_environment = build_environment
    SWEBenchProAgenticAdapter.match_score = match_score
    SWEBenchProAgenticAdapter._codex_on_demand_image_hooks = True
