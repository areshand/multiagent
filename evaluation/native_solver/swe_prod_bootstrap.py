from __future__ import annotations

try:
    from .swe_prod_contracts import *  # noqa: F403
except ImportError:  # pragma: no cover - direct execution in task containers
    from swe_prod_contracts import *  # type: ignore  # noqa: F403

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


def find_go_binary() -> str | None:
    for candidate in (
        Path("/usr/local/go/bin/go-real"),
        Path("/usr/local/go/bin/go"),
        Path("/usr/bin/go-real"),
        Path("/usr/bin/go"),
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which("go")
    return found


def write_go_singleflight_wrapper(real_go: str | None = None) -> None:
    real_go = real_go or find_go_binary()
    if not real_go:
        return
    system_go_path: Path | None = None
    real_go_path = Path(real_go)
    if real_go_path.name == "go" and real_go_path.exists() and os.access(real_go_path.parent, os.W_OK):
        go_real_path = real_go_path.with_name("go-real")
        if not go_real_path.exists():
            real_go_path.rename(go_real_path)
        real_go = str(go_real_path)
        system_go_path = real_go_path
    elif real_go_path.name == "go-real" and os.access(real_go_path.parent, os.W_OK):
        system_go_path = real_go_path.with_name("go")

    go_path = RUNTIME_ROOT / "go"
    wrapper_text = f'''#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


REAL_GO = {real_go!r}
LOCK_ROOT = Path(os.environ.get("MULTIAGENT_GO_TEST_LOCK_ROOT", "/tmp/multiagent-prod-swe/go-test-locks"))
WAIT_TIMEOUT = int(os.environ.get("MULTIAGENT_GO_TEST_WAIT_TIMEOUT", "3600"))
RUN_TIMEOUT = int(os.environ.get("MULTIAGENT_GO_TEST_TIMEOUT_SECONDS", os.environ.get("MULTIAGENT_VALIDATION_TIMEOUT_SECONDS", "600")))


def repo_diff_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-color"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return "nogit"
    if result.returncode != 0:
        return "nogit"
    return hashlib.sha256(result.stdout.encode()).hexdigest()


def canonical_argv(argv: list[str]) -> list[str]:
    if not argv or argv[0] != "test":
        return argv
    packages: list[str] = []
    others: list[str] = []
    for item in argv[1:]:
        if item.startswith("./"):
            packages.append(item)
        else:
            others.append(item)
    if len(packages) <= 1:
        return argv
    return [argv[0], *others, *sorted(packages)]


def key_for(argv: list[str]) -> str:
    payload = {{
        "cwd": str(Path.cwd()),
        "argv": canonical_argv(argv),
    }}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def result_key_for(argv: list[str]) -> str:
    payload = {{
        "cwd": str(Path.cwd()),
        "argv": canonical_argv(argv),
        "diff": repo_diff_hash(),
    }}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def replay(lock_dir: Path) -> int:
    stdout = lock_dir / "stdout.log"
    stderr = lock_dir / "stderr.log"
    rc_file = lock_dir / "returncode"
    if stdout.exists():
        sys.stdout.write(stdout.read_text(errors="replace"))
    if stderr.exists():
        sys.stderr.write(stderr.read_text(errors="replace"))
    try:
        return int(rc_file.read_text().strip())
    except Exception:
        return 1


def kill_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        proc.wait()


def run_owner(lock_dir: Path, argv: list[str]) -> int:
    (lock_dir / "pid").write_text(f"{{os.getpid()}}\\n")
    (lock_dir / "command.json").write_text(json.dumps(argv, indent=2) + "\\n")
    (lock_dir / "status").write_text("running\\n")
    started = time.time()
    start_diff_hash = repo_diff_hash()
    (lock_dir / "start_diff_hash").write_text(f"{{start_diff_hash}}\\n")
    with (lock_dir / "stdout.log").open("w") as stdout, (lock_dir / "stderr.log").open("w") as stderr:
        proc = subprocess.Popen(
            [REAL_GO, *argv],
            text=True,
            stdout=stdout,
            stderr=stderr,
            preexec_fn=child_preexec,
        )
        (lock_dir / "child_pid").write_text(f"{{proc.pid}}\\n")

        def forward_signal(signum, _frame):
            kill_process_group(proc)
            raise SystemExit(128 + signum)

        previous_handlers = {{}}
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, forward_signal)
        try:
            timed_out = False
            try:
                returncode = proc.wait(timeout=RUN_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_process_group(proc)
                returncode = 124
                stderr.write(f"\\ngo singleflight: go test timed out after {{RUN_TIMEOUT}} seconds\\n")
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        finish_diff_hash = repo_diff_hash()
        stale_diff = finish_diff_hash != start_diff_hash
        if stale_diff:
            stderr.write(
                "\\ngo singleflight: validation diff changed while command was running; "
                f"start_diff_hash={{start_diff_hash}} finish_diff_hash={{finish_diff_hash}}\\n"
            )
            # No command result can validate a diff other than the one it
            # started against. Preserve timeout/failure details in the logs,
            # but return the dedicated stale-evidence code so callers retry
            # against the final diff instead of treating this as a patch miss.
            returncode = 125
    (lock_dir / "returncode").write_text(f"{{returncode}}\\n")
    (lock_dir / "finish_diff_hash").write_text(f"{{finish_diff_hash}}\\n")
    (lock_dir / "finished.json").write_text(json.dumps({{"started": started, "finished": time.time(), "returncode": returncode, "timeout_seconds": RUN_TIMEOUT, "timed_out": timed_out, "start_diff_hash": start_diff_hash, "finish_diff_hash": finish_diff_hash, "stale_diff": stale_diff}}, sort_keys=True) + "\\n")
    (lock_dir / "status").write_text(("timed-out" if timed_out else "stale-diff" if stale_diff else "done") + "\\n")
    return replay(lock_dir)


def child_preexec() -> None:
    if sys.platform.startswith("linux"):
        try:
            os.setsid()
        except Exception:
            pass
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6")
            PR_SET_PDEATHSIG = 1
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        except Exception:
            pass


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] != "test":
        os.execv(REAL_GO, [REAL_GO, *argv])
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    results_root = LOCK_ROOT / "results"
    results_root.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_ROOT / f"{{key_for(argv)}}.lock"
    with lock_path.open("a+") as lock_file:
        wait_started = time.time()
        announced_wait = False
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not announced_wait:
                    sys.stderr.write(f"go singleflight: waiting for duplicate validation {{lock_path.stem}}\\n")
                    announced_wait = True
                if time.time() - wait_started >= WAIT_TIMEOUT:
                    sys.stderr.write(f"go singleflight: duplicate validation wait timed out after {{WAIT_TIMEOUT}} seconds\\n")
                    return 124
                time.sleep(1)
        lock_dir = results_root / result_key_for(argv)
        status = lock_dir / "status"
        if status.exists() and status.read_text(errors="replace").strip() in {{"done", "timed-out", "stale-diff"}}:
            sys.stderr.write(f"go singleflight: replaying completed validation {{lock_dir.name}}\\n")
            return replay(lock_dir)
        lock_dir.mkdir(parents=True, exist_ok=True)
        return run_owner(lock_dir, argv)


if __name__ == "__main__":
    raise SystemExit(main())
'''
    go_path.write_text(wrapper_text, encoding="utf-8")
    go_path.chmod(0o755)
    if system_go_path is not None:
        system_go_path.write_text(wrapper_text, encoding="utf-8")
        system_go_path.chmod(0o755)
        log(f"installed go test singleflight wrapper at {system_go_path} -> {real_go}")
    log(f"installed go test singleflight wrapper at {go_path} -> {real_go}")
