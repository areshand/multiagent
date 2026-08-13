#!/usr/bin/env python3
"""Regression tests for the container-native solver package boundary."""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NATIVE_SOLVER = ROOT / "evaluation" / "native_solver"
MODULE_ENTRYPOINT = "evaluation.native_solver.solve_swe_prod"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def assigned_string(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise AssertionError(f"{name} string assignment not found in {path}")


class NativeSolverImportModelTest(unittest.TestCase):
    def test_package_import_and_module_entrypoint(self) -> None:
        from evaluation.native_solver import solve_swe_prod

        self.assertEqual(solve_swe_prod.__name__, MODULE_ENTRYPOINT)
        result = subprocess.run(
            [sys.executable, "-m", MODULE_ENTRYPOINT, "--help"],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--multiagent-root", result.stdout)

    def test_entrypoint_preserves_legacy_export_enumeration(self) -> None:
        from evaluation.native_solver import solve_swe_prod

        namespace = {}
        exec("from evaluation.native_solver.solve_swe_prod import *", namespace)
        for name in ("final_diff_sha256", "git_diff", "run_prod_solver"):
            self.assertIn(name, solve_swe_prod.__dict__)
            self.assertIn(name, dir(solve_swe_prod))
            self.assertIs(namespace[name], getattr(solve_swe_prod, name))

    def test_launcher_uses_exact_container_module_command(self) -> None:
        launcher = assigned_string(
            ROOT / "evaluation" / "evalscope_multiagent_native_runner.py",
            "_SOLVER_LAUNCHER",
        )
        expected = (
            "cd /opt/multiagent\n"
            "  exec python3 -m evaluation.native_solver.solve_swe_prod "
            '"$prompt_file" "${timeout_args[@]}"'
        )
        self.assertIn(expected, launcher)
        self.assertNotIn('python3 "$solver"', launcher)

    def test_bake_copies_package_initializers(self) -> None:
        from evaluation.swe_bench_pro_on_demand import OnDemandImageManager

        with tempfile.TemporaryDirectory() as temporary_dir:
            temporary = Path(temporary_dir)
            manager = OnDemandImageManager(
                archive_dir=temporary / "archives",
                status_path=temporary / "status.json",
                platform="linux/amd64",
                image_timeout=60,
                retries=1,
                backoff_s=0,
                min_free_gb=0,
                prune_after_sample=False,
                native_solver_source=ROOT,
            )
            copy_lines, package_hint = manager._copy_native_solver_source(temporary / "context")
            baked_root = temporary / "context" / "multiagent"
            self.assertTrue((baked_root / "evaluation" / "__init__.py").is_file())
            self.assertTrue((baked_root / "evaluation" / "native_solver" / "__init__.py").is_file())
            self.assertEqual(package_hint, f"python3 -m {MODULE_ENTRYPOINT}")
            self.assertEqual(
                copy_lines[-1],
                "RUN chmod +x /opt/multiagent/launch.sh /opt/multiagent/bin/multiagent",
            )
            self.assertIn(
                "COPY --from=multiagent-builder /build/target/release/multiagent /opt/multiagent/bin/multiagent",
                copy_lines,
            )
            self.assertEqual(
                manager._rust_builder_lines()[0],
                "FROM rust:1.85-alpine AS multiagent-builder",
            )
            self.assertIn("RUN cargo build --release --locked", manager._rust_builder_lines())

    def test_native_modules_have_strict_relative_imports(self) -> None:
        failures = []
        for path in sorted(NATIVE_SOLVER.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if any(alias.name == "*" for alias in node.names):
                        failures.append(f"{path.name}:{node.lineno}: wildcard import")
                    if node.level == 0 and (node.module or "").startswith("swe_prod_"):
                        failures.append(f"{path.name}:{node.lineno}: top-level native import")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("swe_prod_"):
                            failures.append(f"{path.name}:{node.lineno}: top-level native import")
            for node in tree.body:
                if not isinstance(node, ast.Try):
                    continue
                imports_module = any(isinstance(child, (ast.Import, ast.ImportFrom)) for child in node.body)
                catches_import_error = any(
                    isinstance(handler.type, ast.Name) and handler.type.id == "ImportError"
                    for handler in node.handlers
                )
                if imports_module and catches_import_error:
                    failures.append(f"{path.name}:{node.lineno}: import fallback")
        self.assertEqual(failures, [])

    def test_dependency_import_error_preserves_original_traceback(self) -> None:
        script = r'''
import builtins
import traceback

class SyntheticDependencyError(ImportError):
    pass

original_import = builtins.__import__

def fail_lifecycle_dependency(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 1 and "swe_prod_repository" in (fromlist or ()):
        raise SyntheticDependencyError("synthetic-native-solver-dependency")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = fail_lifecycle_dependency
try:
    import evaluation.native_solver.swe_prod_lifecycle
except SyntheticDependencyError as exc:
    rendered = traceback.format_exc()
    assert str(exc) == "synthetic-native-solver-dependency"
    assert "fail_lifecycle_dependency" in rendered
    assert "swe_prod_lifecycle.py" in rendered
else:
    raise AssertionError("synthetic dependency error was caught or rerouted")
'''
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
