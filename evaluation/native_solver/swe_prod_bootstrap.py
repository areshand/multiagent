from __future__ import annotations

import os
import shutil
from pathlib import Path

from .swe_prod_contracts import (
    APPLY_PATCH_WRAPPER,
    CODEX_HOME,
    CODEX_WRAPPER,
    RUNTIME_ROOT,
    STABLE_APPLY_PATCH,
    log,
)

def require_path(path: Path, description: str) -> None:
    if not path.exists():
        raise RuntimeError(f"missing {description}: {path}")


def write_codex_bridge(real_codex: str, model: str, auth_mode: str) -> None:
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    node_bin = str(Path(real_codex).parent / "node")
    codex_exec = (
        f"exec {node_bin!r} {real_codex!r} \\"
        if Path(node_bin).exists() and os.access(node_bin, os.X_OK)
        else f"exec {real_codex!r} \\"
    )
    (CODEX_HOME / "config.toml").write_text(
        """[projects."/app"]
trust_level = "trusted"

[projects."/opt/multiagent"]
trust_level = "trusted"
""",
        encoding="utf-8",
    )
    if auth_mode == "chatgpt":
        CODEX_WRAPPER.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
export CODEX_HOME={str(CODEX_HOME)!r}
{codex_exec}
  -c 'model_provider="openai"' \\
  -c 'model="{model}"' \\
  "$@"
""",
            encoding="utf-8",
        )
        CODEX_WRAPPER.chmod(0o755)
        return

    CODEX_WRAPPER.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
export CODEX_HOME={str(CODEX_HOME)!r}
{codex_exec}
  -c 'model_provider="evalscope"' \\
  -c 'model_providers.evalscope.name="EvalScope Bridge"' \\
  -c "model_providers.evalscope.base_url=\\"${{OPENAI_BASE_URL}}\\"" \\
  -c 'model_providers.evalscope.env_key="OPENAI_API_KEY"' \\
  -c 'model_providers.evalscope.wire_api="responses"' \\
  -c 'model="{model}"' \\
  "$@"
""",
        encoding="utf-8",
    )
    CODEX_WRAPPER.chmod(0o755)


def write_apply_patch_helper() -> None:
    APPLY_PATCH_WRAPPER.parent.mkdir(parents=True, exist_ok=True)
    APPLY_PATCH_WRAPPER.write_text(
        r'''#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def die(message: str) -> None:
    print(f"apply_patch: {message}", file=sys.stderr)
    raise SystemExit(1)


def strip_prefix(line: str) -> str:
    if not line:
        die("malformed empty patch line")
    return line[1:]


def remove_prefix(value: str, prefix: str) -> str:
    if not value.startswith(prefix):
        die(f"expected prefix {prefix!r}: {value!r}")
    return value[len(prefix):]


def find_sequence(lines: list[str], needle: list[str], start: int) -> int:
    if not needle:
        return start
    limit = len(lines) - len(needle) + 1
    for idx in range(max(0, start), max(0, limit)):
        if lines[idx : idx + len(needle)] == needle:
            return idx
    for idx in range(0, max(0, limit)):
        if lines[idx : idx + len(needle)] == needle:
            return idx
    return -1


def apply_update(path: Path, hunks: list[list[str]]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    cursor = 0
    for hunk in hunks:
        old: list[str] = []
        new: list[str] = []
        for line in hunk:
            if line.startswith(" "):
                old.append(strip_prefix(line))
                new.append(strip_prefix(line))
            elif line.startswith("-"):
                old.append(strip_prefix(line))
            elif line.startswith("+"):
                new.append(strip_prefix(line))
            elif line.startswith("\\"):
                continue
            else:
                die(f"unsupported hunk line in {path}: {line!r}")
        idx = find_sequence(lines, old, cursor)
        if idx < 0:
            die(f"could not find hunk context in {path}")
        lines[idx : idx + len(old)] = new
        cursor = idx + len(new)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def main() -> int:
    text = sys.stdin.read().splitlines()
    if not text or text[0] != "*** Begin Patch":
        die("expected *** Begin Patch")
    idx = 1
    changed: list[Path] = []
    while idx < len(text):
        line = text[idx]
        if line == "*** End Patch":
            break
        if line.startswith("*** Update File: "):
            path = Path(remove_prefix(line, "*** Update File: "))
            idx += 1
            hunks: list[list[str]] = []
            current: list[str] | None = None
            while idx < len(text) and not text[idx].startswith("*** "):
                if text[idx].startswith("@@"):
                    if current is not None:
                        hunks.append(current)
                    current = []
                else:
                    if current is None:
                        die(f"expected hunk header for {path}")
                    current.append(text[idx])
                idx += 1
            if current is not None:
                hunks.append(current)
            apply_update(path, hunks)
            changed.append(path)
            continue
        if line.startswith("*** Add File: "):
            path = Path(remove_prefix(line, "*** Add File: "))
            idx += 1
            new_lines: list[str] = []
            while idx < len(text) and not text[idx].startswith("*** "):
                if not text[idx].startswith("+"):
                    die(f"expected add line for {path}")
                new_lines.append(strip_prefix(text[idx]))
                idx += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
            changed.append(path)
            continue
        if line.startswith("*** Delete File: "):
            path = Path(remove_prefix(line, "*** Delete File: "))
            path.unlink()
            changed.append(path)
            idx += 1
            continue
        die(f"unsupported patch directive: {line!r}")
    if idx >= len(text) or text[idx] != "*** End Patch":
        die("missing *** End Patch")
    for path in changed:
        print(f"patched {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
        encoding="utf-8",
    )
    APPLY_PATCH_WRAPPER.chmod(0o755)
    try:
        if not STABLE_APPLY_PATCH.exists():
            shutil.copy2(APPLY_PATCH_WRAPPER, STABLE_APPLY_PATCH)
            STABLE_APPLY_PATCH.chmod(0o755)
    except OSError as exc:
        log(f"could not install stable apply_patch helper at {STABLE_APPLY_PATCH}: {exc}")


def write_rg_fallback() -> None:
    if shutil.which("rg"):
        return
    rg_path = RUNTIME_ROOT / "rg"
    rg_path.write_text(
        r'''#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


IGNORED_DIRS = {".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build", "coverage", "__pycache__"}


def iter_files(paths: list[str]) -> list[Path]:
    roots = [Path(path) for path in (paths or ["."])]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        if not root.exists():
            continue
        for current, dirs, names in os.walk(root):
            dirs[:] = [name for name in dirs if name not in IGNORED_DIRS]
            for name in names:
                path = Path(current) / name
                if path.is_file():
                    files.append(path)
    return files


def parse_args(argv: list[str]) -> tuple[dict[str, bool], str | None, list[str]]:
    flags = {"files": False, "ignore_case": False, "files_with_matches": False}
    pattern: str | None = None
    paths: list[str] = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg == "--":
            if flags["files"]:
                paths.extend(argv[idx + 1 :])
            elif idx + 1 < len(argv) and pattern is None:
                pattern = argv[idx + 1]
                paths.extend(argv[idx + 2 :])
            else:
                paths.extend(argv[idx + 1 :])
            break
        if arg == "--files":
            flags["files"] = True
            idx += 1
            continue
        if arg in {"-i", "--ignore-case"}:
            flags["ignore_case"] = True
            idx += 1
            continue
        if arg in {"-l", "--files-with-matches"}:
            flags["files_with_matches"] = True
            idx += 1
            continue
        if arg in {"-n", "-S", "--no-heading", "--hidden", "--follow", "--color=never"}:
            idx += 1
            continue
        if arg in {"-g", "--glob", "--type", "-t", "--type-not", "-T"}:
            idx += 2
            continue
        if arg.startswith("-"):
            idx += 1
            continue
        if flags["files"]:
            paths.append(arg)
            idx += 1
            continue
        if pattern is None:
            pattern = arg
        else:
            paths.append(arg)
        idx += 1
    return flags, pattern, paths


def is_binary(path: Path) -> bool:
    try:
        return b"\0" in path.read_bytes()[:4096]
    except OSError:
        return True


def main() -> int:
    flags, pattern, paths = parse_args(sys.argv[1:])
    if flags["files"]:
        for path in iter_files(paths):
            print(path)
        return 0
    if pattern is None:
        print("rg fallback: missing pattern", file=sys.stderr)
        return 2
    try:
        regex = re.compile(pattern, re.IGNORECASE if flags["ignore_case"] else 0)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE if flags["ignore_case"] else 0)
    matched = False
    for path in iter_files(paths):
        if is_binary(path):
            continue
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        file_matched = False
        for line_no, line in enumerate(lines, 1):
            if not regex.search(line):
                continue
            matched = True
            file_matched = True
            if not flags["files_with_matches"]:
                print(f"{path}:{line_no}:{line}")
        if flags["files_with_matches"] and file_matched:
            print(path)
    return 0 if matched else 1


if __name__ == "__main__":
    raise SystemExit(main())
''',
        encoding="utf-8",
    )
    rg_path.chmod(0o755)
    log(f"installed rg fallback at {rg_path}")
