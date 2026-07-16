#!/usr/bin/env python3
"""Build a docker-loadable OCI archive from a registry image.

This is useful in environments where `docker pull` or `docker build` stalls
while resolving metadata, but direct registry HTTP blob downloads still work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ACCEPT_MANIFESTS = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    reference: str

    @property
    def registry_url(self) -> str:
        return f"https://{self.registry}"

    @property
    def repo_tag(self) -> str:
        if self.registry == "registry-1.docker.io":
            repository = self.repository.removeprefix("library/")
            return f"{repository}:{self.reference}"
        return f"{self.registry}/{self.repository}:{self.reference}"


def parse_image_ref(value: str) -> ImageRef:
    image, sep, reference = value.rpartition(":")
    if "/" in reference or not sep:
        image = value
        reference = "latest"

    parts = image.split("/")
    if "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        registry = parts[0]
        repository = "/".join(parts[1:])
    else:
        registry = "registry-1.docker.io"
        repository = image
    if registry == "docker.io":
        registry = "registry-1.docker.io"
    if registry == "registry-1.docker.io" and "/" not in repository:
        repository = f"library/{repository}"
    if not repository:
        raise ValueError(f"invalid image reference: {value!r}")
    return ImageRef(registry=registry, repository=repository, reference=reference)


def request_json(url: str, headers: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=120) as response:
        payload = json.loads(response.read().decode("utf-8"))
        return payload, {key.lower(): value for key, value in response.headers.items()}


def parse_www_authenticate(value: str) -> dict[str, str]:
    scheme, _, rest = value.partition(" ")
    if scheme.lower() != "bearer":
        raise ValueError(f"unsupported auth challenge: {scheme}")
    parsed: dict[str, str] = {}
    for part in rest.split(","):
        key, _, raw = part.strip().partition("=")
        parsed[key] = raw.strip('"')
    return parsed


def get_bearer_token(ref: ImageRef, scope: str) -> str:
    challenge_url = f"{ref.registry_url}/v2/"
    try:
        request_json(challenge_url)
    except urllib.error.HTTPError as exc:
        challenge = exc.headers.get("WWW-Authenticate")
        if exc.code != 401 or not challenge:
            raise
    else:
        raise RuntimeError("registry did not require bearer auth")

    params = parse_www_authenticate(challenge)
    realm = params["realm"]
    query = {
        "service": params.get("service", ref.registry),
        "scope": scope,
    }
    token_url = f"{realm}?{urllib.parse.urlencode(query)}"
    payload, _ = request_json(token_url)
    token = payload.get("token") or payload.get("access_token")
    if not token:
        raise RuntimeError("auth server did not return token")
    return str(token)


def registry_headers(token: str, accept: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if accept:
        headers["Accept"] = accept
    return headers


def fetch_manifest(ref: ImageRef, token: str, reference: str) -> tuple[dict[str, Any], str]:
    url = f"{ref.registry_url}/v2/{ref.repository}/manifests/{reference}"
    req = urllib.request.Request(url, headers=registry_headers(token, ACCEPT_MANIFESTS))
    with urllib.request.urlopen(req, timeout=120) as response:
        payload = json.loads(response.read().decode("utf-8"))
        digest = response.headers.get("Docker-Content-Digest", reference)
        return payload, digest


def select_manifest(index: dict[str, Any], os_name: str, arch: str) -> dict[str, Any]:
    manifests = index.get("manifests") or []
    for descriptor in manifests:
        platform = descriptor.get("platform") or {}
        if platform.get("os") == os_name and platform.get("architecture") == arch:
            return descriptor
    available = [
        f"{(item.get('platform') or {}).get('os')}/{(item.get('platform') or {}).get('architecture')}"
        for item in manifests
    ]
    raise RuntimeError(f"no manifest for {os_name}/{arch}; available: {', '.join(available)}")


def download_blob(ref: ImageRef, token: str, digest: str, output: Path) -> int:
    url = f"{ref.registry_url}/v2/{ref.repository}/blobs/{digest}"
    req = urllib.request.Request(url, headers=registry_headers(token))
    with urllib.request.urlopen(req, timeout=300) as response, output.open("wb") as fh:
        shutil.copyfileobj(response, fh)
    return output.stat().st_size


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, separators=(",", ":")).encode("utf-8")


def digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def normalize_manifest_for_oci(manifest: dict[str, Any]) -> dict[str, Any]:
    """Convert Docker distribution media types to OCI archive media types."""
    normalized = json.loads(json.dumps(manifest))
    normalized["mediaType"] = "application/vnd.oci.image.manifest.v1+json"
    if "config" in normalized:
        normalized["config"]["mediaType"] = "application/vnd.oci.image.config.v1+json"
    for layer in normalized.get("layers", []):
        media_type = str(layer.get("mediaType") or "")
        if media_type.endswith(".tar.gzip") or media_type.endswith(".tar+gzip"):
            layer["mediaType"] = "application/vnd.oci.image.layer.v1.tar+gzip"
        elif media_type.endswith(".tar"):
            layer["mediaType"] = "application/vnd.oci.image.layer.v1.tar"
    return normalized


def write_oci_archive(
    ref: ImageRef,
    manifest: dict[str, Any],
    manifest_digest: str,
    blobs: dict[str, int],
    workdir: Path,
    archive: Path,
) -> None:
    (workdir / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "1.0.0"}), encoding="utf-8")
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": manifest.get("mediaType", "application/vnd.oci.image.manifest.v1+json"),
                "digest": manifest_digest,
                "size": len(manifest_bytes(manifest)),
                "annotations": {"org.opencontainers.image.ref.name": ref.repo_tag},
            }
        ],
    }
    (workdir / "index.json").write_text(json.dumps(index), encoding="utf-8")
    with tarfile.open(archive, "w") as tar:
        tar.add(workdir / "oci-layout", arcname="oci-layout")
        tar.add(workdir / "index.json", arcname="index.json")
        for digest in blobs:
            algo, value = digest.split(":", 1)
            tar.add(workdir / "blobs" / algo / value, arcname=f"blobs/{algo}/{value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="image reference, for example python:3.11-slim")
    parser.add_argument("--platform", default="linux/arm64", help="platform to select from an index")
    parser.add_argument("--archive", required=True, help="output OCI tar path")
    parser.add_argument("--metadata", required=True, help="output metadata JSON path")
    args = parser.parse_args()

    os_name, arch = args.platform.split("/", 1)
    ref = parse_image_ref(args.image)
    token = get_bearer_token(ref, f"repository:{ref.repository}:pull")
    root_manifest, root_digest = fetch_manifest(ref, token, ref.reference)
    media_type = root_manifest.get("mediaType", "")
    if media_type.endswith("image.index.v1+json") or media_type.endswith("manifest.list.v2+json"):
        descriptor = select_manifest(root_manifest, os_name, arch)
        manifest, manifest_digest = fetch_manifest(ref, token, descriptor["digest"])
    else:
        manifest = root_manifest
        manifest_digest = root_digest

    archive = Path(args.archive)
    metadata = Path(args.metadata)
    archive.parent.mkdir(parents=True, exist_ok=True)
    metadata.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="registry-oci-") as tmp:
        workdir = Path(tmp)
        blob_root = workdir / "blobs" / "sha256"
        blob_root.mkdir(parents=True)
        registry_manifest_digest = manifest_digest
        manifest = normalize_manifest_for_oci(manifest)
        manifest_json = manifest_bytes(manifest)
        manifest_digest = digest_bytes(manifest_json)
        manifest_algo, manifest_hash = manifest_digest.split(":", 1)
        if manifest_algo != "sha256":
            raise RuntimeError(f"unsupported manifest digest algorithm: {manifest_algo}")
        (blob_root / manifest_hash).write_bytes(manifest_json)

        blob_sizes = {manifest_digest: len(manifest_json)}
        descriptors = [manifest["config"], *manifest.get("layers", [])]
        for descriptor in descriptors:
            digest = descriptor["digest"]
            algo, value = digest.split(":", 1)
            if algo != "sha256":
                raise RuntimeError(f"unsupported blob digest algorithm: {algo}")
            blob_sizes[digest] = download_blob(ref, token, digest, blob_root / value)

        write_oci_archive(ref, manifest, manifest_digest, blob_sizes, workdir, archive)

    metadata.write_text(
        json.dumps(
            {
                "image": args.image,
                "repo_tag": ref.repo_tag,
                "platform": args.platform,
                "manifest_digest": manifest_digest,
                "registry_manifest_digest": registry_manifest_digest,
                "archive": str(archive),
                "blobs": blob_sizes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {archive}")
    print(f"wrote {metadata}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
