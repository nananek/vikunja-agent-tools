import pytest

from vikunja_agent_tools.openwebui_tools import Tools


@pytest.fixture
def tools(task_service):
    instance = Tools.__new__(Tools)
    instance._task_service = task_service
    return instance


async def test_create_and_list_tasks(tools):
    created = await tools.create_task(title="部屋の掃除", agent_id="claude-code")
    assert "タスクを作成しました" in created
    assert "部屋の掃除" in created

    listed = await tools.list_tasks(agent_id="claude-code")
    assert "部屋の掃除" in listed


async def test_list_projects(tools):
    projects = await tools.list_projects()
    assert projects == "#1: 資格学習"


async def test_start_and_show_task(tools):
    created = await tools.create_task(title="レポート作成")
    task_id = int(created.splitlines()[1].split("#")[1].split(":")[0])

    started = await tools.start_task(task_id, agent_id="claude-code", message="開始します")
    assert "タスクを開始しました" in started
    assert "running" in started

    detail = await tools.show_task(task_id)
    assert "実行ID" in detail
    assert "開始します" not in detail  # メッセージはコメントに残るが詳細本文には含めない


async def test_complete_task_reports_result_summary(tools):
    created = await tools.create_task(title="バグ修正")
    task_id = int(created.splitlines()[1].split("#")[1].split(":")[0])

    completed = await tools.complete_task(task_id, result_summary="修正して確認済み")
    assert "タスクを完了しました" in completed
    assert "完了: はい" in completed


async def test_fail_task_returns_readable_text(tools):
    created = await tools.create_task(title="デプロイ")
    task_id = int(created.splitlines()[1].split("#")[1].split(":")[0])

    failed = await tools.fail_task(task_id, message="権限エラーで失敗")
    assert "タスクを失敗として記録しました" in failed
    assert "failed" in failed


async def test_show_task_not_found_returns_japanese_error_text(tools):
    result = await tools.show_task(999)
    assert "エラーが発生しました" in result
    assert "not_found" in result


async def test_list_tasks_invalid_status_returns_error_text(tools):
    result = await tools.list_tasks(status="invalid-status")
    assert "入力エラー" in result


async def test_agent_dashboard_reports_counts(tools):
    await tools.create_task(title="A")
    text = await tools.agent_dashboard()
    assert "合計タスク数" in text
    assert "queued" in text
