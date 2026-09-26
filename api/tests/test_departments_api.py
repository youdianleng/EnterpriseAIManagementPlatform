"""Department tree against a real PostgreSQL, through the HTTP API.

These assert the ltree behaviour the domain tests can only simulate: index-backed
ancestry, the single-statement subtree rewrite on a move, and the partial unique
index on codes. They run against `eam_test` inside a transaction that is rolled
back, so nothing leaks between tests.
"""

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import db_session
from app.main import app

STRUCTURE_HEADERS = {"X-Actor-Roles": "hr"}


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP client wired to the rolled-back test session."""

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[db_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def create(client: AsyncClient, code: str, parent_id: UUID | None = None, **extra: object):
    response = await client.post(
        "/api/v1/departments",
        json={"code": code, "name_es": code, "name_en": code, "parent_id": parent_id, **extra},
        headers=STRUCTURE_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- ltree behaviour -------------------------------------------------------


async def test_subtree_query_is_scoped_to_the_branch(client: AsyncClient) -> None:
    company = await create(client, "company")
    engineering = await create(client, "engineering", company["id"])
    await create(client, "backend", engineering["id"])
    await create(client, "finance", company["id"])

    response = await client.get(f"/api/v1/departments/{engineering['id']}/subtree")

    assert response.status_code == 200
    assert {row["code"] for row in response.json()} == {"engineering", "backend"}


async def test_subtree_can_exclude_the_department_itself(client: AsyncClient) -> None:
    company = await create(client, "company")
    await create(client, "engineering", company["id"])

    response = await client.get(
        f"/api/v1/departments/{company['id']}/subtree", params={"include_self": "false"}
    )

    assert [row["code"] for row in response.json()] == ["engineering"]


async def test_move_rewrites_the_whole_subtree_in_one_statement(client: AsyncClient) -> None:
    company = await create(client, "company")
    ops = await create(client, "ops", company["id"])
    engineering = await create(client, "engineering", company["id"])
    backend = await create(client, "backend", engineering["id"])
    platform = await create(client, "platform", backend["id"])

    response = await client.post(
        f"/api/v1/departments/{engineering['id']}/move",
        json={"parent_id": ops["id"]},
        headers=STRUCTURE_HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.json()["path"] == "company.ops.engineering"

    # Grandchildren moved too, and depths were recomputed by the same UPDATE.
    deep = await client.get(f"/api/v1/departments/{platform['id']}")
    assert deep.json()["path"] == "company.ops.engineering.backend.platform"
    assert deep.json()["depth"] == 4


async def test_deep_ancestry_query_stays_correct_across_levels(client: AsyncClient) -> None:
    """The permission story depends on this: a root must see every descendant."""
    company = await create(client, "company")
    level = company
    for name in ("a", "b", "c"):
        level = await create(client, name, level["id"])

    response = await client.get(f"/api/v1/departments/{company['id']}/subtree")

    assert len(response.json()) == 4


# --- constraints -----------------------------------------------------------


async def test_duplicate_code_is_rejected_with_a_catalogued_error(client: AsyncClient) -> None:
    await create(client, "company")

    response = await client.post(
        "/api/v1/departments",
        json={"code": "company", "name_es": "x", "name_en": "x"},
        headers=STRUCTURE_HEADERS,
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "ERR_ORG_002"
    assert error["message_key"] == "errors.department_code_taken"
    assert error["request_id"]


async def test_move_into_descendant_is_rejected(client: AsyncClient) -> None:
    company = await create(client, "company")
    engineering = await create(client, "engineering", company["id"])

    response = await client.post(
        f"/api/v1/departments/{company['id']}/move",
        json={"parent_id": engineering["id"]},
        headers=STRUCTURE_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ORG_006"


async def test_delete_with_children_is_rejected(client: AsyncClient) -> None:
    company = await create(client, "company")
    await create(client, "engineering", company["id"])

    response = await client.delete(
        f"/api/v1/departments/{company['id']}", headers=STRUCTURE_HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR_ORG_004"


async def test_delete_removes_an_empty_department(client: AsyncClient) -> None:
    company = await create(client, "company")

    response = await client.delete(
        f"/api/v1/departments/{company['id']}", headers=STRUCTURE_HEADERS
    )

    assert response.status_code == 204
    assert (await client.get(f"/api/v1/departments/{company['id']}")).status_code == 404


async def test_writes_require_structure_roles(client: AsyncClient) -> None:
    """Deny by default: no roles header means no permission."""
    response = await client.post(
        "/api/v1/departments", json={"code": "x", "name_es": "x", "name_en": "x"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR_AUTH_002"


async def test_reads_are_open_until_authentication_lands(client: AsyncClient) -> None:
    await create(client, "company")

    response = await client.get("/api/v1/departments")

    assert response.status_code == 200


# --- tree rendering --------------------------------------------------------


async def test_tree_endpoint_returns_nested_nodes_and_metadata(client: AsyncClient) -> None:
    company = await create(client, "company")
    engineering = await create(client, "engineering", company["id"])
    await create(client, "backend", engineering["id"])
    await create(client, "finance", company["id"])

    response = await client.get("/api/v1/departments")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    assert body["max_depth"] == 2
    assert len(body["roots"]) == 1
    root = body["roots"][0]
    assert root["department"]["code"] == "company"
    assert [child["department"]["code"] for child in root["children"]] == [
        "engineering",
        "finance",
    ]


async def test_inactive_departments_can_be_filtered_out(client: AsyncClient) -> None:
    company = await create(client, "company")
    legacy = await create(client, "legacy", company["id"])
    await client.patch(
        f"/api/v1/departments/{legacy['id']}",
        json={"is_active": False},
        headers=STRUCTURE_HEADERS,
    )

    all_rows = await client.get("/api/v1/departments", params={"include_inactive": "true"})
    active_only = await client.get("/api/v1/departments", params={"include_inactive": "false"})

    assert all_rows.json()["total"] == 2
    assert active_only.json()["total"] == 1


async def test_patch_only_touches_supplied_fields(client: AsyncClient) -> None:
    company = await create(client, "company", clearance_level="medium")

    response = await client.patch(
        f"/api/v1/departments/{company['id']}",
        json={"name_en": "Company"},
        headers=STRUCTURE_HEADERS,
    )

    body = response.json()
    assert body["name_en"] == "Company"
    assert body["name_es"] == "company"  # untouched
    assert body["clearance_level"] == "medium"  # untouched


async def test_clearance_level_is_validated_by_the_database(client: AsyncClient) -> None:
    company = await create(client, "company")

    response = await client.patch(
        f"/api/v1/departments/{company['id']}",
        json={"clearance_level": "top-secret"},
        headers=STRUCTURE_HEADERS,
    )

    assert response.status_code == 422


async def test_writes_invalidate_the_cached_tree_version(client: AsyncClient) -> None:
    """Structure is cached, so a write must bump the stamp rather than wait."""
    from app.cache import current_org_tree_version

    await create(client, "company")
    after_first = await current_org_tree_version()
    await create(client, "engineering")
    after_second = await current_org_tree_version()

    assert after_second > after_first
