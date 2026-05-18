"""Workspace-aware path resolution for benchmark task runs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


CANONICAL_BENCHMARK_DIRS = {
    "tasks",
    "tasks_no_skills_generate",
    "tasks-no-skills",
}


class BenchmarkPathViolation(ValueError):
    """Raised when benchmark execution drifts outside the task workspace."""


def _resolve_existing_or_parent(path: Path) -> Path:
    if path.exists():
        return path.resolve()
    parent = path.parent if path.parent != Path("") else Path.cwd()
    return parent.resolve() / path.name


def is_path_inside(path: str | Path | None, workspace_root: str | Path | None) -> bool:
    """Return True when ``path`` resolves inside ``workspace_root``."""
    if path is None or workspace_root is None:
        return False
    root = Path(workspace_root).resolve()
    candidate = _resolve_existing_or_parent(Path(path))
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def file_material_snapshot(path: str | Path | None, *, prefix: str) -> dict[str, Any]:
    """Return cheap material signals for a file path."""
    if path is None:
        return {
            f"{prefix}_exists": None,
            f"{prefix}_size": None,
            f"{prefix}_mtime": None,
            f"{prefix}_sha256": None,
        }
    resolved = _resolve_existing_or_parent(Path(path))
    if not resolved.exists() or not resolved.is_file():
        return {
            f"{prefix}_exists": False,
            f"{prefix}_size": None,
            f"{prefix}_mtime": None,
            f"{prefix}_sha256": None,
        }
    stat = resolved.stat()
    digest = hashlib.sha256()
    with resolved.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        f"{prefix}_exists": True,
        f"{prefix}_size": stat.st_size,
        f"{prefix}_mtime": stat.st_mtime,
        f"{prefix}_sha256": digest.hexdigest(),
    }


def material_change_detected(before: dict[str, Any], after: dict[str, Any]) -> bool | None:
    """Compare before/after material snapshot dictionaries."""
    before_exists = before.get("expected_output_exists_before")
    after_exists = after.get("expected_output_exists_after")
    if before_exists is None or after_exists is None:
        return None
    if before_exists != after_exists:
        return True
    if not after_exists:
        return False
    return any(
        before.get(before_key) != after.get(after_key)
        for before_key, after_key in (
            ("expected_output_size_before", "expected_output_size_after"),
            ("expected_output_mtime_before", "expected_output_mtime_after"),
            ("expected_output_sha256_before", "expected_output_sha256_after"),
        )
    )


def _has_canonical_benchmark_segment(path: Path) -> bool:
    return any(part in CANONICAL_BENCHMARK_DIRS for part in path.parts)


def _find_workspace_match(workspace_root: Path, raw_path: Path) -> Path | None:
    name = raw_path.name
    if not name:
        return None
    matches = sorted(
        candidate.resolve()
        for candidate in workspace_root.rglob(name)
        if candidate.is_file()
    )
    return matches[0] if matches else None


@dataclass
class WorkspacePathResolver:
    """Authoritative benchmark resolver anchored to one workspace root."""

    workspace_root: Path
    expected_output_path: Path | None = None
    target_workbook_path: Path | None = None
    files_touched: list[str] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        workspace_root: str | Path,
        *,
        expected_output_path: str | Path | None = None,
        target_workbook_path: str | Path | None = None,
    ) -> "WorkspacePathResolver":
        root = Path(workspace_root).resolve()
        return cls(
            workspace_root=root,
            expected_output_path=(
                cls._resolve_against_root(root, expected_output_path)
                if expected_output_path
                else None
            ),
            target_workbook_path=(
                cls._resolve_against_root(root, target_workbook_path)
                if target_workbook_path
                else None
            ),
        )

    @staticmethod
    def _resolve_against_root(root: Path, raw_path: str | Path | None) -> Path:
        path = Path(raw_path) if raw_path is not None else root
        return _resolve_existing_or_parent(path if path.is_absolute() else root / path)

    def is_inside(self, path: str | Path | None) -> bool:
        return is_path_inside(path, self.workspace_root)

    def canonicalize_and_validate(
        self,
        raw_path: str | Path,
        *,
        purpose: str = "path",
        prefer_workspace_match: bool = True,
    ) -> Path:
        """Resolve a task path and reject outside-workspace benchmark drift."""
        candidate = Path(raw_path)
        resolved = self._resolve_against_root(self.workspace_root, candidate)
        if self.is_inside(resolved):
            return resolved

        if prefer_workspace_match and _has_canonical_benchmark_segment(resolved):
            workspace_match = _find_workspace_match(self.workspace_root, resolved)
            if workspace_match is not None:
                return workspace_match

        raise BenchmarkPathViolation(
            f"{purpose} resolves outside benchmark workspace: {resolved} "
            f"(workspace: {self.workspace_root})"
        )

    def record_touched(self, raw_path: str | Path) -> Path:
        path = self.canonicalize_and_validate(
            raw_path,
            purpose="touched file",
            prefer_workspace_match=False,
        )
        value = str(path)
        if value not in self.files_touched:
            self.files_touched.append(value)
        return path

    def validate_final_output(self, raw_path: str | Path | None) -> Path | None:
        if raw_path is None or str(raw_path).strip() == "":
            return None
        return self.canonicalize_and_validate(
            raw_path,
            purpose="final output path",
            prefer_workspace_match=False,
        )

    def to_state(self, *, final_output_path: str | Path | None = None) -> dict[str, Any]:
        final_path = str(final_output_path) if final_output_path else None
        target_path = self.target_workbook_path or self.expected_output_path
        expected_path = str(self.expected_output_path) if self.expected_output_path else None
        target_path_text = str(target_path) if target_path else None
        target_inside = self.is_inside(target_path) if target_path else None
        target_is_expected = (
            target_path.resolve() == self.expected_output_path.resolve()
            if target_path and self.expected_output_path
            else None
        )
        return {
            "workspace_root": str(self.workspace_root),
            "current_working_directory": str(self.workspace_root),
            "workspace_grounded": True,
            "expected_output_path": expected_path,
            "target_workbook_path": target_path_text,
            "chosen_target_workbook_path": target_path_text,
            "target_path_in_workspace": target_inside,
            "chosen_target_in_workspace": target_inside,
            "chosen_target_is_expected_or_derivative": (
                bool(target_inside) and (target_is_expected is not False)
                if target_inside is not None
                else None
            ),
            "final_output_path": final_path,
            "final_output_path_in_workspace": (
                self.is_inside(final_path) if final_path else None
            ),
            "files_touched": list(self.files_touched),
        }

    def material_state_before(self) -> dict[str, Any]:
        snapshot = file_material_snapshot(self.expected_output_path, prefix="expected_output")
        return {
            "expected_output_exists_before": snapshot["expected_output_exists"],
            "expected_output_size_before": snapshot["expected_output_size"],
            "expected_output_mtime_before": snapshot["expected_output_mtime"],
            "expected_output_sha256_before": snapshot["expected_output_sha256"],
        }

    def material_state_after(self) -> dict[str, Any]:
        snapshot = file_material_snapshot(self.expected_output_path, prefix="expected_output")
        return {
            "expected_output_exists_after": snapshot["expected_output_exists"],
            "expected_output_size_after": snapshot["expected_output_size"],
            "expected_output_mtime_after": snapshot["expected_output_mtime"],
            "expected_output_sha256_after": snapshot["expected_output_sha256"],
        }


def benchmark_path_state_from_options(options: dict[str, Any]) -> WorkspacePathResolver | None:
    """Build benchmark path state from known option keys, if present."""
    workspace_root = (
        options.get("benchmark_workspace_root")
        or options.get("workspace_root")
        or options.get("_evoskill_workspace_root")
        or options.get("cwd")
        or options.get("working_directory")
    )
    expected_output_path = (
        options.get("expected_output_path")
        or options.get("_evoskill_expected_output_path")
    )
    target_workbook_path = (
        options.get("target_workbook_path")
        or options.get("_evoskill_target_workbook_path")
    )

    benchmark_enabled = bool(
        options.get("_evoskill_benchmark_task")
        or options.get("benchmark_task")
        or expected_output_path
        or target_workbook_path
    )
    if not benchmark_enabled or not workspace_root:
        return None

    return WorkspacePathResolver.create(
        workspace_root,
        expected_output_path=expected_output_path,
        target_workbook_path=target_workbook_path,
    )


def extract_path_values(value: Any) -> list[str]:
    """Recursively extract likely filesystem path values from tool payloads."""
    paths: list[str] = []
    path_keys = {
        "file",
        "file_path",
        "filepath",
        "filename",
        "path",
        "target",
        "target_path",
        "output_path",
        "final_output_path",
    }

    def visit(item: Any, key_hint: str | None = None) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                visit(child, str(key).lower())
            return
        if isinstance(item, list):
            for child in item:
                visit(child, key_hint)
            return
        if isinstance(item, str) and key_hint in path_keys:
            paths.append(item)

    visit(value)
    return paths


def first_final_output_path(raw_structured: Any) -> str | None:
    """Return the most likely final artifact path from structured output."""
    if not isinstance(raw_structured, dict):
        return None
    for key in (
        "final_output_path",
        "expected_output_path",
        "output_path",
        "artifact_path",
        "workbook_path",
        "path",
    ):
        value = raw_structured.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None
