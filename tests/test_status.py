from datetime import datetime, timedelta, timezone

from vikunja_agent_tools.models import AgentStatus, AgentTaskMeta, VikunjaLabel
from vikunja_agent_tools import status


def test_status_label_name_and_parse_roundtrip():
    name = status.status_label_name(AgentStatus.RUNNING, "status-")
    assert name == "status-running"
    assert status.parse_status_from_label(name, "status-") == AgentStatus.RUNNING


def test_parse_status_from_label_ignores_other_prefixes():
    assert status.parse_status_from_label("agent-claude", "status-") is None
    assert status.parse_status_from_label("status-unknown-value", "status-") is None


def test_diff_status_labels_adds_new_and_removes_old():
    current = [
        VikunjaLabel(id=1, title="status-queued"),
        VikunjaLabel(id=2, title="agent-claude"),
    ]
    to_add, to_remove = status.diff_status_labels(current, AgentStatus.RUNNING, "status-")
    assert to_add == ["status-running"]
    assert to_remove == [VikunjaLabel(id=1, title="status-queued")]


def test_diff_status_labels_is_idempotent_when_already_set():
    current = [
        VikunjaLabel(id=1, title="status-running"),
        VikunjaLabel(id=2, title="agent-claude"),
    ]
    to_add, to_remove = status.diff_status_labels(current, AgentStatus.RUNNING, "status-")
    assert to_add == []
    assert to_remove == []


def test_diff_status_labels_removes_multiple_stale_status_labels():
    current = [
        VikunjaLabel(id=1, title="status-queued"),
        VikunjaLabel(id=2, title="status-blocked"),
    ]
    to_add, to_remove = status.diff_status_labels(current, AgentStatus.RUNNING, "status-")
    assert to_add == ["status-running"]
    assert {label.id for label in to_remove} == {1, 2}


def test_diff_agent_labels_adds_new_and_removes_old():
    current = [
        VikunjaLabel(id=1, title="agent-research-01"),
        VikunjaLabel(id=2, title="status-running"),
    ]
    to_add, to_remove = status.diff_agent_labels(current, "coding-02", "agent-")
    assert to_add == ["agent-coding-02"]
    assert to_remove == [VikunjaLabel(id=1, title="agent-research-01")]


def test_diff_agent_labels_is_idempotent_when_already_set():
    current = [
        VikunjaLabel(id=1, title="agent-research-01"),
        VikunjaLabel(id=2, title="status-running"),
    ]
    to_add, to_remove = status.diff_agent_labels(current, "research-01", "agent-")
    assert to_add == []
    assert to_remove == []


def test_meta_block_roundtrip():
    meta = AgentTaskMeta(
        agent_id="claude-code",
        execution_id="exec-123",
        status=AgentStatus.RUNNING,
        started_at=datetime(2026, 8, 19, 3, 0, tzinfo=timezone.utc),
    )
    description = "ユーザーが書いた本文\n\n詳細説明"
    updated = status.upsert_meta_block(description, meta)

    assert updated.startswith("ユーザーが書いた本文")
    parsed = status.parse_meta_block(updated)
    assert parsed is not None
    assert parsed.agent_id == "claude-code"
    assert parsed.execution_id == "exec-123"
    assert parsed.status == AgentStatus.RUNNING


def test_upsert_meta_block_replaces_existing_block_and_keeps_body():
    meta1 = AgentTaskMeta(agent_id="a1", status=AgentStatus.QUEUED)
    meta2 = AgentTaskMeta(agent_id="a1", status=AgentStatus.RUNNING)

    description = "本文"
    with_first = status.upsert_meta_block(description, meta1)
    with_second = status.upsert_meta_block(with_first, meta2)

    assert with_second.startswith("本文")
    assert with_second.count("agent-meta:begin") == 1
    parsed = status.parse_meta_block(with_second)
    assert parsed.status == AgentStatus.RUNNING


def test_upsert_meta_block_on_empty_description():
    meta = AgentTaskMeta(agent_id="a1", status=AgentStatus.QUEUED)
    updated = status.upsert_meta_block(None, meta)
    parsed = status.parse_meta_block(updated)
    assert parsed.agent_id == "a1"


def test_parse_meta_block_returns_none_when_absent():
    assert status.parse_meta_block("普通の説明文") is None
    assert status.parse_meta_block(None) is None
    assert status.parse_meta_block("") is None


def test_render_status_comment_contains_key_fields():
    now = datetime(2026, 8, 19, 4, 30, tzinfo=timezone.utc)
    comment = status.render_status_comment(
        AgentStatus.FAILED,
        agent_id="claude-code",
        execution_id="exec-1",
        message="テストが失敗しました",
        now=now,
    )
    assert comment.startswith("[agent-status]")
    assert "status: failed" in comment
    assert "agent_id: claude-code" in comment
    assert "execution_id: exec-1" in comment
    assert "テストが失敗しました" in comment
    assert "2026-08-19T04:30:00+00:00" in comment


def test_should_post_heartbeat_comment():
    now = datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)
    assert status.should_post_heartbeat_comment(None, now, 900) is True

    recent = now - timedelta(seconds=100)
    assert status.should_post_heartbeat_comment(recent, now, 900) is False

    old = now - timedelta(seconds=1000)
    assert status.should_post_heartbeat_comment(old, now, 900) is True


def test_is_stale_running():
    now = datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)
    assert status.is_stale_running(None, now, 30) is True

    recent = now - timedelta(minutes=5)
    assert status.is_stale_running(recent, now, 30) is False

    old = now - timedelta(minutes=45)
    assert status.is_stale_running(old, now, 30) is True
