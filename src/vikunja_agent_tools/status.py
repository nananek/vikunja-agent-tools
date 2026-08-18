"""状態ラベル・メタブロック・ハートビート判定などの純粋ロジック (I/O なし)。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime, timezone

from vikunja_agent_tools.models import AgentStatus, AgentTaskMeta, VikunjaLabel

_META_BEGIN = "<!-- agent-meta:begin -->"
_META_END = "<!-- agent-meta:end -->"
_META_BLOCK_RE = re.compile(
    re.escape(_META_BEGIN) + r".*?" + re.escape(_META_END), re.DOTALL
)
_META_JSON_RE = re.compile(
    re.escape(_META_BEGIN) + r"\s*```json\s*(.*?)\s*```\s*" + re.escape(_META_END),
    re.DOTALL,
)


def status_label_name(status: AgentStatus, prefix: str) -> str:
    """状態値からラベル名 (例: ``status-running``) を作る。"""
    return f"{prefix}{status.value}"


def parse_status_from_label(label_title: str, prefix: str) -> AgentStatus | None:
    """ラベル名から状態値を復元する。対象外のラベルなら ``None``。"""
    if not label_title.startswith(prefix):
        return None
    value = label_title[len(prefix) :]
    try:
        return AgentStatus(value)
    except ValueError:
        return None


def diff_status_labels(
    current_labels: Sequence[VikunjaLabel], new_status: AgentStatus, prefix: str
) -> tuple[list[str], list[VikunjaLabel]]:
    """状態ラベルの追加/削除差分を計算する。

    既存の ``{prefix}*`` ラベルは (新しい状態と同名のものを除いて) すべて削除対象にし、
    新しい状態のラベルがまだ付いていなければ追加対象にする。同じ状態への変更は
    冪等 (to_add が空になる) に扱う。
    """
    new_title = status_label_name(new_status, prefix)
    status_labels = [label for label in current_labels if label.title.startswith(prefix)]
    already_present = any(label.title == new_title for label in status_labels)
    to_remove = [label for label in status_labels if label.title != new_title]
    to_add = [] if already_present else [new_title]
    return to_add, to_remove


def diff_agent_labels(
    current_labels: Sequence[VikunjaLabel], new_agent_id: str, prefix: str
) -> tuple[list[str], list[VikunjaLabel]]:
    """``agent-*`` ラベルの追加/削除差分を計算する (状態ラベルと同じロジック)。

    既存の ``{prefix}*`` ラベルは (新しい agent_id と同名のものを除いて) すべて削除対象にし、
    新しい agent_id のラベルがまだ付いていなければ追加対象にする。担当エージェントの
    付け替え時に古いラベルが残留しないようにするため、常に1つに絞り込む。
    """
    new_title = f"{prefix}{new_agent_id}"
    agent_labels = [label for label in current_labels if label.title.startswith(prefix)]
    already_present = any(label.title == new_title for label in agent_labels)
    to_remove = [label for label in agent_labels if label.title != new_title]
    to_add = [] if already_present else [new_title]
    return to_add, to_remove


def render_meta_block(meta: AgentTaskMeta) -> str:
    """`AgentTaskMeta` を HTML コメントで区切った JSON ブロックに直列化する。"""
    payload = json.dumps(
        meta.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
    )
    return f"{_META_BEGIN}\n```json\n{payload}\n```\n{_META_END}"


def upsert_meta_block(description: str | None, meta: AgentTaskMeta) -> str:
    """タスク説明内の meta ブロックを更新する。既存のブロックがあれば置換し、
    なければ末尾に追加する。ユーザーが書いた本文は保持する。"""
    block = render_meta_block(meta)
    description = description or ""
    if _META_BLOCK_RE.search(description):
        return _META_BLOCK_RE.sub(block, description)
    if description.strip():
        return f"{description.rstrip()}\n\n{block}"
    return block


def parse_meta_block(description: str | None) -> AgentTaskMeta | None:
    """タスク説明から meta ブロックを取り出す。存在しない/壊れていれば ``None``。"""
    if not description:
        return None
    match = _META_JSON_RE.search(description)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    try:
        return AgentTaskMeta.model_validate(data)
    except Exception:
        return None


def render_status_comment(
    status: AgentStatus,
    *,
    agent_id: str | None = None,
    execution_id: str | None = None,
    message: str | None = None,
    now: datetime | None = None,
) -> str:
    """``[agent-status]`` ブロック形式の状態変更コメントを生成する。"""
    now = now or datetime.now(timezone.utc)
    lines = [
        "[agent-status]",
        f"status: {status.value}",
        f"timestamp: {now.isoformat()}",
    ]
    if agent_id:
        lines.append(f"agent_id: {agent_id}")
    if execution_id:
        lines.append(f"execution_id: {execution_id}")
    if message:
        lines.append("")
        lines.append(message)
    return "\n".join(lines)


def should_post_heartbeat_comment(
    last_comment_at: datetime | None, now: datetime, interval_seconds: int
) -> bool:
    """前回のハートビートコメントから ``interval_seconds`` 以上経過していれば ``True``。"""
    if last_comment_at is None:
        return True
    elapsed = (now - last_comment_at).total_seconds()
    return elapsed >= interval_seconds


def is_stale_running(
    last_update_at: datetime | None, now: datetime, threshold_minutes: int
) -> bool:
    """``running`` 状態のタスクが ``threshold_minutes`` 以上更新されていなければ ``True``。

    最終更新時刻が不明な場合も、生存確認ができないため stale として扱う。
    """
    if last_update_at is None:
        return True
    elapsed_minutes = (now - last_update_at).total_seconds() / 60
    return elapsed_minutes >= threshold_minutes
