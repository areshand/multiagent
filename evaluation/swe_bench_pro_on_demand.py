"""On-demand SWE Bench Pro image loading hooks for EvalScope.

EvalScope's SWE Bench Pro adapter asks ms-enclave to create a Docker sandbox
from the per-instance ``jefzda/sweap-images`` tag. In this environment direct
Docker pulls are unreliable and the full 731-image set is too large to keep
resident, so these hooks ensure the required image exists immediately before a
sample starts and optionally remove it after scoring.
"""

from __future__ import annotations

import hashlib
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


SOLVER_SOURCE_LABEL = "org.multiagent.solver-source-sha256"


def inspect_image_identity(image: str) -> dict[str, Any]:
    """Return content-addressed local identity for a runnable Docker image."""

    result = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{json .}}"],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker image identity inspection failed for {image}: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    image_id = str(payload.get("Id") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError(f"docker image identity is missing for {image}: {image_id!r}")
    return {
        "reference": image,
        "image_id": image_id,
        "repo_digests": list(payload.get("RepoDigests") or []),
        "os": str(payload.get("Os") or ""),
        "architecture": str(payload.get("Architecture") or ""),
        "labels": dict(((payload.get("Config") or {}).get("Labels") or {})),
    }


def skip_repo_bake_path(path: Path) -> bool:
    """Return whether a repository path is excluded from task-image source."""

    parts = set(path.parts)
    if parts & {".git", ".multiagent", "__pycache__", ".pytest_cache", "node_modules"}:
        return True
    if path.parts and path.parts[0] in {"tests", "docs"}:
        return True
    if len(path.parts) == 1 and path.suffix == ".md" and path.name != "orchestrator_prompt.md":
        return True
    if path.parts and path.parts[0] == "evaluation":
        if path in {Path("evaluation"), Path("evaluation/__init__.py")}:
            return False
        native_solver_root = Path("evaluation/native_solver")
        support_root = Path("evaluation/support")
        is_solver_module = path.parent == native_solver_root and path.suffix == ".py"
        is_solver_template = len(path.parts) >= 3 and Path(*path.parts[:3]) == native_solver_root / "templates"
        is_support_module = (
            len(path.parts) >= 3
            and path.parts[:2] == ("evaluation", "support")
            and path.suffix == ".py"
        )
        if path not in {native_solver_root, native_solver_root / "templates"} and not (
            is_solver_module
            or is_solver_template
            or path in {support_root, support_root / "coding"}
            or is_support_module
        ):
            return True
    if len(path.parts) >= 2 and path.parts[0] == "evaluation" and path.parts[1] in {"reports", "runs"}:
        return True
    return path.name.endswith((".pyc", ".pyo", ".log"))


def native_solver_source_digest(source_root: Path) -> str:
    """Hash the exact source bytes copied into production task images."""

    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source_root)
        if skip_repo_bake_path(relative):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(format(path.stat().st_mode & 0o7777, "o").encode("ascii"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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
                    "bake_native_solver": True,
                    "native_solver_source": str(self.native_solver_source),
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

    def _native_solver_tag(self, image: str, fingerprint: str | None = None) -> str:
        fingerprint = fingerprint or self._native_solver_fingerprint()
        safe = re.sub(r"[^a-z0-9_.-]+", "-", image.lower()).strip("-")
        safe = safe[:90].strip("-") or "image"
        return f"multiagent-native-swe:{safe}-{fingerprint}"

    def _native_solver_fingerprint(self) -> str:
        return native_solver_source_digest(self.native_solver_source)[:16]

    @staticmethod
    def _rust_builder_lines() -> list[str]:
        return [
            "FROM rust:1.85-alpine AS multiagent-builder",
            "RUN apk add --no-cache musl-dev",
            "WORKDIR /build",
            "COPY multiagent/Cargo.toml multiagent/Cargo.lock ./",
            "COPY multiagent/src ./src",
            "RUN cargo build --release --locked",
        ]

    @staticmethod
    def _skip_repo_bake_path(path: Path) -> bool:
        return skip_repo_bake_path(path)

    def _copy_native_solver_source(self, context_dir: Path) -> tuple[list[str], str]:
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
        entrypoint = dest / "evaluation" / "native_solver" / "solve_swe_prod.py"
        package_init = dest / "evaluation" / "native_solver" / "__init__.py"
        evaluation_init = dest / "evaluation" / "__init__.py"
        missing = [path for path in (entrypoint, package_init, evaluation_init) if not path.exists()]
        if missing:
            raise RuntimeError(
                "production native solver package is incomplete: "
                + ", ".join(str(path) for path in missing)
            )
        return (
            [
                "COPY multiagent/ /opt/multiagent/",
                "COPY --from=multiagent-builder /build/target/release/multiagent /opt/multiagent/bin/multiagent",
                "RUN chmod +x /opt/multiagent/launch.sh /opt/multiagent/bin/multiagent",
            ],
            "python3 -m evaluation.native_solver.solve_swe_prod",
        )

    def _ensure_baked_image(self, image: str, instance_id: str) -> str:
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

        solver_digest = native_solver_source_digest(self.native_solver_source)
        baked_image = self._native_solver_tag(image, solver_digest[:16])
        present, _ = docker_image_present(baked_image)
        if present:
            record = {
                "instance_id": instance_id,
                "image": image,
                "baked_image": baked_image,
                "status": "bake_reused",
            }
            try:
                record["base_identity"] = inspect_image_identity(image)
                record["baked_identity"] = inspect_image_identity(baked_image)
                if record["baked_identity"]["labels"].get(SOLVER_SOURCE_LABEL) != solver_digest:
                    raise RuntimeError(f"reused image is not bound to current solver source: {baked_image}")
                record["solver_source_sha256"] = solver_digest
            except Exception as exc:
                record["status"] = "image_identity_failed"
                record["identity_error"] = repr(exc)
                self.records.append(record)
                self.counts["bake_failed"] += 1
                self._write("failed")
                raise
            self.counts["bake_reused"] += 1
            self.records.append(record)
            self._write("running")
            return baked_image

        context_dir = self.archive_dir / "native-solver-build" / re.sub(r"[^A-Za-z0-9_.-]+", "_", baked_image)
        context_dir.mkdir(parents=True, exist_ok=True)
        copy_lines, package_hint = self._copy_native_solver_source(context_dir)
        dockerfile = context_dir / "Dockerfile"
        dockerfile_lines = [
            *self._rust_builder_lines(),
            f"FROM {image}",
            f'LABEL {SOLVER_SOURCE_LABEL}="{solver_digest}"',
        ]
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
            alpine_node_url = (
                "https://unofficial-builds.nodejs.org/download/release/v20.19.0/"
                "node-v20.19.0-linux-x64-musl.tar.xz"
            )
            linux_node_url = "https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz"
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
                f"{node_download}"
                f"download_node {alpine_node_url} /tmp/node.tar.xz; "
                "mkdir -p /opt/node22; "
                "tar -xJf /tmp/node.tar.xz -C /opt/node22 --strip-components=1; "
                "rm -f /tmp/node.tar.xz; "
                "fi; "
                "else "
                f"{node_download}"
                f"download_node {linux_node_url} /tmp/node.tar.xz; "
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
                f"download_node {alpine_node_url} /tmp/node.tar.xz; "
                "else "
                f"{node_download}"
                f"download_node {linux_node_url} /tmp/node.tar.xz; "
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
                f"download_node {alpine_node_url} /tmp/codex-node.tar.xz; "
                "else "
                f"{node_download}"
                f"download_node {linux_node_url} /tmp/codex-node.tar.xz; "
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
        if result.returncode == 0:
            try:
                record["base_identity"] = inspect_image_identity(image)
                record["baked_identity"] = inspect_image_identity(baked_image)
                if record["baked_identity"]["labels"].get(SOLVER_SOURCE_LABEL) != solver_digest:
                    raise RuntimeError(f"built image is not bound to current solver source: {baked_image}")
                record["solver_source_sha256"] = solver_digest
            except Exception as exc:
                record["status"] = "image_identity_failed"
                record["identity_error"] = repr(exc)
                self.records.append(record)
                self.counts["bake_failed"] += 1
                self._write("failed")
                raise
            self.records.append(record)
            self.counts["baked"] += 1
            self._write("running")
            return baked_image
        self.records.append(record)
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
