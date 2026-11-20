"""接口级验收：迁移明细、版本切换、回查与阻断的 HTTP 契约。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app
from app.seed import SEED_ROWS
from app.store import store


def _reset() -> None:
    store._tables["attr_dict"] = [dict(row) for row in SEED_ROWS["attr_dict"]]
    store._tables["attr_enum_sample"] = [dict(row) for row in SEED_ROWS["attr_enum_sample"]]
    store._tables["tag_compute"] = [dict(row) for row in SEED_ROWS["tag_compute"]]
    store._artifacts.clear()
    store._snapshots.clear()
    store._compute_cache.clear()


async def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    """直接驱动 ASGI 应用，避免引入额外的测试依赖。"""
    body = json.dumps(payload).encode() if payload is not None else b""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"host", b"testserver"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if body and not any(m.get("type") == "http.request" for m in messages):
            messages.append({"type": "http.request"})
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    chunks: list[bytes] = []
    status = {"code": 0}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    raw = b"".join(chunks)
    return status["code"], json.loads(raw) if raw else None


def call(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    return asyncio.run(_request(method, path, payload))


class TestMigrationApi(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def test_underscored_module_path_is_served_for_frontend(self) -> None:
        """前端使用的下划线模块路径能落到同一套接口上。"""
        status, data = call("GET", "/api/attr_dict")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 5)

    def test_migration_detail_lists_every_sample(self) -> None:
        status, data = call("GET", "/api/attr-dict/ATTR-005/migration")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["details"]), 7)
        statuses = {row["sample_id"]: row["status"] for row in data["details"]}
        self.assertEqual(statuses["AES-001"], "migrated")
        self.assertEqual(statuses["AES-003"], "blocked_missing_source")

    def test_apply_then_detail_reports_applied(self) -> None:
        status, data = call("POST", "/api/attr-dict/ATTR-005/migration/apply")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["entry"]["attr"]["field_version"], 2)

        status, detail = call("GET", "/api/attr-dict/migrations")
        self.assertEqual(status, 200)
        self.assertEqual(detail["items"][0]["result"], "applied")

    def test_switch_endpoint_returns_conflict_for_ambiguous_tag(self) -> None:
        call("POST", "/api/attr-dict/ATTR-005/migration/apply")
        status, data = call("POST", "/api/tag-compute/TC-005/run", {"field_versions": {"ATTR-005": 2}})
        self.assertEqual(status, 409)
        self.assertEqual(data["detail"]["issues"][0]["code"], "ambiguous_original_value")

        status, replay = call("POST", "/api/tag-compute/TC-005/replay")
        self.assertEqual(status, 200)
        self.assertEqual(replay["entry"]["field_versions"]["ATTR-005"], 1)
        self.assertIn("普通", replay["entry"]["condition_used"])

    def test_switch_endpoint_succeeds_for_tag_a(self) -> None:
        call("POST", "/api/attr-dict/ATTR-005/migration/apply")
        status, data = call("POST", "/api/tag-compute/TC-003/run", {"field_versions": {"*": 2}})
        self.assertEqual(status, 200)
        entry = data["entry"]
        self.assertEqual(entry["mode"], "switch")
        self.assertEqual(entry["field_versions"]["ATTR-005"], 2)
        self.assertIn("GOLD", entry["condition_used"])

        status, runs = call("GET", "/api/tag-compute/runs")
        self.assertEqual(status, 200)
        self.assertTrue(any(run["field_versions"].get("ATTR-005") == 2 for run in runs["items"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
