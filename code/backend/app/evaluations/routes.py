"""管理员评测运行 API；每次运行使用独立 PostgreSQL 数据库和服务进程。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shutil
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.engine import make_url

from app.auth.context import SYSTEM_ADMIN, AuthContext
from app.auth.dependencies import get_current_identity, require_csrf
from app.config import settings

router = APIRouter(prefix="/api/v1/admin/eval-runs", tags=["admin-evaluations"])
_active_tasks: set[asyncio.Task[None]] = set()
_REPORT_FILES = {"summary.json", "cases.jsonl", "report.md"}
_CATEGORY_COUNTS = {
    "normal": 6,
    "abnormal": 6,
    "ambiguity": 6,
    "unauthorized": 6,
    "quota": 6,
    "duplicate_submission": 6,
}


def _selected_case_count(categories: list[str]) -> int:
    return (
        sum(_CATEGORY_COUNTS.values()) + 5
        if not categories
        else sum(_CATEGORY_COUNTS[item] for item in categories)
    )


class EvaluationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: Literal["v1"] = "v1"
    mode: Literal["offline", "online"] = "offline"
    categories: list[
        Literal[
            "normal",
            "abnormal",
            "ambiguity",
            "unauthorized",
            "quota",
            "duplicate_submission",
        ]
    ] = Field(default_factory=list, max_length=6)

    @field_validator("categories")
    @classmethod
    def categories_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("评测分类不能重复")
        return value


def _admin(context: AuthContext) -> None:
    if SYSTEM_ADMIN not in context.roles:
        raise HTTPException(
            status_code=403,
            detail={"code": "ACCESS_DENIED", "message": "当前账号没有评测中心访问权限"},
        )


def _run_dir(run_id: UUID) -> Path:
    root = settings.evaluation_reports_root.resolve()
    target = (root / str(run_id)).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=404, detail="评测运行不存在")
    return target


def _write_status(directory: Path, value: dict[str, object]) -> None:
    temporary = directory / "run.json.tmp"
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(directory / "run.json")


def _read_status(directory: Path) -> dict[str, object] | None:
    try:
        value = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_progress(directory: Path, state: dict[str, object]) -> dict[str, object]:
    base = state.get("progress")
    if state.get("status") != "RUNNING":
        return base if isinstance(base, dict) else {"completed": 0, "total": 0}
    try:
        value = json.loads((directory / "progress.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base if isinstance(base, dict) else {"completed": 0, "total": 0}
    if not isinstance(value, dict) or not isinstance(base, dict):
        return base if isinstance(base, dict) else {"completed": 0, "total": 0}
    completed, total = value.get("completed"), value.get("total")
    if (
        isinstance(completed, int)
        and isinstance(total, int)
        and 0 <= completed <= total
        and total == base.get("total")
    ):
        return value
    return base


def _new_port() -> int:
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        return int(listener.getsockname()[1])


def _evaluation_database_url(database_name: str) -> str:
    business_url = make_url(settings.database_url.get_secret_value())
    admin_setting = settings.evaluation_database_admin_url
    if admin_setting is None:
        raise RuntimeError("EVAL_ISOLATION_UNAVAILABLE")
    admin_url = make_url(admin_setting.get_secret_value())
    # Evaluation has a separate PostgreSQL server and credentials; its child
    # processes cannot connect to the business database with their environment.
    if (
        business_url.host != "db"
        or admin_url.host != "eval-db"
        or business_url.host == admin_url.host
        or admin_url.database in {None, "postgres", database_name}
    ):
        raise RuntimeError("EVAL_ISOLATION_UNAVAILABLE")
    return admin_url.set(database=database_name).render_as_string(hide_password=False)


def _evaluation_child_environment() -> dict[str, str]:
    """不向评测 API、MCP、CLI 或 Worker 传递主库或评测管理凭据。"""
    env = os.environ.copy()
    for protected_name in (
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "EVALUATION_POSTGRES_PASSWORD",
        "EVALUATION_DATABASE_ADMIN_URL",
    ):
        env.pop(protected_name, None)
    return env


def _create_database(database_name: str) -> None:
    admin_setting = settings.evaluation_database_admin_url
    if admin_setting is None:
        raise RuntimeError("EVAL_ISOLATION_UNAVAILABLE")
    url = make_url(admin_setting.get_secret_value()).set(
        drivername="postgresql", database="postgres"
    )
    with psycopg.connect(
        url.render_as_string(hide_password=False), autocommit=True
    ) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))


def _drop_database(database_name: str) -> None:
    admin_setting = settings.evaluation_database_admin_url
    if admin_setting is None:
        raise RuntimeError("EVAL_ISOLATION_UNAVAILABLE")
    url = make_url(admin_setting.get_secret_value()).set(
        drivername="postgresql", database="postgres"
    )
    with psycopg.connect(
        url.render_as_string(hide_password=False), autocommit=True
    ) as db:
        db.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database_name)
            )
        )


async def _command(
    arguments: list[str],
    *,
    env: dict[str, str],
    acceptable_codes: tuple[int, ...] = (0,),
) -> int:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd="/app",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return_code = await process.wait()
    except asyncio.CancelledError:
        await _stop_process(process)
        raise
    if return_code not in acceptable_codes:
        raise RuntimeError("EVAL_SANDBOX_SETUP_FAILED")
    return return_code


async def _wait_ready(url: str, process: asyncio.subprocess.Process) -> None:
    async with httpx.AsyncClient(timeout=1) as client:
        for _ in range(60):
            if process.returncode is not None:
                raise RuntimeError("EVAL_SANDBOX_SERVICE_FAILED")
            try:
                response = await client.get(url)
                if response.is_success:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise RuntimeError("EVAL_SANDBOX_SERVICE_TIMEOUT")


async def _wait_port(port: int, process: asyncio.subprocess.Process) -> None:
    for _ in range(60):
        if process.returncode is not None:
            raise RuntimeError("EVAL_SANDBOX_SERVICE_FAILED")
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=1
            )
        except (OSError, TimeoutError):
            await asyncio.sleep(0.5)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise RuntimeError("EVAL_SANDBOX_SERVICE_TIMEOUT")


def _start_service(
    arguments: list[str], *, env: dict[str, str]
) -> asyncio.subprocess.Process:
    # Subprocess output is suppressed because service logs can contain business
    # input. Public errors use stable codes and never include process output.
    return asyncio.create_subprocess_exec(
        *arguments,
        cwd="/app",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _stop_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _execute_run(
    run_id: UUID, body: EvaluationRunRequest, directory: Path
) -> None:
    started = datetime.now(UTC).isoformat()
    state: dict[str, object] = {
        "run_id": str(run_id),
        "status": "RUNNING",
        "mode": body.mode,
        "dataset_version": body.dataset_version,
        "started_at": started,
        "completed_at": None,
        "progress": {"completed": 0, "total": _selected_case_count(body.categories)},
        "error_code": None,
    }
    _write_status(directory, state)
    processes: list[asyncio.subprocess.Process] = []
    database_name = f"tp_eval_{run_id.hex}"
    database_created = False
    runtime_root: Path | None = None
    try:
        isolated_url = _evaluation_database_url(database_name)
        create_task = asyncio.create_task(
            asyncio.to_thread(_create_database, database_name)
        )
        try:
            await asyncio.shield(create_task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(create_task)
                database_created = True
            finally:
                raise
        database_created = True
        secret = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        runtime_root = Path("/tmp/tradepilot-eval") / run_id.hex
        runtime_root.mkdir(parents=True, exist_ok=True)
        api_port, mcp_port = _new_port(), _new_port()
        env = {
            **_evaluation_child_environment(),
            "DATABASE_URL": isolated_url,
            "DEMO_MODE": "true",
            "DEMO_ALLOW_SELF_APPROVAL": "false",
            "DEMO_PASSWORD": secrets.token_urlsafe(32),
            "SESSION_COOKIE_SECURE": "false",
            "APP_ORIGIN": "http://localhost",
            "MCP_CONTEXT_SIGNING_KEY": secret,
            "MCP_URL": f"http://127.0.0.1:{mcp_port}/mcp",
            "MCP_HOST": "0.0.0.0",
            "MCP_PORT": str(mcp_port),
            "ATTACHMENTS_ROOT": str(runtime_root / "attachments"),
            "PYTHONPATH": "/workspace:/app",
            "EVAL_API_URL": f"http://api:{api_port}",
            "EVAL_DATABASE_HOST": "eval-db",
            "EVAL_PROJECT_NAME": f"tp-issue037-{run_id.hex}",
            "TRADEPILOT_EVAL_SANDBOX": "1",
            "EVAL_PROGRESS_FILE": str(directory / "progress.json"),
        }
        await _command([sys.executable, "-m", "alembic", "upgrade", "head"], env=env)
        await _command([sys.executable, "-m", "app.seed"], env=env)

        mcp = await _start_service(
            [sys.executable, "-m", "app.mcp_server.main"], env=env
        )
        processes.append(mcp)
        await _wait_port(mcp_port, mcp)
        api_env = {**env, "MCP_URL": f"http://127.0.0.1:{mcp_port}/mcp"}
        api = await _start_service(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(api_port),
            ],
            env=api_env,
        )
        processes.append(api)
        await _wait_ready(f"http://127.0.0.1:{api_port}/health/ready", api)
        # BackendCaseExecutor owns one-shot Workers so recovery and concurrency
        # cases control exactly which job is claimed. A polling Worker here would
        # race those assertions and make the fixed dataset nondeterministic.
        cli_env = {
            **env,
            "MCP_URL": f"http://127.0.0.1:{mcp_port}/mcp",
        }
        cli_args = [
            sys.executable,
            "-m",
            "evals.runner",
            "--mode",
            body.mode,
            "--dataset",
            body.dataset_version,
            "--report-dir",
            str(directory),
            "--progress-file",
            str(directory / "progress.json"),
        ]
        for category in body.categories:
            cli_args.extend(["--category", category])
        await _command(cli_args, env=cli_env, acceptable_codes=(0, 1))
        state["status"] = "COMPLETED"
        state["progress"] = {
            "completed": _selected_case_count(body.categories),
            "total": _selected_case_count(body.categories),
        }
        state["error_code"] = None
    except (
        OSError,
        RuntimeError,
        ValueError,
        psycopg.Error,
        httpx.HTTPError,
        asyncio.SubprocessError,
    ) as error:
        state["status"] = "FAILED"
        state["error_code"] = (
            str(error) if str(error).startswith("EVAL_") else "EVAL_RUN_FAILED"
        )
    except asyncio.CancelledError:
        state["status"] = "FAILED"
        state["error_code"] = "EVAL_RUN_INTERRUPTED"
        raise
    finally:
        for process in reversed(processes):
            await _stop_process(process)
        if runtime_root is not None:
            shutil.rmtree(runtime_root, ignore_errors=True)
        if database_created:
            try:
                await asyncio.to_thread(_drop_database, database_name)
            except (OSError, psycopg.Error):
                state["error_code"] = "EVAL_DATABASE_CLEANUP_FAILED"
                state["status"] = "FAILED"
        state["completed_at"] = datetime.now(UTC).isoformat()
        _write_status(directory, state)


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def start_evaluation(
    body: EvaluationRunRequest,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_csrf)],
) -> dict[str, object]:
    _admin(context)
    if _active_tasks:
        raise HTTPException(
            status_code=409,
            detail={"code": "EVAL_RUN_IN_PROGRESS", "message": "已有评测正在运行"},
        )
    run_id = uuid4()
    directory = _run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=False)
    state: dict[str, object] = {
        "run_id": str(run_id),
        "status": "QUEUED",
        "mode": body.mode,
        "dataset_version": body.dataset_version,
        "started_at": None,
        "completed_at": None,
        "progress": {"completed": 0, "total": _selected_case_count(body.categories)},
        "error_code": None,
    }
    _write_status(directory, state)
    task = asyncio.create_task(_run_with_lock(run_id, body, directory))
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)
    response.headers["Location"] = f"/api/v1/admin/eval-runs/{run_id}"
    return {"data": state, "request_id": getattr(request.state, "request_id", "")}


async def _run_with_lock(
    run_id: UUID, body: EvaluationRunRequest, directory: Path
) -> None:
    try:
        await _execute_run(run_id, body, directory)
    except asyncio.CancelledError:
        state = _read_status(directory) or {"run_id": str(run_id)}
        if state.get("status") in {"QUEUED", "RUNNING"}:
            state["status"] = "FAILED"
            state["error_code"] = "EVAL_RUN_INTERRUPTED"
            state["completed_at"] = datetime.now(UTC).isoformat()
            _write_status(directory, state)
        raise


async def shutdown_evaluations() -> None:
    """优雅关闭 API 时终止评测子进程并清理其隔离数据库。"""
    tasks = tuple(_active_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@router.get("")
async def list_evaluations(
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
) -> dict[str, object]:
    _admin(context)
    root = settings.evaluation_reports_root
    items = []
    if root.is_dir():
        for directory in root.iterdir():
            try:
                UUID(directory.name)
            except ValueError:
                continue
            state = _read_status(directory)
            if state is not None:
                state["progress"] = _read_progress(directory, state)
                summary_path = directory / "summary.json"
                if summary_path.is_file():
                    state["summary"] = json.loads(
                        summary_path.read_text(encoding="utf-8")
                    )
                items.append(state)
    items.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    return {"data": items[:50], "request_id": getattr(request.state, "request_id", "")}


@router.get("/{run_id}")
async def get_evaluation(
    run_id: UUID,
    request: Request,
    context: Annotated[AuthContext, Depends(get_current_identity)],
) -> dict[str, object]:
    _admin(context)
    directory = _run_dir(run_id)
    state = _read_status(directory)
    if state is None:
        raise HTTPException(status_code=404, detail="评测运行不存在")
    state["progress"] = _read_progress(directory, state)
    summary_path = directory / "summary.json"
    if summary_path.is_file():
        state["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
    cases_path = directory / "cases.jsonl"
    if cases_path.is_file():
        state["cases"] = [
            json.loads(line)
            for line in cases_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    return {"data": state, "request_id": getattr(request.state, "request_id", "")}


@router.get("/{run_id}/report/{filename}")
async def download_report(
    run_id: UUID,
    filename: Literal["summary.json", "cases.jsonl", "report.md"],
    context: Annotated[AuthContext, Depends(get_current_identity)],
) -> FileResponse:
    _admin(context)
    if filename not in _REPORT_FILES:
        raise HTTPException(status_code=404, detail="评测报告不存在")
    target = _run_dir(run_id) / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail="评测报告尚未生成")
    media_type = (
        "application/json"
        if filename.endswith(".json")
        else "application/x-ndjson"
        if filename.endswith(".jsonl")
        else "text/markdown"
    )
    return FileResponse(target, media_type=media_type, filename=filename)
