from datetime import datetime, timedelta, timezone

import pytest

from vikunja_agent_tools import status as status_logic
from vikunja_agent_tools.config import Settings
from vikunja_agent_tools.models import AgentStatus, CreateTaskParams
from vikunja_agent_tools.task_service import TaskService, TaskServiceError


async def test_create_task_sets_queued_status_and_agent_label(task_service, fake_client):
    summary = await task_service.create_task(
        CreateTaskParams(title="掃除する", agent_id="claude-code")
    )

    assert summary.status == AgentStatus.QUEUED
    assert summary.agent_id == "claude-code"

    detail = await task_service.get_task(summary.task_id)
    label_titles = {label.title for label in detail.task.labels}
    assert label_titles == {"status-queued", "agent-claude-code"}
    assert detail.meta is not None
    assert detail.meta.status == AgentStatus.QUEUED


async def test_create_and_update_task_dates(task_service):
    start_date = datetime(2026, 8, 20, 9, tzinfo=timezone.utc)
    due_date = datetime(2026, 8, 21, 18, tzinfo=timezone.utc)
    created = await task_service.create_task(
        CreateTaskParams(title="日付付き", start_date=start_date, due_date=due_date)
    )

    detail = await task_service.get_task(created.task_id)
    assert detail.task.start_date == start_date
    assert detail.task.due_date == due_date

    new_due_date = datetime(2026, 8, 22, 18, tzinfo=timezone.utc)
    await task_service.update_task(created.task_id, due_date=new_due_date)
    updated = await task_service.get_task(created.task_id)
    assert updated.task.start_date == start_date
    assert updated.task.due_date == new_due_date


async def test_create_task_uses_project_id_fallback_from_settings(fake_client):
    settings = Settings(
        vikunja_base_url="https://vikunja.example.com",
        vikunja_api_token="test-token",
        vikunja_project_id="42",
    )
    service = TaskService(fake_client, settings)

    summary = await service.create_task(CreateTaskParams(title="フォールバック確認"))

    detail = await service.get_task(summary.task_id)
    assert detail.task.project_id == 42


async def test_create_task_without_project_id_raises(fake_client):
    settings = Settings(
        vikunja_base_url="https://vikunja.example.com",
        vikunja_api_token="test-token",
        vikunja_project_id=None,
    )
    service = TaskService(fake_client, settings)

    with pytest.raises(TaskServiceError):
        await service.create_task(CreateTaskParams(title="プロジェクト未指定"))


async def test_set_task_status_swaps_status_label_and_updates_meta(task_service):
    created = await task_service.create_task(CreateTaskParams(title="タスク"))

    summary = await task_service.set_task_status(
        created.task_id, AgentStatus.RUNNING, agent_id="claude-code"
    )
    assert summary.status == AgentStatus.RUNNING

    detail = await task_service.get_task(created.task_id)
    status_labels = {
        label.title for label in detail.task.labels if label.title.startswith("status-")
    }
    assert status_labels == {"status-running"}
    assert detail.meta.status == AgentStatus.RUNNING
    assert detail.meta.started_at is not None


async def test_set_task_status_swaps_agent_label_when_agent_id_changes(task_service):
    created = await task_service.create_task(
        CreateTaskParams(title="タスク", agent_id="research-01")
    )

    await task_service.set_task_status(
        created.task_id, AgentStatus.RUNNING, agent_id="coding-02"
    )

    detail = await task_service.get_task(created.task_id)
    agent_labels = {
        label.title for label in detail.task.labels if label.title.startswith("agent-")
    }
    assert agent_labels == {"agent-coding-02"}
    assert detail.meta.agent_id == "coding-02"


async def test_set_task_status_same_status_twice_is_idempotent(task_service):
    created = await task_service.create_task(CreateTaskParams(title="タスク"))
    await task_service.set_task_status(created.task_id, AgentStatus.RUNNING)
    await task_service.set_task_status(created.task_id, AgentStatus.RUNNING)

    detail = await task_service.get_task(created.task_id)
    status_labels = [
        label.title for label in detail.task.labels if label.title.startswith("status-")
    ]
    assert status_labels == ["status-running"]


async def test_add_task_comment(task_service, fake_client):
    created = await task_service.create_task(CreateTaskParams(title="タスク"))

    comment = await task_service.add_task_comment(created.task_id, "進捗レポートです")

    assert comment.comment == "進捗レポートです"
    stored = await fake_client.list_comments(created.task_id)
    assert stored[-1].comment == "進捗レポートです"


async def test_complete_task_marks_done_and_records_result_summary(task_service, fake_client):
    created = await task_service.create_task(CreateTaskParams(title="タスク"))

    summary = await task_service.complete_task(created.task_id, result_summary="全部終わった")

    assert summary.done is True
    assert summary.status == AgentStatus.COMPLETED
    detail = await task_service.get_task(created.task_id)
    assert detail.meta.result_summary == "全部終わった"
    assert detail.meta.completed_at is not None
    comments = await fake_client.list_comments(created.task_id)
    assert any("全部終わった" in c.comment for c in comments)


async def test_fail_task_does_not_mark_done_but_records_message(task_service, fake_client):
    created = await task_service.create_task(CreateTaskParams(title="タスク"))

    summary = await task_service.set_task_status(
        created.task_id, AgentStatus.FAILED, message="テストが失敗しました"
    )

    assert summary.done is False
    assert summary.status == AgentStatus.FAILED
    detail = await task_service.get_task(created.task_id)
    assert detail.meta.result_summary == "テストが失敗しました"
    comments = await fake_client.list_comments(created.task_id)
    assert any("テストが失敗しました" in c.comment for c in comments)


async def test_reopen_task_resets_done_flag_and_status(task_service):
    created = await task_service.create_task(CreateTaskParams(title="タスク"))
    await task_service.complete_task(created.task_id)

    summary = await task_service.reopen_task(created.task_id)

    assert summary.done is False
    assert summary.status == AgentStatus.QUEUED


async def test_update_task_preserves_meta_block_while_replacing_body(task_service):
    created = await task_service.create_task(
        CreateTaskParams(title="タスク", agent_id="claude-code")
    )

    await task_service.update_task(created.task_id, description="新しい説明本文です")

    detail = await task_service.get_task(created.task_id)
    assert detail.task.description.startswith("新しい説明本文です")
    assert detail.meta.agent_id == "claude-code"
    assert detail.meta.status == AgentStatus.QUEUED


async def test_list_tasks_filters_by_agent_id_and_status(task_service):
    t1 = await task_service.create_task(CreateTaskParams(title="A", agent_id="claude-code"))
    t2 = await task_service.create_task(CreateTaskParams(title="B", agent_id="codex"))
    await task_service.set_task_status(t2.task_id, AgentStatus.RUNNING, agent_id="codex")

    by_agent = await task_service.list_tasks(agent_id="claude-code")
    assert [s.task_id for s in by_agent] == [t1.task_id]

    by_status = await task_service.list_tasks(status_filter=AgentStatus.RUNNING)
    assert [s.task_id for s in by_status] == [t2.task_id]


async def test_execution_id_is_auto_generated_and_stable_until_new_run(task_service):
    created = await task_service.create_task(
        CreateTaskParams(title="タスク", agent_id="claude-code")
    )

    await task_service.set_task_status(
        created.task_id, AgentStatus.RUNNING, agent_id="claude-code"
    )
    first_detail = await task_service.get_task(created.task_id)
    first_execution_id = first_detail.meta.execution_id
    assert first_execution_id

    # ハートビート相当の再通知: 実行中のまま execution_id は変わらない
    await task_service.set_task_status(
        created.task_id,
        AgentStatus.RUNNING,
        agent_id="claude-code",
        post_comment=False,
    )
    second_detail = await task_service.get_task(created.task_id)
    assert second_detail.meta.execution_id == first_execution_id

    # 完了後に再度開始すると新しい execution_id が発行される
    await task_service.complete_task(created.task_id)
    await task_service.set_task_status(
        created.task_id, AgentStatus.RUNNING, agent_id="claude-code"
    )
    third_detail = await task_service.get_task(created.task_id)
    assert third_detail.meta.execution_id != first_execution_id


async def test_execution_id_preserved_when_resuming_from_blocked_or_needs_input(
    task_service,
):
    created = await task_service.create_task(
        CreateTaskParams(title="タスク", agent_id="claude-code")
    )
    await task_service.set_task_status(
        created.task_id, AgentStatus.RUNNING, agent_id="claude-code"
    )
    first_execution_id = (await task_service.get_task(created.task_id)).meta.execution_id
    assert first_execution_id

    # blocked を経由して再開しても、同一実行の継続として execution_id は変わらない
    await task_service.set_task_status(
        created.task_id, AgentStatus.BLOCKED, agent_id="claude-code", message="依存タスク待ち"
    )
    blocked_detail = await task_service.get_task(created.task_id)
    assert blocked_detail.meta.execution_id == first_execution_id

    resumed = await task_service.set_task_status(
        created.task_id, AgentStatus.RUNNING, agent_id="claude-code"
    )
    assert resumed.status == AgentStatus.RUNNING
    resumed_detail = await task_service.get_task(created.task_id)
    assert resumed_detail.meta.execution_id == first_execution_id

    # needs-input を経由した場合も同様
    await task_service.set_task_status(
        created.task_id, AgentStatus.NEEDS_INPUT, agent_id="claude-code"
    )
    await task_service.set_task_status(
        created.task_id, AgentStatus.RUNNING, agent_id="claude-code"
    )
    final_detail = await task_service.get_task(created.task_id)
    assert final_detail.meta.execution_id == first_execution_id


@pytest.mark.parametrize("terminal_status", [AgentStatus.COMPLETED, AgentStatus.FAILED])
async def test_execution_id_regenerated_when_restarting_after_terminal_status(
    task_service, terminal_status
):
    created = await task_service.create_task(CreateTaskParams(title="タスク"))
    await task_service.set_task_status(created.task_id, AgentStatus.RUNNING)
    first_execution_id = (await task_service.get_task(created.task_id)).meta.execution_id
    assert first_execution_id

    await task_service.set_task_status(created.task_id, terminal_status, message="終了")
    await task_service.set_task_status(created.task_id, AgentStatus.RUNNING)

    detail = await task_service.get_task(created.task_id)
    assert detail.meta.execution_id != first_execution_id


async def test_set_task_status_post_comment_false_skips_comment(task_service, fake_client):
    created = await task_service.create_task(CreateTaskParams(title="タスク"))

    await task_service.set_task_status(
        created.task_id, AgentStatus.RUNNING, post_comment=False
    )

    comments = await fake_client.list_comments(created.task_id)
    assert comments == []
    last_comment_at = await task_service.get_last_comment_at(created.task_id)
    assert last_comment_at is None


async def test_list_tasks_max_items_applies_after_filtering_not_before(task_service):
    # 先に非該当タスクを max_items 件以上作り、その後に該当タスクを作る。
    # max_items がフィルタ前の生の取得件数に効いてしまうと、後から作った該当タスクが
    # 取りこぼされてしまう。
    for i in range(3):
        await task_service.create_task(CreateTaskParams(title=f"他エージェント{i}", agent_id="other"))
    matching_ids = []
    for i in range(2):
        created = await task_service.create_task(
            CreateTaskParams(title=f"対象{i}", agent_id="claude-code")
        )
        matching_ids.append(created.task_id)

    summaries = await task_service.list_tasks(agent_id="claude-code", max_items=2)

    assert [s.task_id for s in summaries] == matching_ids


async def test_get_dashboard_buckets_by_status_and_flags_stale_running(
    task_service, fake_client
):
    t1 = await task_service.create_task(CreateTaskParams(title="A", agent_id="a1"))
    t2 = await task_service.create_task(CreateTaskParams(title="B", agent_id="a2"))
    await task_service.set_task_status(t1.task_id, AgentStatus.RUNNING, agent_id="a1")

    dashboard = await task_service.get_dashboard(stale_running_minutes=30)
    assert dashboard.total == 2
    assert dashboard.by_status["running"] == 1
    assert dashboard.by_status["queued"] == 1
    assert dashboard.stale_running == []

    detail = await task_service.get_task(t1.task_id)
    stale_meta = detail.meta.model_copy(
        update={"last_update_at": datetime.now(timezone.utc) - timedelta(minutes=90)}
    )
    stale_description = status_logic.upsert_meta_block(detail.task.description, stale_meta)
    await fake_client.update_task(t1.task_id, description=stale_description)

    dashboard2 = await task_service.get_dashboard(stale_running_minutes=30)
    assert [s.task_id for s in dashboard2.stale_running] == [t1.task_id]
