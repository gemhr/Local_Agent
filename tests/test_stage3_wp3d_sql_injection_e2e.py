"""WP3-D：真实 FastAPI route schema + 临时 Memory SQLite 的 HTTP E2E。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

import server
from core.memory_manager import MemoryManager


SQL_CORPUS = (
    "'",
    "' OR 1=1 --",
    '"; DROP TABLE messages; --',
    "/* comment */",
    "-- comment",
    "; SELECT 1;",
    "'); DELETE FROM messages; --",
)


class _MemoryHttpService:
    def __init__(self, manager: MemoryManager) -> None:
        self.manager = manager
        self.calls: list[tuple[str, object]] = []

    def get_history(self, *, agent_id: str, limit: int, offset: int) -> list[dict]:
        self.calls.append(("history", (agent_id, limit, offset)))
        records = self.manager.get_chat_history(
            agent_id=agent_id,
            limit=limit,
            offset=offset,
            ascending=False,
            memory_scope="direct",
        )
        return list(reversed(records))

    def search_memory(self, keyword: str) -> list[dict]:
        self.calls.append(("search", keyword))
        return self.manager.search_messages(keyword, memory_scope="direct")

    def get_all_memory(self) -> dict[str, list[dict]]:
        self.calls.append(("get_all", None))
        return {
            "messages": self.manager.get_all_messages(),
            "summaries": self.manager.get_all_summaries(),
        }

    def delete_memory(
        self, *, message_ids: list[int] | None = None, delete_all: bool = False
    ) -> dict[str, object]:
        self.calls.append(("delete", (message_ids, delete_all)))
        if delete_all:
            self.manager.clear_all_memory()
            return {
                "status": "success",
                "affected_agent_ids": [],
                "refresh_agent_ids": [],
                "delete_all": True,
            }
        result: dict[str, object] = self.manager.delete_messages(message_ids or [])
        result["status"] = "success"
        result["delete_all"] = False
        return result


def _schema_names(path: str) -> frozenset[str]:
    with sqlite3.connect(path) as connection:
        return frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            )
        )


def _http_client(service: object, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    monkeypatch.setattr(server, "chat_service", service)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_history_sql_looking_agent_id_and_valid_pagination_are_bound_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MemoryManager(str(tmp_path / "history.db"))
    service = _MemoryHttpService(manager)
    manager.add_message("agent-a", "user", "A-PRIVATE")
    manager.add_message("victim", "user", "VICTIM-PRIVATE")
    before_schema = _schema_names(manager.db_path)

    async with _http_client(service, monkeypatch) as client:
        injected = quote("a' OR 1=1 --", safe="")
        response = await client.get(
            f"/api/history/{injected}", params={"limit": 100, "offset": 100000}
        )
        assert response.status_code == 200
        assert response.json() == {"messages": []}
        assert "VICTIM-PRIVATE" not in response.text

        response = await client.get(
            "/api/history/agent-a", params={"limit": 1, "offset": 0}
        )
        assert response.status_code == 200
        assert [row["content"] for row in response.json()["messages"]] == ["A-PRIVATE"]
    assert _schema_names(manager.db_path) == before_schema


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 101},
        {"limit": "1 OR 1=1"},
        {"offset": -1},
        {"offset": 100001},
        {"offset": "0; DROP TABLE messages"},
    ],
)
async def test_history_invalid_pagination_is_422_before_service_or_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, object],
) -> None:
    manager = MemoryManager(str(tmp_path / "invalid-history.db"))
    service = _MemoryHttpService(manager)
    before_schema = _schema_names(manager.db_path)

    async with _http_client(service, monkeypatch) as client:
        response = await client.get("/api/history/agent-a", params=params)

    assert response.status_code == 422
    assert service.calls == []
    assert _schema_names(manager.db_path) == before_schema


def test_history_openapi_has_no_request_controlled_sort_or_sql_authority() -> None:
    operation = server.app.openapi()["paths"]["/api/history/{agent_id}"]["get"]
    assert {parameter["name"] for parameter in operation["parameters"]} == {
        "agent_id",
        "limit",
        "offset",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("query", SQL_CORPUS)
async def test_search_sql_corpus_is_data_not_statement_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    manager = MemoryManager(str(tmp_path / f"search-{abs(hash(query))}.db"))
    service = _MemoryHttpService(manager)
    manager.add_message("core_router", "user", "needle")
    manager.add_message("victim", "user", "UNRELATED-PRIVATE")
    before_schema = _schema_names(manager.db_path)

    async with _http_client(service, monkeypatch) as client:
        response = await client.get("/api/search", params={"keyword": query})

    assert response.status_code == 200
    assert "UNRELATED-PRIVATE" not in {row["content"] for row in response.json()["results"]}
    assert _schema_names(manager.db_path) == before_schema


@pytest.mark.asyncio
async def test_search_fts_and_like_are_semantic_limitations_not_sql_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MemoryManager(str(tmp_path / "search-semantics.db"))
    service = _MemoryHttpService(manager)
    for content in ("needle", "other", "needle other", "unrelated"):
        manager.add_message("core_router", "user", content)
    before_schema = _schema_names(manager.db_path)

    async with _http_client(service, monkeypatch) as client:
        responses = {
            query: await client.get("/api/search", params={"keyword": query})
            for query in ("needle OR other", "needle NOT other", "NEAR(needle other)", "'", "%", "_")
        }

    assert all(response.status_code == 200 for response in responses.values())
    assert {row["content"] for row in responses["needle OR other"].json()["results"]} == {
        "needle",
        "other",
        "needle other",
    }
    assert {row["content"] for row in responses["needle NOT other"].json()["results"]} == {
        "needle"
    }
    assert {row["content"] for row in responses["NEAR(needle other)"].json()["results"]} <= {
        "needle other"
    }
    assert responses["'"].json()["results"] == []
    assert {row["content"] for row in responses["%"].json()["results"]} == {
        "needle",
        "other",
        "needle other",
        "unrelated",
    }
    assert responses["_"].json()["results"] == []
    # SEARCH_QUERY_SEMANTIC_LIMITATION != SQL_INJECTION：不承诺 literal search。
    assert _schema_names(manager.db_path) == before_schema


@pytest.mark.asyncio
async def test_delete_one_and_exactly_1000_ids_only_delete_selected_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MemoryManager(str(tmp_path / "delete-valid.db"))
    service = _MemoryHttpService(manager)
    one_id = manager.add_message("target-one", "user", "ONE")
    thousand_ids = [
        manager.add_message("target-many", "user", f"TARGET-{index}")
        for index in range(1000)
    ]
    victim_id = manager.add_message("victim", "user", "UNRELATED-PRIVATE")
    before_schema = _schema_names(manager.db_path)

    async with _http_client(service, monkeypatch) as client:
        one = await client.request("DELETE", "/api/memory", json={"message_ids": [one_id]})
        many = await client.request(
            "DELETE", "/api/memory", json={"message_ids": thousand_ids}
        )

    assert one.status_code == 200
    assert many.status_code == 200
    with sqlite3.connect(manager.db_path) as connection:
        assert connection.execute(
            "SELECT content FROM messages WHERE id = ?", (victim_id,)
        ).fetchone()[0] == "UNRELATED-PRIVATE"
        remaining_targets = connection.execute(
            "SELECT COUNT(1) FROM messages WHERE agent_id LIKE 'target-%'"
        ).fetchone()[0]
    assert remaining_targets == 0
    assert _schema_names(manager.db_path) == before_schema


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_ids",
    [
        list(range(1, 1002)),
        ["not-an-integer"],
        ["1) OR 1=1 --"],
        [-1],
        [0],
        [2**63],
    ],
)
async def test_delete_invalid_ids_are_422_before_service_or_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message_ids: list[object],
) -> None:
    manager = MemoryManager(str(tmp_path / f"delete-invalid-{len(message_ids)}-{abs(hash(str(message_ids[:2])))}.db"))
    service = _MemoryHttpService(manager)
    manager.add_message("victim", "user", "UNRELATED-PRIVATE")
    before_rows = list(manager.get_all_messages())
    before_schema = _schema_names(manager.db_path)

    async with _http_client(service, monkeypatch) as client:
        response = await client.request(
            "DELETE", "/api/memory", json={"message_ids": message_ids}
        )

    assert response.status_code == 422
    assert service.calls == []
    assert manager.get_all_messages() == before_rows
    assert _schema_names(manager.db_path) == before_schema


@pytest.mark.asyncio
async def test_delete_all_is_boolean_application_branch_with_constant_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MemoryManager(str(tmp_path / "delete-all.db"))
    service = _MemoryHttpService(manager)
    manager.add_message("target", "user", "DELETE-ME")
    before_schema = _schema_names(manager.db_path)

    async with _http_client(service, monkeypatch) as client:
        response = await client.request(
            "DELETE", "/api/memory", json={"delete_all": True}
        )

    assert response.status_code == 200
    assert service.calls == [("delete", ([], True))]
    assert manager.get_all_messages() == []
    assert _schema_names(manager.db_path) == before_schema


class _FailingSearchService:
    def search_memory(self, _keyword: str):
        raise sqlite3.OperationalError(
            "WP3D_SQL_MARKER_A71F WP3D_SCHEMA_MARKER_A71F "
            r"C:\wp3d-secret\db.sqlite"
        )


@pytest.mark.asyncio
async def test_database_error_markers_never_reach_user_visible_http_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _http_client(_FailingSearchService(), monkeypatch) as client:
        response = await client.get("/api/search", params={"keyword": "needle"})

    visible = response.text + "\n" + "\n".join(
        f"{key}: {value}" for key, value in response.headers.items()
    )
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    for marker in (
        "WP3D_SQL_MARKER_A71F",
        "WP3D_SCHEMA_MARKER_A71F",
        r"C:\wp3d-secret\db.sqlite",
    ):
        assert marker not in visible
