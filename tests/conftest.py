"""共有テストフィクスチャ。外部 Vikunja サーバーには一切依存しない。"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from vikunja_agent_tools.config import Settings
from vikunja_agent_tools.models import TaskComment, VikunjaLabel, VikunjaProject, VikunjaTask
from vikunja_agent_tools.task_service import TaskService
from vikunja_agent_tools.vikunja_client import VikunjaNotFoundError


class FakeVikunjaClient:
    """インメモリの Vikunja クライアント代替。

    `unittest.mock.AsyncMock` ではなく、タスク/ラベル/コメントを保持する簡易
    ストアとして実装することで、read-modify-write のバグ (ラベル差分の
    取りこぼし等) をテストで検出しやすくする。
    """

    def __init__(self) -> None:
        self._tasks: dict[int, dict] = {}
        self._task_labels: dict[int, list[int]] = {}
        self._labels: dict[int, VikunjaLabel] = {}
        self._comments: dict[int, list[TaskComment]] = {}
        self._task_ids = itertools.count(1)
        self._label_ids = itertools.count(1)
        self._comment_ids = itertools.count(1)

    def _build_task(self, task_id: int) -> VikunjaTask:
        raw = self._tasks[task_id]
        label_ids = self._task_labels.get(task_id, [])
        labels = [self._labels[lid] for lid in label_ids if lid in self._labels]
        return VikunjaTask(**raw, labels=labels)

    async def create_task(
        self, project_id: int, title: str, description: str | None = None, **fields: object
    ) -> VikunjaTask:
        task_id = next(self._task_ids)
        now = datetime.now(timezone.utc)
        self._tasks[task_id] = {
            "id": task_id,
            "title": title,
            "description": description,
            "done": False,
            "project_id": project_id,
            "created": now,
            "updated": now,
            **fields,
        }
        self._task_labels[task_id] = []
        return self._build_task(task_id)

    async def get_task(self, task_id: int) -> VikunjaTask:
        if task_id not in self._tasks:
            raise VikunjaNotFoundError(
                f"タスクが見つかりません: {task_id}", status_code=404
            )
        return self._build_task(task_id)

    async def update_task(self, task_id: int, **fields: object) -> VikunjaTask:
        if task_id not in self._tasks:
            raise VikunjaNotFoundError(
                f"タスクが見つかりません: {task_id}", status_code=404
            )
        self._tasks[task_id].update(fields)
        self._tasks[task_id]["updated"] = datetime.now(timezone.utc)
        return self._build_task(task_id)

    async def list_tasks(
        self, project_id: int | None = None, *, max_items: int | None = None
    ) -> list[VikunjaTask]:
        items = [
            self._build_task(task_id)
            for task_id, raw in self._tasks.items()
            if project_id is None or raw.get("project_id") == project_id
        ]
        items.sort(key=lambda task: task.id)
        if max_items is not None:
            items = items[:max_items]
        return items

    async def list_projects(self) -> list[VikunjaProject]:
        return [VikunjaProject(id=1, title="資格学習")]

    async def list_comments(self, task_id: int) -> list[TaskComment]:
        return list(self._comments.get(task_id, []))

    async def add_comment(self, task_id: int, comment: str) -> TaskComment:
        comment_id = next(self._comment_ids)
        obj = TaskComment(
            id=comment_id, comment=comment, created=datetime.now(timezone.utc)
        )
        self._comments.setdefault(task_id, []).append(obj)
        return obj

    async def list_labels(self) -> list[VikunjaLabel]:
        return list(self._labels.values())

    async def create_label(
        self, title: str, hex_color: str | None = None
    ) -> VikunjaLabel:
        label_id = next(self._label_ids)
        label = VikunjaLabel(id=label_id, title=title, hex_color=hex_color)
        self._labels[label_id] = label
        return label

    async def get_or_create_label(self, title: str) -> VikunjaLabel:
        for label in self._labels.values():
            if label.title == title:
                return label
        return await self.create_label(title)

    async def add_label_to_task(self, task_id: int, label_id: int) -> None:
        labels = self._task_labels.setdefault(task_id, [])
        if label_id not in labels:
            labels.append(label_id)

    async def remove_label_from_task(self, task_id: int, label_id: int) -> None:
        labels = self._task_labels.get(task_id, [])
        if label_id in labels:
            labels.remove(label_id)


@pytest.fixture
def fake_client() -> FakeVikunjaClient:
    return FakeVikunjaClient()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        vikunja_base_url="https://vikunja.example.com",
        vikunja_api_token="test-token",
        vikunja_project_id="1",
    )


@pytest.fixture
def task_service(fake_client: FakeVikunjaClient, settings: Settings) -> TaskService:
    return TaskService(fake_client, settings)
