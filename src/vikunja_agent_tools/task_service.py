"""Vikunja タスクを介した AI エージェント状態管理のサービス層。

MCP ツール層 / OpenWebUI ツール層の双方から共有される。Vikunja API のエラーは
ここで握りつぶさずそのまま呼び出し元に伝播させ、エラーの JSON 化は呼び出し元
(ツール層) の責務とする。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from vikunja_agent_tools import status as status_logic
from vikunja_agent_tools.config import Settings
from vikunja_agent_tools.models import (
    AgentStatus,
    AgentTaskMeta,
    CreateTaskParams,
    DashboardResult,
    TaskComment,
    TaskSummary,
    VikunjaLabel,
    VikunjaProject,
    VikunjaTask,
)
from vikunja_agent_tools.vikunja_client import VikunjaClient


class TaskServiceError(Exception):
    """サービス層で完結する入力バリデーションエラー。"""


@dataclass
class TaskDetail:
    task: VikunjaTask
    meta: AgentTaskMeta | None


_TERMINAL_STATUSES = (AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED)
# RUNNING への遷移が「新しい実行 (fresh start)」とみなされ、新規 execution_id が発行される
# 直前の状態。blocked/needs-input からの再開は同一実行の継続とみなし、ここには含めない。
_FRESH_START_STATUSES = (AgentStatus.QUEUED, *_TERMINAL_STATUSES)


class TaskService:
    def __init__(self, client: VikunjaClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    # -- 内部ヘルパー -----------------------------------------------------

    def _task_url(self, task_id: int) -> str:
        return f"{self._settings.base_url}/tasks/{task_id}"

    def _resolve_project_id(self, project_id: int | None) -> int | None:
        if project_id is not None:
            return project_id
        fallback = self._settings.vikunja_project_id
        return int(fallback) if fallback else None

    def _to_summary(self, task: VikunjaTask, meta: AgentTaskMeta | None) -> TaskSummary:
        return TaskSummary(
            task_id=task.id,
            title=task.title,
            status=meta.status if meta else None,
            agent_id=meta.agent_id if meta else None,
            url=self._task_url(task.id),
            priority=task.priority,
            start_date=task.start_date,
            due_date=task.due_date,
            done=task.done,
        )

    def _resolve_execution_id(
        self,
        current_meta: AgentTaskMeta | None,
        new_status: AgentStatus,
        execution_id: str | None,
    ) -> str | None:
        if execution_id is not None:
            return execution_id
        if new_status == AgentStatus.RUNNING:
            is_fresh_start = (
                current_meta is None
                or not current_meta.execution_id
                or current_meta.status is None
                or current_meta.status in _FRESH_START_STATUSES
            )
            if is_fresh_start:
                return uuid.uuid4().hex
            return current_meta.execution_id
        return current_meta.execution_id if current_meta else None

    async def _apply_label_diff(
        self, task_id: int, to_add: list[str], to_remove: list[VikunjaLabel]
    ) -> None:
        for label in to_remove:
            await self._client.remove_label_from_task(task_id, label.id)
        for title in to_add:
            label = await self._client.get_or_create_label(title)
            await self._client.add_label_to_task(task_id, label.id)

    async def _apply_labels_and_meta(
        self, task: VikunjaTask, meta: AgentTaskMeta, *, agent_id: str | None
    ) -> VikunjaTask:
        status_add, status_remove = status_logic.diff_status_labels(
            task.labels, meta.status, self._settings.vikunja_status_label_prefix
        )
        await self._apply_label_diff(task.id, status_add, status_remove)

        if agent_id is not None:
            agent_add, agent_remove = status_logic.diff_agent_labels(
                task.labels, agent_id, self._settings.vikunja_agent_label_prefix
            )
            await self._apply_label_diff(task.id, agent_add, agent_remove)

        new_description = status_logic.upsert_meta_block(task.description, meta)
        return await self._client.update_task(task.id, description=new_description)

    # -- 公開 API -----------------------------------------------------

    async def create_task(self, params: CreateTaskParams) -> TaskSummary:
        project_id = self._resolve_project_id(params.project_id)
        if project_id is None:
            raise TaskServiceError(
                "project_id が指定されておらず、VIKUNJA_PROJECT_ID も未設定です。"
            )

        extra_fields: dict[str, object] = {}
        if params.priority is not None:
            extra_fields["priority"] = params.priority
        if params.start_date is not None:
            extra_fields["start_date"] = params.start_date.isoformat()
        if params.due_date is not None:
            extra_fields["due_date"] = params.due_date.isoformat()

        task = await self._client.create_task(
            project_id, params.title, description=params.description, **extra_fields
        )

        meta = AgentTaskMeta(
            agent_id=params.agent_id,
            status=AgentStatus.QUEUED,
            last_update_at=datetime.now(timezone.utc),
        )
        task = await self._apply_labels_and_meta(task, meta, agent_id=params.agent_id)
        return self._to_summary(task, meta)

    async def list_tasks(
        self,
        *,
        project_id: int | None = None,
        agent_id: str | None = None,
        status_filter: AgentStatus | None = None,
        max_items: int | None = None,
    ) -> list[TaskSummary]:
        effective_project_id = self._resolve_project_id(project_id)
        has_filter = agent_id is not None or status_filter is not None
        # agent_id/status はタスク取得後にクライアント側でフィルタするため、フィルタがある場合に
        # 生の取得件数を max_items で絞ると、フィルタ後の該当タスクを取りこぼす。フィルタがある
        # ときは全件取得したうえでフィルタ後の件数を max_items で絞る。
        fetch_limit = None if has_filter else max_items
        tasks = await self._client.list_tasks(effective_project_id, max_items=fetch_limit)

        summaries: list[TaskSummary] = []
        for task in tasks:
            meta = status_logic.parse_meta_block(task.description)
            if agent_id is not None and (meta is None or meta.agent_id != agent_id):
                continue
            if status_filter is not None and (meta is None or meta.status != status_filter):
                continue
            summaries.append(self._to_summary(task, meta))
            if has_filter and max_items is not None and len(summaries) >= max_items:
                break
        return summaries

    async def list_projects(self) -> list[VikunjaProject]:
        """利用可能なプロジェクトをIDと名称付きで取得する。"""
        return await self._client.list_projects()

    async def get_task(self, task_id: int) -> TaskDetail:
        task = await self._client.get_task(task_id)
        meta = status_logic.parse_meta_block(task.description)
        return TaskDetail(task=task, meta=meta)

    async def get_last_comment_at(self, task_id: int) -> datetime | None:
        comments = await self._client.list_comments(task_id)
        timestamps = [c.created for c in comments if c.created is not None]
        return max(timestamps) if timestamps else None

    async def update_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        priority: int | None = None,
        start_date: datetime | None = None,
        due_date: datetime | None = None,
    ) -> TaskSummary:
        fields: dict[str, object] = {}
        if title is not None:
            fields["title"] = title
        if priority is not None:
            fields["priority"] = priority
        if start_date is not None:
            fields["start_date"] = start_date.isoformat()
        if due_date is not None:
            fields["due_date"] = due_date.isoformat()

        if description is not None:
            current = await self._client.get_task(task_id)
            meta = status_logic.parse_meta_block(current.description)
            fields["description"] = (
                status_logic.upsert_meta_block(description, meta) if meta else description
            )

        if fields:
            task = await self._client.update_task(task_id, **fields)
        else:
            task = await self._client.get_task(task_id)

        meta = status_logic.parse_meta_block(task.description)
        return self._to_summary(task, meta)

    async def set_task_status(
        self,
        task_id: int,
        new_status: AgentStatus,
        *,
        agent_id: str | None = None,
        execution_id: str | None = None,
        message: str | None = None,
        post_comment: bool = True,
    ) -> TaskSummary:
        task = await self._client.get_task(task_id)
        current_meta = status_logic.parse_meta_block(task.description)

        now = datetime.now(timezone.utc)
        resolved_agent_id = agent_id or (current_meta.agent_id if current_meta else None)
        resolved_execution_id = self._resolve_execution_id(
            current_meta, new_status, execution_id
        )

        is_fresh_run = current_meta is None or resolved_execution_id != current_meta.execution_id
        if new_status == AgentStatus.RUNNING:
            started_at = now if is_fresh_run else current_meta.started_at or now
        else:
            started_at = current_meta.started_at if current_meta else None

        if new_status in _TERMINAL_STATUSES:
            completed_at = now
        elif new_status in (AgentStatus.QUEUED, AgentStatus.RUNNING):
            completed_at = None
        else:
            completed_at = current_meta.completed_at if current_meta else None

        if new_status in (AgentStatus.COMPLETED, AgentStatus.FAILED) and message:
            result_summary = message
        else:
            result_summary = current_meta.result_summary if current_meta else None

        new_meta = AgentTaskMeta(
            agent_id=resolved_agent_id,
            execution_id=resolved_execution_id,
            status=new_status,
            started_at=started_at,
            last_update_at=now,
            completed_at=completed_at,
            result_summary=result_summary,
        )

        task = await self._apply_labels_and_meta(task, new_meta, agent_id=resolved_agent_id)

        if post_comment:
            comment_text = status_logic.render_status_comment(
                new_status,
                agent_id=resolved_agent_id,
                execution_id=resolved_execution_id,
                message=message,
                now=now,
            )
            await self._client.add_comment(task_id, comment_text)

        if new_status == AgentStatus.COMPLETED and not task.done:
            task = await self._client.update_task(task_id, done=True)
        elif new_status != AgentStatus.COMPLETED and task.done:
            task = await self._client.update_task(task_id, done=False)

        return self._to_summary(task, new_meta)

    async def add_task_comment(self, task_id: int, comment: str) -> TaskComment:
        return await self._client.add_comment(task_id, comment)

    async def complete_task(
        self,
        task_id: int,
        *,
        result_summary: str | None = None,
        agent_id: str | None = None,
    ) -> TaskSummary:
        return await self.set_task_status(
            task_id, AgentStatus.COMPLETED, agent_id=agent_id, message=result_summary
        )

    async def reopen_task(
        self,
        task_id: int,
        *,
        agent_id: str | None = None,
        message: str | None = None,
    ) -> TaskSummary:
        return await self.set_task_status(
            task_id, AgentStatus.QUEUED, agent_id=agent_id, message=message
        )

    async def get_dashboard(
        self,
        *,
        project_id: int | None = None,
        stale_running_minutes: int | None = None,
    ) -> DashboardResult:
        threshold = (
            stale_running_minutes
            if stale_running_minutes is not None
            else self._settings.vikunja_stale_running_minutes
        )
        effective_project_id = self._resolve_project_id(project_id)
        tasks = await self._client.list_tasks(effective_project_id)

        now = datetime.now(timezone.utc)
        by_status: dict[str, int] = {}
        stale_running: list[TaskSummary] = []
        summaries: list[TaskSummary] = []

        for task in tasks:
            meta = status_logic.parse_meta_block(task.description)
            if meta is None or meta.status is None:
                continue
            by_status[meta.status.value] = by_status.get(meta.status.value, 0) + 1
            summary = self._to_summary(task, meta)
            summaries.append(summary)
            if meta.status == AgentStatus.RUNNING and status_logic.is_stale_running(
                meta.last_update_at, now, threshold
            ):
                stale_running.append(summary)

        return DashboardResult(
            total=len(summaries),
            by_status=by_status,
            stale_running=stale_running,
            tasks=summaries,
        )
