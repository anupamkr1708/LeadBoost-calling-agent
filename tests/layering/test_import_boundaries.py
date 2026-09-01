"""AST-based layering test.

This is not a style check that could be silently skipped — it is a real
pytest test that parses every first-party .py file in the repo with Python's
`ast` module, collects its imports, and fails if any import violates
`app.layers.LAYER_GRAPH`. Mirrors the technique used by both source repos.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.layers import LAYER_GRAPH

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories we never scan for layering purposes.
EXCLUDED_DIR_PARTS = {
    ".venv", "__pycache__", ".git", "alembic", ".secrets_local",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "htmlcov",
}


def _iter_first_party_python_files() -> list[Path]:
    files = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in EXCLUDED_DIR_PARTS for part in path.parts):
            continue
        files.append(path)
    return files


def _module_name_for(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts)


def _collect_imports(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):  # noqa: SIM102 - kept separate from ast.Import intentionally, different node shape
            if node.module:
                names.append(node.module)
    return names


def _all_modules_and_imports() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for path in _iter_first_party_python_files():
        module_name = _module_name_for(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:  # pragma: no cover - a syntax error is its own failure
            pytest.fail(f"Could not parse {path} for layering check: {e}")
        result[module_name] = _collect_imports(tree)
    return result


def test_vendor_sdk_confinement() -> None:
    """No module outside the allowed importer prefixes may import a
    forbidden vendor library (Exotel, Deepgram, Groq clients)."""
    modules_and_imports = _all_modules_and_imports()
    violations: list[str] = []

    for module_name, imports in modules_and_imports.items():
        for rule in LAYER_GRAPH.vendor_confinement:
            is_allowed_importer = any(
                module_name == prefix or module_name.startswith(prefix + ".")
                for prefix in rule.allowed_importer_prefixes
            )
            if is_allowed_importer:
                continue
            for imported in imports:
                for forbidden in rule.forbidden_import_prefixes:
                    if imported == forbidden or imported.startswith(forbidden + "."):
                        violations.append(
                            f"{module_name} imports '{imported}' but only "
                            f"{rule.allowed_importer_prefixes} may do so. {rule.reason}"
                        )

    assert not violations, "Layering violations found:\n" + "\n".join(violations)


def test_entrypoint_modules_are_never_imported() -> None:
    """app.main is the composition root; nothing OUTSIDE THE TEST SUITE may
    import it (no cycles back into the entrypoint from application code).
    Contract tests are explicitly allowed to import it — that's how they
    exercise the real, fully-wired app (Phase Gate Protocol requires exactly
    this: real entrypoint, not an isolated re-construction)."""
    modules_and_imports = _all_modules_and_imports()
    violations: list[str] = []

    for module_name, imports in modules_and_imports.items():
        if module_name in LAYER_GRAPH.entrypoint_only_modules:
            continue
        if module_name.startswith("tests."):
            continue
        for imported in imports:
            if imported in LAYER_GRAPH.entrypoint_only_modules:
                violations.append(f"{module_name} imports entrypoint module '{imported}'")

    assert not violations, "Entrypoint import violations found:\n" + "\n".join(violations)


def test_internal_dependency_bans() -> None:
    """Phase 1's internal layering rules (docs/PHASE1_DESIGN.md): dependency
    direction is orchestrator -> conversation -> telephony, never the
    reverse, and orchestrator/ stays framework-agnostic (no fastapi
    import). See app.layers.LAYER_GRAPH.dependency_bans."""
    modules_and_imports = _all_modules_and_imports()
    violations: list[str] = []

    for module_name, imports in modules_and_imports.items():
        for rule in LAYER_GRAPH.dependency_bans:
            is_forbidden_importer = any(
                module_name == prefix or module_name.startswith(prefix + ".")
                for prefix in rule.forbidden_importer_prefixes
            )
            if not is_forbidden_importer:
                continue
            for imported in imports:
                for banned in rule.banned_import_prefixes:
                    if imported == banned or imported.startswith(banned + "."):
                        violations.append(f"{module_name} imports '{imported}'. {rule.reason}")

    assert not violations, "Dependency ban violations found:\n" + "\n".join(violations)


def test_layering_check_actually_scans_a_nonzero_number_of_files() -> None:
    """Guards against this test silently passing because glob found nothing
    (e.g. a bad REPO_ROOT) — an empty scan must not read as 'all clear'."""
    modules_and_imports = _all_modules_and_imports()
    assert len(modules_and_imports) > 10, (
        f"Only found {len(modules_and_imports)} first-party modules — "
        "this looks like the scan is broken, not like the repo is small."
    )
