import json
import logging
import ssl

import httpx
import pytest
import respx

from vikunja_agent_tools.vikunja_client import (
    VikunjaAPIError,
    VikunjaAuthError,
    VikunjaClient,
    VikunjaNotFoundError,
    VikunjaRateLimitError,
    VikunjaServerError,
)

BASE_URL = "https://vikunja.example.com"
API_ROOT = f"{BASE_URL}/api/v1"
TOKEN = "super-secret-token"


def _body(request: httpx.Request) -> object:
    return json.loads(request.content)


@pytest.fixture
def client():
    c = VikunjaClient(BASE_URL, TOKEN, timeout_seconds=5)
    yield c


async def test_create_task_sends_auth_header_and_correct_body(respx_mock, client):
    route = respx_mock.put(f"{API_ROOT}/projects/7/tasks").mock(
        return_value=httpx.Response(200, json={"id": 1, "title": "掃除する"})
    )

    task = await client.create_task(7, "掃除する", description="部屋の掃除")

    assert task.id == 1
    assert task.title == "掃除する"
    sent_request = route.calls.last.request
    assert sent_request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert _body(sent_request) == {"title": "掃除する", "description": "部屋の掃除"}


async def test_get_task(respx_mock, client):
    respx_mock.get(f"{API_ROOT}/tasks/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "title": "テスト"})
    )
    task = await client.get_task(42)
    assert task.id == 42


async def test_update_task(respx_mock, client):
    route = respx_mock.post(f"{API_ROOT}/tasks/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "title": "更新後", "done": True})
    )
    task = await client.update_task(42, done=True)
    assert task.done is True
    assert _body(route.calls.last.request) == {"done": True}


async def test_401_raises_auth_error_without_leaking_token(respx_mock, client, caplog):
    respx_mock.get(f"{API_ROOT}/tasks/1").mock(
        return_value=httpx.Response(401, json={"message": "invalid token"})
    )
    caplog.set_level(logging.INFO, logger="vikunja_agent_tools.client")

    with pytest.raises(VikunjaAuthError) as exc_info:
        await client.get_task(1)

    payload = exc_info.value.to_error_payload()
    assert payload["kind"] == "auth_error"
    assert payload["status_code"] == 401
    assert TOKEN not in payload["message"]
    assert TOKEN not in caplog.text
    assert TOKEN not in str(exc_info.value)


async def test_404_raises_not_found_error(respx_mock, client):
    respx_mock.get(f"{API_ROOT}/tasks/999").mock(return_value=httpx.Response(404))
    with pytest.raises(VikunjaNotFoundError) as exc_info:
        await client.get_task(999)
    assert exc_info.value.status_code == 404


async def test_4xx_does_not_retry(respx_mock, client):
    route = respx_mock.get(f"{API_ROOT}/tasks/1").mock(return_value=httpx.Response(400))
    with pytest.raises(VikunjaAPIError):
        await client.get_task(1)
    assert route.calls.call_count == 1


async def test_429_retries_and_respects_retry_after(respx_mock, client):
    route = respx_mock.get(f"{API_ROOT}/tasks/1")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"id": 1, "title": "リトライ後成功"}),
    ]

    task = await client.get_task(1)

    assert task.title == "リトライ後成功"
    assert route.calls.call_count == 2


async def test_5xx_retries_then_raises_server_error(respx_mock, client):
    route = respx_mock.get(f"{API_ROOT}/tasks/1").mock(
        return_value=httpx.Response(503)
    )
    with pytest.raises(VikunjaServerError):
        await client.get_task(1)
    assert route.calls.call_count == 3


async def test_connection_error_retries_then_raises_wrapped_error(respx_mock, client):
    route = respx_mock.get(f"{API_ROOT}/tasks/1").mock(
        side_effect=httpx.ConnectError("dns failure")
    )
    with pytest.raises(VikunjaAPIError) as exc_info:
        await client.get_task(1)
    assert route.calls.call_count == 3
    assert "dns failure" not in str(exc_info.value)


async def test_pagination_iterates_all_pages(respx_mock, client):
    route = respx_mock.get(f"{API_ROOT}/tasks/all")

    def _responder(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            body = [{"id": 1, "title": "task-1"}, {"id": 2, "title": "task-2"}]
        elif page == 2:
            body = [{"id": 3, "title": "task-3"}]
        else:
            body = []
        return httpx.Response(
            200, json=body, headers={"x-pagination-total-pages": "2"}
        )

    route.side_effect = _responder

    tasks = await client.list_tasks()

    assert [t.id for t in tasks] == [1, 2, 3]
    assert route.calls.call_count == 2


async def test_list_tasks_scoped_to_project(respx_mock, client):
    respx_mock.get(f"{API_ROOT}/projects/7/tasks").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 1, "title": "task-1", "project_id": 7}],
            headers={"x-pagination-total-pages": "1"},
        )
    )
    tasks = await client.list_tasks(project_id=7)
    assert len(tasks) == 1
    assert tasks[0].project_id == 7


async def test_comments(respx_mock, client):
    respx_mock.get(f"{API_ROOT}/tasks/1/comments").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "comment": "こんにちは"}])
    )
    add_route = respx_mock.put(f"{API_ROOT}/tasks/1/comments").mock(
        return_value=httpx.Response(200, json={"id": 2, "comment": "追記です"})
    )

    comments = await client.list_comments(1)
    assert comments[0].comment == "こんにちは"

    new_comment = await client.add_comment(1, "追記です")
    assert new_comment.comment == "追記です"
    assert _body(add_route.calls.last.request) == {"comment": "追記です"}


async def test_get_or_create_label_reuses_existing(respx_mock, client):
    respx_mock.get(f"{API_ROOT}/labels").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 5, "title": "status-running"}],
            headers={"x-pagination-total-pages": "1"},
        )
    )
    create_route = respx_mock.put(f"{API_ROOT}/labels")

    label = await client.get_or_create_label("status-running")

    assert label.id == 5
    assert create_route.calls.call_count == 0


async def test_get_or_create_label_creates_when_missing(respx_mock, client):
    respx_mock.get(f"{API_ROOT}/labels").mock(
        return_value=httpx.Response(200, json=[], headers={"x-pagination-total-pages": "1"})
    )
    respx_mock.put(f"{API_ROOT}/labels").mock(
        return_value=httpx.Response(200, json={"id": 9, "title": "status-blocked"})
    )

    label = await client.get_or_create_label("status-blocked")
    assert label.id == 9
    assert label.title == "status-blocked"


async def test_add_and_remove_label_from_task(respx_mock, client):
    add_route = respx_mock.put(f"{API_ROOT}/tasks/1/labels").mock(
        return_value=httpx.Response(200, json={})
    )
    remove_route = respx_mock.delete(f"{API_ROOT}/tasks/1/labels/5").mock(
        return_value=httpx.Response(200, json={})
    )

    await client.add_label_to_task(1, 5)
    await client.remove_label_from_task(1, 5)

    assert _body(add_route.calls.last.request) == {"label_id": 5}
    assert remove_route.calls.call_count == 1


def test_verify_tls_is_passed_through_to_transport():
    secure_client = VikunjaClient(BASE_URL, TOKEN, verify_tls=True)
    insecure_client = VikunjaClient(BASE_URL, TOKEN, verify_tls=False)

    secure_ctx = secure_client._client._transport._pool._ssl_context
    insecure_ctx = insecure_client._client._transport._pool._ssl_context

    assert secure_ctx.verify_mode == ssl.CERT_REQUIRED
    assert insecure_ctx.verify_mode == ssl.CERT_NONE


def test_timeout_is_passed_through():
    client = VikunjaClient(BASE_URL, TOKEN, timeout_seconds=3.5)
    assert client._client.timeout.connect == 3.5
    assert client._client.timeout.read == 3.5
