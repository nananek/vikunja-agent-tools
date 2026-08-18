import json

import pytest
from fastmcp import Client

from vikunja_agent_tools import mcp_server

EXPECTED_TOOL_NAMES = {
    "create_agent_task",
    "list_agent_tasks",
    "get_agent_task",
    "start_agent_task",
    "report_agent_progress",
    "heartbeat_agent_task",
    "block_agent_task",
    "request_agent_input",
    "complete_agent_task",
    "fail_agent_task",
    "reopen_agent_task",
    "get_agent_dashboard",
}


@pytest.fixture
def configured(task_service, settings):
    mcp_server.configure(task_service, settings)
    yield
    mcp_server._state.clear()


async def test_all_agent_tools_are_registered():
    async with Client(mcp_server.mcp) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOL_NAMES


async def _call(client: Client, name: str, **kwargs):
    result = await client.call_tool(name, kwargs)
    (content,) = result.content
    return json.loads(content.text)


async def test_create_and_start_task_via_mcp_tools(configured):
    async with Client(mcp_server.mcp) as client:
        created = await _call(
            client, "create_agent_task", title="MCP経由のタスク", agent_id="claude-code"
        )
        assert created["status"] == "queued"

        started = await _call(
            client,
            "start_agent_task",
            task_id=created["task_id"],
            agent_id="claude-code",
        )
        assert started["status"] == "running"

        fetched = await _call(client, "get_agent_task", task_id=created["task_id"])
        assert fetched["meta"]["status"] == "running"
        assert fetched["meta"]["execution_id"]


async def test_get_agent_task_not_found_returns_error_payload(configured):
    async with Client(mcp_server.mcp) as client:
        payload = await _call(client, "get_agent_task", task_id=999)
    assert payload["error"] is True
    assert payload["kind"] == "not_found"


async def test_complete_and_dashboard_via_mcp_tools(configured):
    async with Client(mcp_server.mcp) as client:
        created = await _call(client, "create_agent_task", title="ダッシュボード用")
        await _call(client, "start_agent_task", task_id=created["task_id"])
        await _call(
            client,
            "complete_agent_task",
            task_id=created["task_id"],
            result_summary="完了しました",
        )

        dashboard = await _call(client, "get_agent_dashboard")
        assert dashboard["by_status"]["completed"] == 1
