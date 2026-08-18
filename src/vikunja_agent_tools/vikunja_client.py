"""Vikunja REST API の非同期クライアント。

トークンはログ・例外メッセージに一切出力しない。削除系操作 (タスク削除・
プロジェクト削除等) はここでは実装しない — 安全性のため最下層から露出面を
なくす方針。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from vikunja_agent_tools.models import TaskComment, VikunjaLabel, VikunjaTask

logger = logging.getLogger("vikunja_agent_tools.client")

_MAX_RETRIES = 3
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_DEFAULT_PER_PAGE = 50


class VikunjaAPIError(Exception):
    """Vikunja API 呼び出しに関する例外の基底クラス。"""

    kind = "api_error"

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def to_error_payload(self) -> dict[str, Any]:
        return {
            "error": True,
            "kind": self.kind,
            "status_code": self.status_code,
            "message": self.message,
        }


class VikunjaAuthError(VikunjaAPIError):
    kind = "auth_error"


class VikunjaNotFoundError(VikunjaAPIError):
    kind = "not_found"


class VikunjaRateLimitError(VikunjaAPIError):
    kind = "rate_limited"


class VikunjaServerError(VikunjaAPIError):
    kind = "server_error"


def _error_for_status(status_code: int, message: str) -> VikunjaAPIError:
    if status_code in (401, 403):
        return VikunjaAuthError(message, status_code=status_code)
    if status_code == 404:
        return VikunjaNotFoundError(message, status_code=status_code)
    if status_code == 429:
        return VikunjaRateLimitError(message, status_code=status_code)
    if status_code >= 500:
        return VikunjaServerError(message, status_code=status_code)
    return VikunjaAPIError(message, status_code=status_code)


class VikunjaClient:
    """`httpx.AsyncClient` をラップした Vikunja API クライアント。"""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        timeout_seconds: float = 15,
        verify_tls: bool = True,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v1",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=timeout_seconds,
            verify=verify_tls,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "VikunjaClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            start = time.monotonic()
            try:
                response = await self._client.request(
                    method, path, json=json_body, params=params
                )
            except httpx.TimeoutException as exc:
                logger.warning(
                    "vikunja request timeout method=%s path=%s attempt=%d",
                    method,
                    path,
                    attempt,
                )
                if attempt >= _MAX_RETRIES:
                    raise VikunjaAPIError(
                        f"Vikunja API へのリクエストがタイムアウトしました: {method} {path}"
                    ) from exc
                await asyncio.sleep(_backoff_seconds(attempt))
                continue

            elapsed_ms = (time.monotonic() - start) * 1000
            logger.info(
                "vikunja request method=%s path=%s status_code=%d elapsed_ms=%.1f",
                method,
                path,
                response.status_code,
                elapsed_ms,
            )

            if response.status_code < 400:
                return response

            if (
                response.status_code in _RETRYABLE_STATUS_CODES
                and attempt < _MAX_RETRIES
            ):
                delay = _retry_after_seconds(response) or _backoff_seconds(attempt)
                await asyncio.sleep(delay)
                continue

            raise _error_for_status(
                response.status_code,
                f"Vikunja API がエラーを返しました ({response.status_code}): {method} {path}",
            )

    async def create_task(
        self, project_id: int, title: str, description: str | None = None, **fields: Any
    ) -> VikunjaTask:
        body: dict[str, Any] = {"title": title, **fields}
        if description is not None:
            body["description"] = description
        response = await self._request(
            "PUT", f"/projects/{project_id}/tasks", json_body=body
        )
        return VikunjaTask.model_validate(response.json())

    async def get_task(self, task_id: int) -> VikunjaTask:
        response = await self._request("GET", f"/tasks/{task_id}")
        return VikunjaTask.model_validate(response.json())

    async def update_task(self, task_id: int, **fields: Any) -> VikunjaTask:
        response = await self._request("POST", f"/tasks/{task_id}", json_body=fields)
        return VikunjaTask.model_validate(response.json())

    async def iter_pages(
        self, path: str, *, params: dict[str, Any] | None = None, per_page: int = _DEFAULT_PER_PAGE
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """`path` をページングしながら生の JSON 配列を1ページずつ返す。"""
        page = 1
        base_params = dict(params or {})
        while True:
            response = await self._request(
                "GET", path, params={**base_params, "page": page, "per_page": per_page}
            )
            items = response.json() or []
            yield items

            total_pages_header = response.headers.get("x-pagination-total-pages")
            if not items:
                return
            if total_pages_header is not None:
                try:
                    total_pages = int(total_pages_header)
                except ValueError:
                    total_pages = page
                if page >= total_pages:
                    return
            elif len(items) < per_page:
                return
            page += 1

    async def list_all_pages(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        per_page: int = _DEFAULT_PER_PAGE,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """全ページを集約して1つのリストとして返す。`max_items` で上限を設けられる。"""
        collected: list[dict[str, Any]] = []
        async for page_items in self.iter_pages(path, params=params, per_page=per_page):
            collected.extend(page_items)
            if max_items is not None and len(collected) >= max_items:
                return collected[:max_items]
        return collected

    async def list_tasks(
        self, project_id: int | None = None, *, max_items: int | None = None
    ) -> list[VikunjaTask]:
        path = f"/projects/{project_id}/tasks" if project_id is not None else "/tasks/all"
        raw_items = await self.list_all_pages(path, max_items=max_items)
        return [VikunjaTask.model_validate(item) for item in raw_items]

    async def list_comments(self, task_id: int) -> list[TaskComment]:
        response = await self._request("GET", f"/tasks/{task_id}/comments")
        return [TaskComment.model_validate(item) for item in response.json() or []]

    async def add_comment(self, task_id: int, comment: str) -> TaskComment:
        response = await self._request(
            "PUT", f"/tasks/{task_id}/comments", json_body={"comment": comment}
        )
        return TaskComment.model_validate(response.json())

    async def list_labels(self) -> list[VikunjaLabel]:
        raw_items = await self.list_all_pages("/labels")
        return [VikunjaLabel.model_validate(item) for item in raw_items]

    async def create_label(self, title: str, hex_color: str | None = None) -> VikunjaLabel:
        body: dict[str, Any] = {"title": title}
        if hex_color is not None:
            body["hex_color"] = hex_color
        response = await self._request("PUT", "/labels", json_body=body)
        return VikunjaLabel.model_validate(response.json())

    async def get_or_create_label(self, title: str) -> VikunjaLabel:
        for label in await self.list_labels():
            if label.title == title:
                return label
        return await self.create_label(title)

    async def add_label_to_task(self, task_id: int, label_id: int) -> None:
        await self._request(
            "PUT", f"/tasks/{task_id}/labels", json_body={"label_id": label_id}
        )

    async def remove_label_from_task(self, task_id: int, label_id: int) -> None:
        await self._request("DELETE", f"/tasks/{task_id}/labels/{label_id}")


def _backoff_seconds(attempt: int) -> float:
    return min(2 ** (attempt - 1) * 0.5, 5.0)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    retry_after = response.headers.get("retry-after")
    if retry_after is None:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None
