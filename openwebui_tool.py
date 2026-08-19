"""Vikunja tool for Open WebUI.

Paste this file into Workspace -> Tools.  It intentionally has no imports from
this repository and uses only Python's standard library.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any


class Tools:
    class Valves:
        VIKUNJA_BASE_URL: str = ""
        VIKUNJA_API_TOKEN: str = ""
        VIKUNJA_PROJECT_ID: int | None = None
        VIKUNJA_TIMEOUT_SECONDS: float = 15.0
        VIKUNJA_AGENT_LABEL_PREFIX: str = "agent-"
        VIKUNJA_STATUS_LABEL_PREFIX: str = "status-"

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _request_sync(
        self, method: str, path: str, body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        if not self.valves.VIKUNJA_BASE_URL or not self.valves.VIKUNJA_API_TOKEN:
            raise ValueError("VIKUNJA_BASE_URL と VIKUNJA_API_TOKEN を Valves に設定してください")
        url = self.valves.VIKUNJA_BASE_URL.rstrip("/") + "/api/v1" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.valves.VIKUNJA_API_TOKEN,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.valves.VIKUNJA_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Vikunja API エラー ({exc.code})") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("Vikunja API への接続に失敗しました") from exc
        return json.loads(raw) if raw else None

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                       query: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self._request_sync, method, path, body, query)

    async def _task(self, task_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/tasks/{task_id}")

    def _meta(self, description: str | None) -> dict[str, Any]:
        if not description:
            return {}
        match = re.search(r"<!-- agent-meta:begin -->\s*```json\s*(.*?)\s*```\s*<!-- agent-meta:end -->", description, re.S)
        if not match:
            return {}
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return {}

    def _description(self, description: str | None, meta: dict[str, Any]) -> str:
        block = "<!-- agent-meta:begin -->\n```json\n" + json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n```\n<!-- agent-meta:end -->"
        pattern = r"<!-- agent-meta:begin -->.*?<!-- agent-meta:end -->"
        if re.search(pattern, description or "", re.S):
            return re.sub(pattern, block, description or "", flags=re.S)
        return ((description or "").rstrip() + "\n\n" if description else "") + block

    def _summary(self, task: dict[str, Any]) -> str:
        meta = self._meta(task.get("description"))
        return "\n".join([
            f"タスク #{task.get('id')}: {task.get('title', '')}",
            f"状態: {meta.get('status', '(不明)')}",
            f"担当エージェント: {meta.get('agent_id') or '-'}",
            f"完了: {'はい' if task.get('done') else 'いいえ'}",
            f"URL: {self.valves.VIKUNJA_BASE_URL.rstrip('/')}/tasks/{task.get('id')}",
        ])

    async def _set_status(self, task_id: int, status: str, agent_id: str = "", message: str = "") -> str:
        task = await self._task(task_id)
        old = self._meta(task.get("description"))
        now = datetime.now(timezone.utc).isoformat()
        meta = dict(old)
        meta.update({"status": status, "last_update_at": now})
        if agent_id:
            meta["agent_id"] = agent_id
        if status == "running" and (old.get("status") != "running" or not old.get("execution_id")):
            meta["execution_id"] = uuid.uuid4().hex
            meta["started_at"] = now
        if status in {"completed", "failed"}:
            meta["completed_at"] = now
        if status == "completed":
            meta["result_summary"] = message or old.get("result_summary")
        updated = await self._request("POST", f"/tasks/{task_id}", {"description": self._description(task.get("description"), meta), "done": status == "completed"})
        await self._request("PUT", f"/tasks/{task_id}/comments", {"comment": "[agent-status]\nstatus: " + status + ("\n\n" + message if message else "")})
        labels = task.get("labels") or []
        wanted = self.valves.VIKUNJA_STATUS_LABEL_PREFIX + status
        for label in labels:
            if label.get("title", "").startswith(self.valves.VIKUNJA_STATUS_LABEL_PREFIX) and label.get("title") != wanted:
                await self._request("DELETE", f"/tasks/{task_id}/labels/{label['id']}")
        if not any(label.get("title") == wanted for label in labels):
            all_labels = await self._request("GET", "/labels")
            label = next((x for x in all_labels if x.get("title") == wanted), None)
            if label is None:
                label = await self._request("PUT", "/labels", {"title": wanted})
            await self._request("PUT", f"/tasks/{task_id}/labels", {"label_id": label["id"]})
        return self._summary(updated)

    async def create_task(self, title: str, description: str = "", project_id: int | None = None,
                          priority: int | None = None, due_date: str = "", agent_id: str = "") -> str:
        """Vikunja にエージェントタスクを作成する。"""
        project = project_id or self.valves.VIKUNJA_PROJECT_ID
        if not project:
            return "入力エラー: project_id を指定するか Valves に VIKUNJA_PROJECT_ID を設定してください"
        body: dict[str, Any] = {"title": title, "description": description}
        if priority is not None: body["priority"] = priority
        if due_date: body["due_date"] = due_date
        task = await self._request("PUT", f"/projects/{project}/tasks", body)
        return "タスクを作成しました。\n" + await self._set_status(task["id"], "queued", agent_id)

    async def list_tasks(self, project_id: int | None = None, agent_id: str = "", status: str = "", max_items: int = 20) -> str:
        """Vikunja のエージェントタスク一覧を取得する。"""
        path = f"/projects/{project_id or self.valves.VIKUNJA_PROJECT_ID}/tasks" if (project_id or self.valves.VIKUNJA_PROJECT_ID) else "/tasks/all"
        tasks = await self._request("GET", path, query={"per_page": max_items})
        result = [t for t in tasks if (not agent_id or self._meta(t.get("description")).get("agent_id") == agent_id) and (not status or self._meta(t.get("description")).get("status") == status)]
        return "\n\n".join(self._summary(t) for t in result[:max_items]) or "該当するタスクはありません。"

    async def show_task(self, task_id: int) -> str:
        """Vikunja タスクの詳細を表示する。"""
        task = await self._task(task_id)
        return self._summary(task) + ("\n--- 説明 ---\n" + (task.get("description") or "") if task.get("description") else "")

    async def start_task(self, task_id: int, agent_id: str = "", message: str = "") -> str:
        """Vikunja タスクを running 状態にする。"""
        return "タスクを開始しました。\n" + await self._set_status(task_id, "running", agent_id, message)

    async def update_progress(self, task_id: int, message: str, agent_id: str = "") -> str:
        """Vikunja タスクに進捗を記録する。"""
        return "進捗を記録しました。\n" + await self._set_status(task_id, "running", agent_id, message)

    async def complete_task(self, task_id: int, result_summary: str = "", agent_id: str = "") -> str:
        """Vikunja タスクを完了する。"""
        return "タスクを完了しました。\n" + await self._set_status(task_id, "completed", agent_id, result_summary)

    async def fail_task(self, task_id: int, message: str, agent_id: str = "") -> str:
        """Vikunja タスクを失敗として記録する。"""
        return "タスクを失敗として記録しました。\n" + await self._set_status(task_id, "failed", agent_id, message)

    async def request_input(self, task_id: int, message: str = "", agent_id: str = "") -> str:
        """Vikunja タスクを人間の入力待ちにする。"""
        return "人間の入力待ちとして記録しました。\n" + await self._set_status(task_id, "needs-input", agent_id, message)

    async def agent_dashboard(self, project_id: int | None = None) -> str:
        """Vikunja のエージェントタスク状況を集計する。"""
        text = await self.list_tasks(project_id=project_id, max_items=100)
        return "エージェントタスク一覧:\n" + text
