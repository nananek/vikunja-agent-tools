"""Open WebUI の Python Tool として読み込む、エージェントタスク管理ツール。

Open WebUI の管理画面にこのファイルの内容をそのまま貼り付けて使う。設定は
(Valves ではなく) `config.load_settings()` を通じて環境変数 / `.env` から
読み込む。各メソッドは例外を投げず、失敗時も日本語のエラーメッセージ文字列
を返す。
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TypeVar

from vikunja_agent_tools.config import load_settings
from vikunja_agent_tools.models import AgentStatus, CreateTaskParams, DashboardResult, TaskSummary
from vikunja_agent_tools.task_service import TaskDetail, TaskService, TaskServiceError
from vikunja_agent_tools.vikunja_client import VikunjaAPIError, VikunjaClient

_F = TypeVar("_F", bound=Callable[..., Awaitable[str]])


def _parse_due_date(due_date: str) -> datetime | None:
    if not due_date:
        return None
    return datetime.fromisoformat(due_date)


def _format_error(exc: Exception) -> str:
    if isinstance(exc, VikunjaAPIError):
        payload = exc.to_error_payload()
        status_part = f", status={payload['status_code']}" if payload["status_code"] else ""
        return f"エラーが発生しました ({payload['kind']}{status_part}): {payload['message']}"
    if isinstance(exc, TaskServiceError):
        return f"入力エラー: {exc}"
    return f"入力エラー: {exc}"


def _handle_errors(func: _F) -> _F:
    @functools.wraps(func)
    async def wrapper(self: "Tools", *args: object, **kwargs: object) -> str:
        try:
            return await func(self, *args, **kwargs)
        except (VikunjaAPIError, TaskServiceError, ValueError) as exc:
            return _format_error(exc)

    return wrapper  # type: ignore[return-value]


def _format_summary(summary: TaskSummary) -> str:
    lines = [
        f"タスク #{summary.task_id}: {summary.title}",
        f"状態: {summary.status.value if summary.status else '(不明)'}",
        f"担当エージェント: {summary.agent_id or '-'}",
        f"優先度: {summary.priority if summary.priority is not None else '-'}",
        f"進捗率: {summary.percent_done * 100:.1f}%" if summary.percent_done is not None else "進捗率: -",
        f"開始日: {summary.start_date.isoformat() if summary.start_date else '-'}",
        f"期限: {summary.due_date.isoformat() if summary.due_date else '-'}",
        f"完了: {'はい' if summary.done else 'いいえ'}",
        f"URL: {summary.url or '-'}",
    ]
    return "\n".join(lines)


def _format_detail(detail: TaskDetail) -> str:
    lines = [
        f"タスク #{detail.task.id}: {detail.task.title}",
        f"完了: {'はい' if detail.task.done else 'いいえ'}",
        f"進捗率: {detail.task.percent_done * 100:.1f}%" if detail.task.percent_done is not None else "進捗率: -",
        f"開始日: {detail.task.start_date.isoformat() if detail.task.start_date else '-'}",
        f"期限: {detail.task.due_date.isoformat() if detail.task.due_date else '-'}",
    ]
    if detail.meta:
        lines.append(f"状態: {detail.meta.status.value if detail.meta.status else '(不明)'}")
        lines.append(f"担当エージェント: {detail.meta.agent_id or '-'}")
        lines.append(f"実行ID: {detail.meta.execution_id or '-'}")
        if detail.meta.result_summary:
            lines.append(f"結果サマリ: {detail.meta.result_summary}")
    if detail.task.description:
        lines.append("--- 説明 ---")
        lines.append(detail.task.description)
    return "\n".join(lines)


def _format_dashboard(dashboard: DashboardResult) -> str:
    lines = [f"合計タスク数: {dashboard.total}"]
    if dashboard.by_status:
        lines.append("状態別件数:")
        for status_value, count in sorted(dashboard.by_status.items()):
            lines.append(f"  - {status_value}: {count}")
    else:
        lines.append("状態別件数: なし")

    if dashboard.stale_running:
        lines.append("停滞している running タスク:")
        for summary in dashboard.stale_running:
            lines.append(f"  - #{summary.task_id} {summary.title} ({summary.url or '-'})")
    else:
        lines.append("停滞している running タスクはありません。")
    return "\n".join(lines)


class Tools:
    def __init__(self) -> None:
        settings = load_settings()
        client = VikunjaClient(
            settings.base_url,
            settings.vikunja_api_token.get_secret_value(),
            timeout_seconds=settings.vikunja_timeout_seconds,
            verify_tls=settings.vikunja_verify_tls,
        )
        self._task_service = TaskService(client, settings)

    @_handle_errors
    async def create_task(
        self,
        title: str,
        description: str = "",
        project_id: int | None = None,
        priority: int | None = None,
        percent_done: float | None = None,
        start_date: str = "",
        due_date: str = "",
        agent_id: str = "",
    ) -> str:
        """新しいエージェントタスクを Vikunja に作成する。

        :param title: タスクのタイトル (必須)
        :param description: タスクの説明文 (省略可)
        :param project_id: 作成先のプロジェクト ID。省略時は環境変数の既定値を使う
        :param priority: 優先度 (Vikunja の優先度値。省略可)
        :param percent_done: 進捗率 (0.0〜1.0。14%なら0.14。省略可)
        :param start_date: 開始日時 (ISO8601形式の文字列。省略可)
        :param due_date: 期限日時 (ISO8601形式の文字列。例: 2026-08-20T18:00:00+09:00)
        :param agent_id: このタスクを担当するエージェントの識別子 (省略可)
        :return: 作成結果を日本語でまとめたテキスト
        """
        params = CreateTaskParams(
            title=title,
            description=description or None,
            project_id=project_id,
            priority=priority,
            percent_done=percent_done,
            start_date=_parse_due_date(start_date),
            due_date=_parse_due_date(due_date),
            agent_id=agent_id or None,
        )
        summary = await self._task_service.create_task(params)
        return f"タスクを作成しました。\n{_format_summary(summary)}"

    @_handle_errors
    async def update_task(
        self,
        task_id: int,
        title: str = "",
        description: str = "",
        priority: int | None = None,
        percent_done: float | None = None,
        start_date: str = "",
        due_date: str = "",
    ) -> str:
        """既存タスクの内容、優先度、開始日、期限を更新する。"""
        summary = await self._task_service.update_task(
            task_id,
            title=title or None,
            description=description or None,
            priority=priority,
            percent_done=percent_done,
            start_date=_parse_due_date(start_date),
            due_date=_parse_due_date(due_date),
        )
        return f"タスクを更新しました。\n{_format_summary(summary)}"

    @_handle_errors
    async def list_tasks(
        self,
        project_id: int | None = None,
        agent_id: str = "",
        status: str = "",
        max_items: int = 20,
    ) -> str:
        """エージェントタスクの一覧を取得する。

        :param project_id: 絞り込むプロジェクト ID (省略可)
        :param agent_id: 絞り込む担当エージェントの識別子 (省略可)
        :param status: 絞り込む状態 (queued/running/blocked/needs-input/completed/failed/cancelled)
        :param max_items: 取得する最大件数 (既定: 20)
        :return: タスク一覧を日本語でまとめたテキスト
        """
        status_filter = AgentStatus(status) if status else None
        summaries = await self._task_service.list_tasks(
            project_id=project_id,
            agent_id=agent_id or None,
            status_filter=status_filter,
            max_items=max_items,
        )
        if not summaries:
            return "該当するタスクはありません。"
        return "\n\n".join(_format_summary(summary) for summary in summaries)

    @_handle_errors
    async def list_projects(self) -> str:
        """利用可能なVikunjaプロジェクトのIDと名称を一覧表示する。"""
        projects = await self._task_service.list_projects()
        if not projects:
            return "利用可能なプロジェクトはありません。"
        return "\n".join(f"#{project.id}: {project.title}" for project in projects)

    @_handle_errors
    async def show_task(self, task_id: int) -> str:
        """タスクの詳細 (説明文・状態・担当エージェント) を表示する。

        :param task_id: 表示するタスクの ID
        :return: タスク詳細を日本語でまとめたテキスト
        """
        detail = await self._task_service.get_task(task_id)
        return _format_detail(detail)

    @_handle_errors
    async def start_task(self, task_id: int, agent_id: str = "", message: str = "") -> str:
        """タスクを running 状態にし、作業開始を記録する。

        :param task_id: 開始するタスクの ID
        :param agent_id: 作業を開始するエージェントの識別子 (省略可)
        :param message: 開始時のメモ (省略可)
        :return: 実行結果を日本語でまとめたテキスト
        """
        summary = await self._task_service.set_task_status(
            task_id, AgentStatus.RUNNING, agent_id=agent_id or None, message=message or None
        )
        return f"タスクを開始しました。\n{_format_summary(summary)}"

    @_handle_errors
    async def update_progress(self, task_id: int, message: str, agent_id: str = "") -> str:
        """running 状態のタスクに進捗メッセージを記録する。

        :param task_id: 対象タスクの ID
        :param message: 進捗メッセージ (必須)
        :param agent_id: 報告するエージェントの識別子 (省略可)
        :return: 実行結果を日本語でまとめたテキスト
        """
        summary = await self._task_service.set_task_status(
            task_id, AgentStatus.RUNNING, agent_id=agent_id or None, message=message
        )
        return f"進捗を記録しました。\n{_format_summary(summary)}"

    @_handle_errors
    async def complete_task(
        self, task_id: int, result_summary: str = "", agent_id: str = ""
    ) -> str:
        """タスクを完了状態にし、Vikunja 上でも完了にする。

        :param task_id: 完了するタスクの ID
        :param result_summary: 完了時の結果サマリ (省略可)
        :param agent_id: 完了させるエージェントの識別子 (省略可)
        :return: 実行結果を日本語でまとめたテキスト
        """
        summary = await self._task_service.complete_task(
            task_id, result_summary=result_summary or None, agent_id=agent_id or None
        )
        return f"タスクを完了しました。\n{_format_summary(summary)}"

    @_handle_errors
    async def fail_task(self, task_id: int, message: str, agent_id: str = "") -> str:
        """タスクを失敗状態にする。Vikunja 上のタスクは完了にせず開いたままにする。

        :param task_id: 対象タスクの ID
        :param message: 失敗理由 (必須)
        :param agent_id: 報告するエージェントの識別子 (省略可)
        :return: 実行結果を日本語でまとめたテキスト
        """
        summary = await self._task_service.set_task_status(
            task_id, AgentStatus.FAILED, agent_id=agent_id or None, message=message
        )
        return f"タスクを失敗として記録しました。\n{_format_summary(summary)}"

    @_handle_errors
    async def request_input(self, task_id: int, message: str = "", agent_id: str = "") -> str:
        """人間の判断/入力が必要な状態 (needs-input) を記録する。

        :param task_id: 対象タスクの ID
        :param message: 何について確認が必要かのメモ (省略可)
        :param agent_id: 報告するエージェントの識別子 (省略可)
        :return: 実行結果を日本語でまとめたテキスト
        """
        summary = await self._task_service.set_task_status(
            task_id,
            AgentStatus.NEEDS_INPUT,
            agent_id=agent_id or None,
            message=message or None,
        )
        return f"人間の入力待ちとして記録しました。\n{_format_summary(summary)}"

    @_handle_errors
    async def agent_dashboard(self, project_id: int | None = None) -> str:
        """状態別のタスク件数と、更新が止まっている running タスクを表示する。

        :param project_id: 絞り込むプロジェクト ID (省略可)
        :return: ダッシュボードを日本語でまとめたテキスト
        """
        dashboard = await self._task_service.get_dashboard(project_id=project_id)
        return _format_dashboard(dashboard)
