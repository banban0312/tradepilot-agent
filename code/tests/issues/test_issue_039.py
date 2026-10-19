from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from app.auth.context import AuthContext
from app.auth.dependencies import get_current_identity, require_csrf
from app.config import settings
from app.evaluations.routes import (
    EvaluationRunRequest,
    _evaluation_child_environment,
    _evaluation_database_url,
    _read_progress,
    _write_status,
    shutdown_evaluations,
)
from app.main import app
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine import make_url

from evals.service import EvaluationService


def _identity(*roles: str) -> AuthContext:
    return AuthContext(uuid4(), "test-user", frozenset(roles), frozenset({uuid4()}))


def test_run_request_is_fixed_to_supported_modes_dataset_and_categories() -> None:
    assert EvaluationRunRequest().mode == "offline"
    with pytest.raises(ValidationError):
        EvaluationRunRequest.model_validate({"dataset_version": "../business"})
    with pytest.raises(ValidationError):
        EvaluationRunRequest.model_validate({"mode": "unknown"})
    with pytest.raises(ValidationError):
        EvaluationRunRequest.model_validate({"categories": ["normal", "normal"]})


def test_evaluation_database_must_be_separate_from_business_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "database_url",
        SecretStr("postgresql+psycopg://runner:pass@db/tradepilot"),
    )
    monkeypatch.setattr(
        settings,
        "evaluation_database_admin_url",
        SecretStr("postgresql+psycopg://eval:other-pass@eval-db/evaluations"),
    )
    isolated = make_url(_evaluation_database_url("tp_eval_123"))
    assert isolated.host == "eval-db"
    assert isolated.database == "tp_eval_123"
    monkeypatch.setattr(
        settings,
        "database_url",
        SecretStr("postgresql+psycopg://runner:pass@localhost/tradepilot"),
    )
    with pytest.raises(RuntimeError, match="EVAL_ISOLATION_UNAVAILABLE"):
        _evaluation_database_url("tp_eval_123")
    monkeypatch.setattr(
        settings,
        "database_url",
        SecretStr("postgresql+psycopg://runner:pass@db/tradepilot"),
    )
    monkeypatch.setattr(
        settings,
        "evaluation_database_admin_url",
        SecretStr("postgresql+psycopg://runner:pass@db/evaluations"),
    )
    with pytest.raises(RuntimeError, match="EVAL_ISOLATION_UNAVAILABLE"):
        _evaluation_database_url("tp_eval_123")


def test_running_status_only_accepts_bounded_case_progress(tmp_path: Path) -> None:
    state = {
        "status": "RUNNING",
        "progress": {"completed": 0, "total": 2},
    }
    (tmp_path / "progress.json").write_text(
        json.dumps({"completed": 1, "total": 2, "case_id": "case-1"}),
        encoding="utf-8",
    )
    assert _read_progress(tmp_path, state) == {
        "completed": 1,
        "total": 2,
        "case_id": "case-1",
    }
    (tmp_path / "progress.json").write_text(
        json.dumps({"completed": 3, "total": 2}),
        encoding="utf-8",
    )
    assert _read_progress(tmp_path, state) == state["progress"]
    completed = {"status": "COMPLETED", "progress": {"completed": 2, "total": 2}}
    assert _read_progress(tmp_path, completed) == completed["progress"]


def test_evaluation_children_do_not_receive_database_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://business-user:secret@db/business")
    monkeypatch.setenv("POSTGRES_PASSWORD", "business-secret")
    monkeypatch.setenv("EVALUATION_POSTGRES_PASSWORD", "evaluation-secret")
    monkeypatch.setenv(
        "EVALUATION_DATABASE_ADMIN_URL", "postgresql://admin:secret@eval-db/postgres"
    )
    env = _evaluation_child_environment()
    assert "DATABASE_URL" not in env
    assert "POSTGRES_PASSWORD" not in env
    assert "EVALUATION_POSTGRES_PASSWORD" not in env
    assert "EVALUATION_DATABASE_ADMIN_URL" not in env


def test_evaluation_center_requires_system_admin_and_returns_real_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = uuid4()
    run_dir = tmp_path / str(run_id)
    run_dir.mkdir()
    _write_status(
        run_dir,
        {
            "run_id": str(run_id),
            "status": "COMPLETED",
            "mode": "offline",
            "dataset_version": "v1",
            "started_at": "2026-10-19T00:00:00+00:00",
            "completed_at": "2026-10-19T00:01:00+00:00",
            "progress": {"completed": 1, "total": 1},
            "error_code": None,
        },
    )
    summary = {"case_count": 1, "status_counts": {"PASS": 1}}
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run_dir / "cases.jsonl").write_text(
        json.dumps({"case_id": "case-1", "trace_id": "trace-ref"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "report.md").write_text("# report", encoding="utf-8")
    monkeypatch.setattr(settings, "evaluation_reports_root", tmp_path)
    app.dependency_overrides[get_current_identity] = lambda: _identity(
        "business_operator"
    )
    try:
        with TestClient(app) as client:
            denied = client.get("/api/v1/admin/eval-runs")
            assert denied.status_code == 403
            app.dependency_overrides[require_csrf] = lambda: _identity(
                "business_operator"
            )
            denied_start = client.post(
                "/api/v1/admin/eval-runs", json={"mode": "offline"}
            )
            assert denied_start.status_code == 403
            app.dependency_overrides[get_current_identity] = lambda: _identity(
                "system_admin"
            )
            response = client.get(f"/api/v1/admin/eval-runs/{run_id}")
            assert response.status_code == 200
            assert response.json()["data"]["summary"] == summary
            assert response.json()["data"]["cases"][0]["trace_id"] == "trace-ref"
            report = client.get(f"/api/v1/admin/eval-runs/{run_id}/report/report.md")
            assert report.status_code == 200 and report.text == "# report"
    finally:
        app.dependency_overrides.pop(get_current_identity, None)
        app.dependency_overrides.pop(require_csrf, None)


@pytest.mark.asyncio
async def test_evaluation_service_reports_per_case_progress() -> None:
    class Executor:
        async def execute_case(self, case: dict, *, mode: str) -> dict:
            assert mode == "offline"
            return {}

    updates: list[tuple[int, int, str]] = []
    cases = [
        {"id": "first", "category": "normal", "expected": {"assertions": []}},
        {"id": "second", "category": "normal", "expected": {"assertions": []}},
    ]
    results = await EvaluationService(Executor(), mode="offline").run(
        cases,
        on_case_completed=lambda done, total, result: updates.append(
            (done, total, result.case_id)
        ),
    )
    assert [result.status for result in results] == ["PASS", "PASS"]
    assert updates == [(1, 2, "first"), (2, 2, "second")]


@pytest.mark.asyncio
async def test_api_shutdown_cancels_active_evaluation_tasks() -> None:
    from app.evaluations.routes import _active_tasks

    never_finishes = asyncio.Event()
    task = asyncio.create_task(never_finishes.wait())
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)
    await shutdown_evaluations()
    await asyncio.sleep(0)
    assert task.cancelled()
    assert task not in _active_tasks
