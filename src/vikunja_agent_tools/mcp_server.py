"""Claude Code など MCP クライアント向けの FastMCP サーバー。

各ツールは薄いオーケストレーション層として `TaskService` を呼び出す。
`VikunjaAPIError` / `TaskServiceError` はここで捕捉し、MCP クライアントに
例外を生で投げる代わりに JSON 形式のエラーペイロードを返す。
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

from fastmcp import FastMCP

from vikunja_agent_tools import status as status_logic
from vikunja_agent_tools.config import Settings, load_settings
from vikunja_agent_tools.models import AgentStatus, CreateTaskParams, TaskSummary
from vikunja_agent_tools.task_service import TaskService, TaskServiceError
from vikunja_agent_tools.vikunja_client import VikunjaAPIError, VikunjaClient

logger = logging.getLogger("vikunja_agent_tools.mcp_server")

mcp = FastMCP("vikunja-agent-tools")

_state: dict[str, Any] = {}

_F = TypeVar("_F", bound=Callable[..., Awaitable[dict[str, Any]]])


def configure(task_service: TaskService, settings: Settings) -> None:
    """`TaskService`/`Settings` を登録する。起動時 (`main()`) に一度だけ呼ぶ。"""
    _state["task_service"] = task_service
    _state["settings"] = settings


def _get_task_service() -> TaskService:
    try:
        return _state["task_service"]
    except KeyError:
        raise RuntimeError(
            "TaskService が初期化されていません。configure() を先に呼び出してください。"
        ) from None


def _get_settings() -> Settings:
    try:
        return _state["settings"]
    except KeyError:
        raise RuntimeError(
            "Settings が初期化されていません。configure() を先に呼び出してください。"
        ) from None


def _summary_dict(summary: TaskSummary) -> dict[str, Any]:
    return summary.model_dump(mode="json")


def _handle_errors(func: _F) -> _F:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await func(*args, **kwargs)
        except VikunjaAPIError as exc:
            logger.warning(
                "vikunja api error tool=%s kind=%s status_code=%s",
                func.__name__,
                exc.kind,
                exc.status_code,
            )
            return exc.to_error_payload()
        except TaskServiceError as exc:
            return {
                "error": True,
                "kind": "validation_error",
                "status_code": None,
                "message": str(exc),
            }

    return wrapper  # type: ignore[return-value]


@mcp.tool()
@_handle_errors
async def create_agent_task(
    title: str,
    description: str | None = None,
    project_id: int | None = None,
    priority: int | None = None,
    due_date: datetime | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """新しいエージェントタスクを Vikunja に作成し、状態を queued にする。"""
    task_service = _get_task_service()
    params = CreateTaskParams(
        title=title,
        description=description,
        project_id=project_id,
        priority=priority,
        due_date=due_date,
        agent_id=agent_id,
    )
    summary = await task_service.create_task(params)
    return _summary_dict(summary)


@mcp.tool()
@_handle_errors
async def list_agent_tasks(
    project_id: int | None = None,
    agent_id: str | None = None,
    status: AgentStatus | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    """エージェントタスクの一覧を取得する。project_id/agent_id/status で絞り込み可能。"""
    task_service = _get_task_service()
    summaries = await task_service.list_tasks(
        project_id=project_id,
        agent_id=agent_id,
        status_filter=status,
        max_items=max_items,
    )
    return {"tasks": [_summary_dict(s) for s in summaries], "count": len(summaries)}


@mcp.tool()
@_handle_errors
async def get_agent_task(task_id: int) -> dict[str, Any]:
    """タスクの詳細 (本文・エージェントメタ情報) を取得する。"""
    task_service = _get_task_service()
    detail = await task_service.get_task(task_id)
    return {
        "task": detail.task.model_dump(mode="json"),
        "meta": detail.meta.model_dump(mode="json") if detail.meta else None,
    }


@mcp.tool()
@_handle_errors
async def start_agent_task(
    task_id: int, agent_id: str | None = None, message: str | None = None
) -> dict[str, Any]:
    """タスクを running 状態にし、新しい execution_id を発行する。"""
    task_service = _get_task_service()
    summary = await task_service.set_task_status(
        task_id, AgentStatus.RUNNING, agent_id=agent_id, message=message
    )
    return _summary_dict(summary)


@mcp.tool()
@_handle_errors
async def report_agent_progress(
    task_id: int, message: str, agent_id: str | None = None
) -> dict[str, Any]:
    """running 状態のまま進捗メッセージをコメントとして記録する。"""
    task_service = _get_task_service()
    summary = await task_service.set_task_status(
        task_id, AgentStatus.RUNNING, agent_id=agent_id, message=message
    )
    return _summary_dict(summary)


@mcp.tool()
@_handle_errors
async def heartbeat_agent_task(
    task_id: int, agent_id: str | None = None, message: str | None = None
) -> dict[str, Any]:
    """生存報告。直近コメントから一定間隔が経つまではコメント投稿を抑制する。"""
    task_service = _get_task_service()
    settings = _get_settings()

    last_comment_at = await task_service.get_last_comment_at(task_id)
    now = datetime.now(timezone.utc)
    post_comment = status_logic.should_post_heartbeat_comment(
        last_comment_at, now, settings.vikunja_heartbeat_interval_seconds
    )

    summary = await task_service.set_task_status(
        task_id,
        AgentStatus.RUNNING,
        agent_id=agent_id,
        message=message,
        post_comment=post_comment,
    )
    return _summary_dict(summary)


@mcp.tool()
@_handle_errors
async def block_agent_task(
    task_id: int, message: str | None = None, agent_id: str | None = None
) -> dict[str, Any]:
    """外部要因等でタスクが進められない状態 (blocked) を記録する。"""
    task_service = _get_task_service()
    summary = await task_service.set_task_status(
        task_id, AgentStatus.BLOCKED, agent_id=agent_id, message=message
    )
    return _summary_dict(summary)


@mcp.tool()
@_handle_errors
async def request_agent_input(
    task_id: int, message: str | None = None, agent_id: str | None = None
) -> dict[str, Any]:
    """人間の判断/入力が必要な状態 (needs-input) を記録する。"""
    task_service = _get_task_service()
    summary = await task_service.set_task_status(
        task_id, AgentStatus.NEEDS_INPUT, agent_id=agent_id, message=message
    )
    return _summary_dict(summary)


@mcp.tool()
@_handle_errors
async def complete_agent_task(
    task_id: int, result_summary: str | None = None, agent_id: str | None = None
) -> dict[str, Any]:
    """タスクを完了状態にし、Vikunja 上でも完了 (done) にする。"""
    task_service = _get_task_service()
    summary = await task_service.complete_task(
        task_id, result_summary=result_summary, agent_id=agent_id
    )
    return _summary_dict(summary)


@mcp.tool()
@_handle_errors
async def fail_agent_task(
    task_id: int, message: str, agent_id: str | None = None
) -> dict[str, Any]:
    """タスクを失敗状態にする。Vikunja 上のタスクは完了にせず開いたままにする。"""
    task_service = _get_task_service()
    summary = await task_service.set_task_status(
        task_id, AgentStatus.FAILED, agent_id=agent_id, message=message
    )
    return _summary_dict(summary)


@mcp.tool()
@_handle_errors
async def reopen_agent_task(
    task_id: int, agent_id: str | None = None, message: str | None = None
) -> dict[str, Any]:
    """完了/失敗したタスクを queued 状態に戻す。"""
    task_service = _get_task_service()
    summary = await task_service.reopen_task(task_id, agent_id=agent_id, message=message)
    return _summary_dict(summary)


@mcp.tool()
@_handle_errors
async def get_agent_dashboard(
    project_id: int | None = None, stale_running_minutes: int | None = None
) -> dict[str, Any]:
    """状態別のタスク件数と、更新が止まっている running タスクの一覧を返す。"""
    task_service = _get_task_service()
    dashboard = await task_service.get_dashboard(
        project_id=project_id, stale_running_minutes=stale_running_minutes
    )
    return dashboard.model_dump(mode="json")


def main() -> None:
    settings = load_settings()
    client = VikunjaClient(
        settings.base_url,
        settings.vikunja_api_token.get_secret_value(),
        timeout_seconds=settings.vikunja_timeout_seconds,
        verify_tls=settings.vikunja_verify_tls,
    )
    configure(TaskService(client, settings), settings)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
