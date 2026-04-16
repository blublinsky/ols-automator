"""Unit tests for the OpenAPI specification."""

import json

import pytest


EXPECTED_ENDPOINTS = (
    "/api/v1/events",
    "/api/v1/items",
    "/api/v1/items/{key}",
    "/api/v1/items/{key}/failed",
    "/api/v1/items/{key}/review",
    "/readiness",
    "/liveness",
)

EXPECTED_SCHEMAS = (
    "Event",
    "EventResponse",
    "FailedItemActionRequest",
    "FailedItemActionResponse",
    "ReviewRequest",
    "ReviewResponse",
    "WorkItemSummary",
    "WorkItemDetail",
)


@pytest.mark.asyncio
class TestOpenAPIEndpoint:
    async def test_openapi_returns_ok(self, client):
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200

    async def test_openapi_has_required_metadata(self, client):
        schema = (await client.get("/openapi.json")).json()
        for key in ("openapi", "info", "paths", "components"):
            assert key in schema, f"Missing top-level key: {key}"

    async def test_openapi_info_description(self, client):
        info = (await client.get("/openapi.json")).json()["info"]
        assert "description" in info
        assert "OLS Automator" in info["title"]

    async def test_openapi_license(self, client):
        info = (await client.get("/openapi.json")).json()["info"]
        assert info["license"]["name"] == "Apache 2.0"

    async def test_all_endpoints_documented(self, client):
        paths = (await client.get("/openapi.json")).json()["paths"]
        for endpoint in EXPECTED_ENDPOINTS:
            assert endpoint in paths, f"Endpoint {endpoint} not in OpenAPI spec"

    async def test_all_schemas_present(self, client):
        schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]
        for name in EXPECTED_SCHEMAS:
            assert name in schemas, f"Schema {name} not in OpenAPI spec"


@pytest.mark.asyncio
class TestOpenAPISchemaFile:
    async def test_checked_in_schema_is_up_to_date(self, client):
        """Verify docs/openapi.json matches the live schema.

        Fails when endpoints or models change but ``make schema`` was
        not re-run before commit.
        """
        with open("docs/openapi.json", encoding="utf-8") as f:
            on_disk = json.load(f)

        live = (await client.get("/openapi.json")).json()
        assert live == on_disk, (
            "docs/openapi.json is out of date — run `make schema` to regenerate"
        )
