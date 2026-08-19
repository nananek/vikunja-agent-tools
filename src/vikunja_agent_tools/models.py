"""共通で使う Pydantic モデル群。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentStatus(StrEnum):
    """AI エージェントの作業状態。"""

    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs-input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VikunjaLabel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    hex_color: str | None = None


class VikunjaProject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str


class VikunjaTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    description: str | None = None
    done: bool = False
    priority: int | None = None
    start_date: datetime | None = None
    due_date: datetime | None = None
    project_id: int | None = None
    labels: list[VikunjaLabel] = Field(default_factory=list)
    created: datetime | None = None
    updated: datetime | None = None

    @field_validator("start_date", "due_date", mode="before")
    @classmethod
    def _normalize_zero_date(cls, value: object) -> object:
        """Vikunja (Go実装) は未設定の日付を null ではなく Go のゼロ値
        (`0001-01-01T00:00:00Z` 等、タイムゾーン表記違いを含む) で返すことがある。
        年が1以下の日時はすべて「未設定」として None に正規化する。"""
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return value
            if parsed.year <= 1:
                return None
        return value


class TaskComment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    comment: str
    created: datetime | None = None


class AgentTaskMeta(BaseModel):
    """タスク説明欄に埋め込まれる agent-meta ブロックのシリアライズ元。"""

    agent_id: str | None = None
    execution_id: str | None = None
    status: AgentStatus | None = None
    started_at: datetime | None = None
    last_update_at: datetime | None = None
    completed_at: datetime | None = None
    result_summary: str | None = None


class CreateTaskParams(BaseModel):
    """create_task 系サービス関数の入力バリデーション用モデル。"""

    title: str = Field(min_length=1)
    description: str | None = None
    project_id: int | None = None
    priority: int | None = None
    start_date: datetime | None = None
    due_date: datetime | None = None
    agent_id: str | None = None


class TaskSummary(BaseModel):
    """MCP / OpenWebUI ツールの戻り値に使う軽量表現。"""

    task_id: int
    title: str
    status: AgentStatus | None = None
    agent_id: str | None = None
    url: str | None = None
    priority: int | None = None
    start_date: datetime | None = None
    due_date: datetime | None = None
    done: bool = False


class DashboardResult(BaseModel):
    """get_agent_dashboard の戻り値。"""

    total: int
    by_status: dict[str, int]
    stale_running: list[TaskSummary] = Field(default_factory=list)
    tasks: list[TaskSummary] = Field(default_factory=list)


class ErrorPayload(BaseModel):
    """API エラーを MCP / OpenWebUI ツール層に伝える共通形状。"""

    error: bool = True
    kind: str
    status_code: int | None = None
    message: str
