from __future__ import annotations

from pathlib import Path

import pytest

from src.harness.opencode import executor
from src.harness.workspace_paths import (
    BenchmarkPathViolation,
    WorkspacePathResolver,
)
from src.schemas import AgentResponse


def _assistant_payload(
    structured: dict,
    *,
    parts: list[dict] | None = None,
    diagnostics: dict | None = None,
) -> list[dict]:
    return [{
        "session_id": "bench-session",
        "chat_info": {},
        "messages": [
            {
                "info": {
                    "role": "assistant",
                    "structured": structured,
                    "cost": 0,
                    "tokens": {},
                },
                "parts": parts or [{"type": "text", "text": ""}],
            }
        ],
        "diagnostics": diagnostics or {},
    }]


def _benchmark_options(tmp_path: Path, expected: Path | None = None) -> dict:
    expected_path = expected or tmp_path / "deliverable.xlsx"
    return {
        "cwd": str(tmp_path),
        "_evoskill_benchmark_task": True,
        "expected_output_path": str(expected_path),
        "provider_id": "anthropic",
        "model_id": "claude-sonnet-4-6",
    }


def _diagnostic_before(options: dict) -> dict:
    return executor._build_message_diagnostic(options, base_url="", query="test")


def test_workspace_file_preferred_over_canonical_repo_file(tmp_path: Path) -> None:
    workspace = tmp_path / "run-workspace"
    canonical = tmp_path / "DreamSkillsBench" / "tasks" / "task-1"
    workspace.mkdir()
    canonical.mkdir(parents=True)

    workspace_copy = workspace / "answer.xlsx"
    canonical_copy = canonical / "answer.xlsx"
    workspace_copy.write_text("workspace", encoding="utf-8")
    canonical_copy.write_text("canonical", encoding="utf-8")

    resolver = WorkspacePathResolver.create(workspace)

    resolved = resolver.canonicalize_and_validate(canonical_copy)

    assert resolved == workspace_copy.resolve()


def test_outside_workspace_canonical_path_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "run-workspace"
    canonical = tmp_path / "DreamSkillsBench" / "tasks_no_skills_generate" / "task-1"
    workspace.mkdir()
    canonical.mkdir(parents=True)
    outside_file = canonical / "missing.xlsx"
    outside_file.write_text("canonical", encoding="utf-8")

    resolver = WorkspacePathResolver.create(workspace)

    with pytest.raises(BenchmarkPathViolation, match="outside benchmark workspace"):
        resolver.canonicalize_and_validate(outside_file)


def test_expected_artifact_path_stored_in_structured_state(tmp_path: Path) -> None:
    expected = tmp_path / "deliverable.xlsx"
    options = _benchmark_options(tmp_path, expected)
    diagnostics = _diagnostic_before(options)
    expected.write_text("created", encoding="utf-8")

    fields = executor.parse_response(
        _assistant_payload({
            "final_answer": "done",
            "reasoning": "saved",
            "final_output_path": str(expected),
        }, diagnostics=diagnostics),
        AgentResponse,
        lambda: options,
    )

    assert fields["workspace_root"] == str(tmp_path.resolve())
    assert fields["expected_output_path"] == str(expected)
    assert fields["workspace_grounded"] is True
    assert fields["diagnostics"]["workspace_root"] == str(tmp_path.resolve())
    assert fields["diagnostics"]["expected_output_path"] == str(expected)
    assert fields["diagnostics"]["workspace_grounded"] is True


def test_wrong_path_finalization_blocked(tmp_path: Path) -> None:
    outside = tmp_path.parent / "tasks" / "task-1" / "deliverable.xlsx"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("wrong", encoding="utf-8")
    expected = tmp_path / "deliverable.xlsx"
    options = _benchmark_options(tmp_path, expected)
    diagnostics = _diagnostic_before(options)
    expected.write_text("right file changed", encoding="utf-8")

    fields = executor.parse_response(
        _assistant_payload({
            "final_answer": "done",
            "reasoning": "saved",
            "final_output_path": str(outside),
        }, diagnostics=diagnostics),
        AgentResponse,
        lambda: options,
    )

    assert fields["output"] is None
    assert fields["is_error"] is True
    assert fields["wrong_path_error"]
    assert fields["parse_error"].startswith("wrong_path_error:")
    assert fields["diagnostics"]["path_violation_result"] == "wrong_path_error"


def test_final_output_path_surfaced_with_workspace_flag(tmp_path: Path) -> None:
    final_path = tmp_path / "deliverable.xlsx"
    options = _benchmark_options(tmp_path, final_path)
    diagnostics = _diagnostic_before(options)
    final_path.write_text("created", encoding="utf-8")

    fields = executor.parse_response(
        _assistant_payload({
            "final_answer": "done",
            "reasoning": "saved",
            "final_output_path": str(final_path),
        }, diagnostics=diagnostics),
        AgentResponse,
        lambda: options,
    )

    assert fields["final_output_path"] == str(final_path)
    assert fields["final_output_path_in_workspace"] is True
    assert fields["diagnostics"]["final_output_path"] == str(final_path)
    assert fields["diagnostics"]["final_output_path_in_workspace"] is True
    assert fields["benchmark_final_status"]["final_output_path"] == str(final_path)
    assert fields["benchmark_final_status"]["final_output_path_in_workspace"] is True


def test_outside_workspace_touched_path_hard_fails(tmp_path: Path) -> None:
    outside = tmp_path.parent / "tasks-no-skills" / "task-1" / "deliverable.xlsx"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("wrong", encoding="utf-8")
    expected = tmp_path / "deliverable.xlsx"
    options = _benchmark_options(tmp_path, expected)
    diagnostics = _diagnostic_before(options)
    expected.write_text("right file changed", encoding="utf-8")

    fields = executor.parse_response(
        _assistant_payload(
            {
                "final_answer": "done",
                "reasoning": "saved",
                "final_output_path": str(expected),
            },
            parts=[{
                "type": "tool",
                "name": "write",
                "path": str(outside),
            }],
            diagnostics=diagnostics,
        ),
        AgentResponse,
        lambda: options,
    )

    assert fields["output"] is None
    assert fields["wrong_path_error"]
    assert "touched file resolves outside benchmark workspace" in fields["wrong_path_error"]


def test_material_change_detected_on_expected_workbook(tmp_path: Path) -> None:
    expected = tmp_path / "deliverable.xlsx"
    expected.write_text("before", encoding="utf-8")
    options = _benchmark_options(tmp_path, expected)
    diagnostics = _diagnostic_before(options)
    expected.write_text("after", encoding="utf-8")

    fields = executor.parse_response(
        _assistant_payload({
            "final_answer": "done",
            "reasoning": "saved",
            "final_output_path": str(expected),
        }, diagnostics=diagnostics),
        AgentResponse,
        lambda: options,
    )

    assert fields["output"] is not None
    assert fields["material_change_detected"] is True
    assert fields["diagnostics"]["expected_output_exists_before"] is True
    assert fields["diagnostics"]["expected_output_exists_after"] is True
    assert fields["benchmark_final_status"]["task_complete"] is True


def test_no_meaningful_edit_blocks_success(tmp_path: Path) -> None:
    expected = tmp_path / "deliverable.xlsx"
    expected.write_text("unchanged", encoding="utf-8")
    options = _benchmark_options(tmp_path, expected)
    diagnostics = _diagnostic_before(options)

    fields = executor.parse_response(
        _assistant_payload({
            "final_answer": "done",
            "reasoning": "saved",
            "final_output_path": str(expected),
        }, diagnostics=diagnostics),
        AgentResponse,
        lambda: options,
    )

    assert fields["output"] is None
    assert fields["is_error"] is True
    assert fields["material_change_detected"] is False
    assert fields["parse_error"].startswith("material_change_missing:")
    assert fields["benchmark_final_status"]["task_complete"] is False
    assert fields["benchmark_final_status"]["termination_reason"] == "material_change_missing"


def test_success_requires_valid_final_structured_status(tmp_path: Path) -> None:
    expected = tmp_path / "deliverable.xlsx"
    options = _benchmark_options(tmp_path, expected)
    diagnostics = _diagnostic_before(options)
    expected.write_text("created", encoding="utf-8")

    fields = executor.parse_response(
        _assistant_payload({
            "final_answer": "done",
            "reasoning": "saved",
            "final_output_path": str(expected),
        }, diagnostics=diagnostics),
        AgentResponse,
        lambda: options,
    )

    status = fields["benchmark_final_status"]
    for key in (
        "summary",
        "termination_reason",
        "task_complete",
        "workspace_root",
        "expected_output_path",
        "final_output_path",
        "final_output_path_in_workspace",
        "files_touched",
        "material_change_detected",
        "wrong_path_error",
        "target_workbook_path",
        "workspace_grounded",
    ):
        assert key in status
    assert status["task_complete"] is True
    assert status["workspace_grounded"] is True
