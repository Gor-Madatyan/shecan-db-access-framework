import json
import unittest
from unittest.mock import MagicMock
from urllib.parse import urlencode

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError

from shecan_db_access_framework.app import app


class AssetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        previous = getattr(app.state, "dbs", {})
        self.addCleanup(setattr, app.state, "dbs", previous)
        self.database = MagicMock()
        self.collection = self.database.__getitem__.return_value
        self.other_database = MagicMock()
        self.sheurl = "https://example.com"
        app.state.dbs = {
            1: {"sheurl": self.sheurl, "dbtoken": "example_db", "database": self.database},
            2: {"sheurl": "https://other.example.com", "dbtoken": "other_db", "database": self.other_database},
        }
        self.asset_id = ObjectId()
        self.payload = {
            "asset_name": "Logo",
            "asset_url": "https://example.com/logo.png",
            "metadata": {"width": 128, "labels": ["logo", None, 42]},
        }
        self.document = {**self.payload, "_id": self.asset_id}
        self.filter = {"asset_name": self.payload["asset_name"]}

    async def request(self, method, path="/assets", payload=None, **query):
        messages = []

        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode()}

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "http",
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "query_string": urlencode(query).encode(),
                "headers": [(b"content-type", b"application/json")],
            },
            receive,
            send,
        )
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        return status, json.loads(body) if body else None

    def test_openapi_places_asset_name_by_method(self) -> None:
        schema = app.openapi()
        for method in ("post", "get", "put", "delete"):
            operation = schema["paths"]["/assets"][method]
            parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
            self.assertEqual(parameters["sheurl"]["in"], "query")
            self.assertTrue(parameters["sheurl"]["required"])
            if method in ("get", "delete"):
                self.assertNotIn("requestBody", operation)
                self.assertEqual(parameters["asset_name"]["in"], "query")
                self.assertEqual(parameters["asset_name"]["required"], method == "delete")
                continue
            self.assertNotIn("asset_name", parameters)
            body = operation["requestBody"]["content"]["application/json"]["schema"]
            if "anyOf" in body:
                body = next(option for option in body["anyOf"] if "$ref" in option)
            fields = schema["components"]["schemas"][body["$ref"].split("/")[-1]]["properties"]
            self.assertIn("asset_name", fields)
            self.assertNotIn("sheurl", fields)

    async def test_sheurl_in_body_is_rejected(self) -> None:
        for method in ("POST", "PUT"):
            for site in (self.sheurl, "https://other.example.com", None):
                with self.subTest(method=method, site=site):
                    status, _ = await self.request(
                        method, payload={**self.payload, "sheurl": site}, sheurl=self.sheurl
                    )
                    self.assertEqual(status, 422)
        self.database.__getitem__.assert_not_called()
        self.other_database.__getitem__.assert_not_called()

    async def test_query_asset_name_controls_reads_and_deletes(self) -> None:
        self.collection.find_one.return_value = self.document
        self.collection.delete_one.return_value.deleted_count = 1
        for method in ("GET", "DELETE"):
            status, _ = await self.request(
                method, payload={"asset_name": "Other", "sheurl": "https://other.example.com"},
                sheurl=self.sheurl, asset_name="Logo",
            )
            self.assertEqual(status, 204 if method == "DELETE" else 200)
        self.collection.find_one.assert_called_once_with(self.filter)
        self.collection.delete_one.assert_called_once_with(self.filter)
        self.collection.find.assert_not_called()
        self.other_database.__getitem__.assert_not_called()

    async def test_query_asset_name_cannot_override_body(self) -> None:
        self.collection.find_one_and_replace.return_value = self.document
        status, _ = await self.request("PUT", payload=self.payload, sheurl=self.sheurl, asset_name="Other")
        self.assertEqual(status, 200)
        self.collection.find_one_and_replace.assert_called_once_with(
            self.filter, self.payload, upsert=True, return_document=ReturnDocument.AFTER
        )

    async def test_crud_preserves_extra_fields_in_selected_database(self) -> None:
        self.collection.insert_one.return_value.inserted_id = self.asset_id
        status, created = await self.request("POST", payload=self.payload, sheurl=self.sheurl)
        self.assertEqual(status, 201)
        self.assertEqual(created, {**self.document, "_id": str(self.asset_id)})
        self.collection.insert_one.assert_called_once_with(self.document)
        self.database.__getitem__.assert_called_with("assets")

        self.collection.find_one.return_value = self.document
        self.assertEqual(
            await self.request("GET", sheurl=self.sheurl, asset_name=self.payload["asset_name"]),
            (200, created),
        )
        self.collection.find_one.assert_called_once_with(self.filter)

        cursor = self.collection.find.return_value
        cursor.skip.return_value.limit.return_value = [self.document]
        self.assertEqual(
            await self.request("GET", sheurl=self.sheurl, skip=2, limit=5),
            (200, [created]),
        )
        self.collection.find.assert_called_once_with({})
        cursor.skip.assert_called_once_with(2)
        cursor.skip.return_value.limit.assert_called_once_with(5)

        replacement = {key: value for key, value in self.payload.items() if key != "metadata"}
        stored = replacement
        self.collection.find_one_and_replace.return_value = {**stored, "_id": self.asset_id}
        self.assertEqual(
            await self.request("PUT", payload=replacement, sheurl=self.sheurl),
            (200, {**stored, "_id": str(self.asset_id)}),
        )
        self.collection.find_one_and_replace.assert_called_once_with(
            {"asset_name": self.payload["asset_name"]},
            stored,
            upsert=True, return_document=ReturnDocument.AFTER,
        )

        self.collection.delete_one.return_value.deleted_count = 1
        self.assertEqual(
            await self.request("DELETE", sheurl=self.sheurl, asset_name=replacement["asset_name"]),
            (204, None),
        )
        self.collection.delete_one.assert_called_once_with(
            {"asset_name": replacement["asset_name"]}
        )
        self.other_database.__getitem__.assert_not_called()

    async def test_routes_to_other_database(self) -> None:
        other_url = "https://other.example.com"
        other_collection = self.other_database.__getitem__.return_value
        other_collection.find_one.return_value = {**self.document}
        status, body = await self.request("GET", sheurl=other_url, asset_name=self.payload["asset_name"])
        self.assertEqual(status, 200)
        self.assertEqual(body, {**self.document, "_id": str(self.asset_id)})
        other_collection.find_one.assert_called_once_with(
            {"asset_name": self.payload["asset_name"]}
        )
        self.database.__getitem__.assert_not_called()

    async def test_invalid_payloads_never_reach_mongodb(self) -> None:
        invalid_payloads = [[], None, {**self.payload, "_id": "custom"}, {**self.payload, "$set": {}}]
        for field in ("asset_name", "asset_url"):
            invalid_payloads.append({key: value for key, value in self.payload.items() if key != field})
            invalid_payloads.extend({**self.payload, field: value} for value in (None, "", 42, {}))
        invalid_payloads.extend([{**self.payload, "count": 2**100}, {**self.payload, "bad\x00key": True}])
        for method in ("POST", "PUT"):
            for payload in invalid_payloads:
                with self.subTest(method=method, payload=payload):
                    status, _ = await self.request(method, payload=payload, sheurl=self.sheurl)
                    self.assertEqual(status, 422)
        self.database.__getitem__.assert_not_called()

    async def test_unknown_and_ambiguous_sites_fail_without_database_access(self) -> None:
        for expected_status, site in [(404, "https://unknown.example.com"), (409, self.sheurl)]:
            if expected_status == 409:
                app.state.dbs[3] = {"sheurl": self.sheurl, "dbtoken": "conflict", "database": self.other_database}
            for method, query in [("GET", {}), ("GET", {"asset_name": "Logo"}), ("DELETE", {"asset_name": "Logo"})]:
                status, _ = await self.request(method, sheurl=site, **query)
                self.assertEqual(status, expected_status)
            for method in ("POST", "PUT"):
                status, _ = await self.request(method, payload=self.payload, sheurl=site)
                self.assertEqual(status, expected_status)
        self.database.__getitem__.assert_not_called()
        self.other_database.__getitem__.assert_not_called()

    async def test_put_creates_missing_asset(self) -> None:
        payload = {**self.payload, "asset_name": "Missing"}
        self.collection.find_one_and_replace.return_value = {**payload, "_id": self.asset_id}
        self.assertEqual(
            await self.request("PUT", payload=payload, sheurl=self.sheurl),
            (200, {**payload, "_id": str(self.asset_id)}),
        )
        self.collection.find_one_and_replace.assert_called_once_with(
            {"asset_name": "Missing"}, payload,
            upsert=True, return_document=ReturnDocument.AFTER,
        )
        self.collection.insert_one.assert_not_called()

    async def test_missing_assets(self) -> None:
        self.collection.find_one.return_value = None
        self.collection.delete_one.return_value.deleted_count = 0
        status, _ = await self.request("GET", sheurl=self.sheurl, asset_name="Missing")
        self.assertEqual(status, 404)
        self.collection.find_one.assert_called_once_with(
            {"asset_name": "Missing"}
        )

        status, _ = await self.request("DELETE", sheurl=self.sheurl, asset_name="Missing")
        self.assertEqual(status, 404)
        self.collection.delete_one.assert_called_once_with(
            {"asset_name": "Missing"}
        )

    async def test_delete_requires_name_and_handles_special_characters(self) -> None:
        for query in [{}, {"sheurl": self.sheurl}, {"sheurl": self.sheurl, "asset_name": ""}, {"asset_name": "Logo"}]:
            status, _ = await self.request("DELETE", payload={"asset_name": "Logo"}, **query)
            self.assertEqual(status, 422)
        self.collection.delete_one.assert_not_called()
        name = "Logo / header? & footer"
        self.collection.delete_one.return_value.deleted_count = 1
        self.assertEqual(
            await self.request("DELETE", sheurl=self.sheurl, asset_name=name),
            (204, None),
        )
        self.collection.delete_one.assert_called_once_with(
            {"asset_name": name}
        )

    async def test_duplicate_asset_name_returns_conflict(self) -> None:
        self.collection.insert_one.side_effect = DuplicateKeyError("private index details")
        self.collection.find_one_and_replace.side_effect = DuplicateKeyError("private index details")
        for method in ("POST", "PUT"):
            with self.subTest(method=method):
                self.assertEqual(
                    await self.request(method, payload=self.payload, sheurl=self.sheurl),
                    (409, {"detail": "Asset name already exists"}),
                )

    async def test_put_requires_name_and_handles_special_characters(self) -> None:
        for payload in [{key: value for key, value in self.payload.items() if key != "asset_name"}, {**self.payload, "asset_name": ""}]:
            status, _ = await self.request("PUT", payload=payload, sheurl=self.sheurl)
            self.assertEqual(status, 422)
        self.collection.find_one_and_replace.assert_not_called()

        name = "Logo / header? & footer"
        payload = {**self.payload, "asset_name": name}
        stored = payload
        self.collection.find_one_and_replace.return_value = {**stored, "_id": self.asset_id}
        status, updated = await self.request("PUT", payload=payload, sheurl=self.sheurl)
        self.assertEqual(status, 200)
        self.assertEqual(updated, {**stored, "_id": str(self.asset_id)})
        self.collection.find_one_and_replace.assert_called_once_with(
            {"asset_name": name},
            stored,
            upsert=True, return_document=ReturnDocument.AFTER,
        )

    async def test_get_name_validation_and_special_characters(self) -> None:
        for query in [{"sheurl": self.sheurl, "asset_name": ""}, {"asset_name": "Logo"}]:
            status, _ = await self.request("GET", **query)
            self.assertEqual(status, 422)
        self.database.__getitem__.assert_not_called()

        name = "Logo / header? & footer"
        self.collection.find_one.return_value = {**self.document, "asset_name": name}
        self.assertEqual(
            await self.request("GET", sheurl=self.sheurl, asset_name=name),
            (200, {**self.document, "asset_name": name, "_id": str(self.asset_id)}),
        )
        self.collection.find_one.assert_called_once_with(
            {"asset_name": name}
        )
        self.collection.find.assert_not_called()

    async def test_asset_id_routes_are_no_longer_supported(self) -> None:
        for method in ("GET", "PUT"):
            status, _ = await self.request(method, f"/assets/{self.asset_id}", self.payload, sheurl=self.sheurl)
            self.assertEqual(status, 404)
        self.database.__getitem__.assert_not_called()

    async def test_required_query_and_pagination_bounds(self) -> None:
        for query in [{}, {"sheurl": self.sheurl, "limit": 0}, {"sheurl": self.sheurl, "limit": 1001}, {"sheurl": self.sheurl, "skip": -1}]:
            status, _ = await self.request("GET", **query)
            self.assertEqual(status, 422)
        self.database.__getitem__.assert_not_called()

    async def test_mongodb_failure_does_not_expose_connection_details(self) -> None:
        self.collection.insert_one.side_effect = ConnectionFailure("private connection details")
        self.assertEqual(
            await self.request("POST", payload=self.payload, sheurl=self.sheurl),
            (503, {"detail": "MongoDB operation failed"}),
        )

    async def test_sheurl_query_required_on_every_endpoint(self) -> None:
        for method in ("POST", "GET", "PUT", "DELETE"):
            payload = self.payload if method in ("POST", "PUT") else None
            query = {"asset_name": "Logo"} if method in ("GET", "DELETE") else {}
            for routing in [{}, {"sheurl": ""}]:
                with self.subTest(method=method, routing=routing):
                    status, _ = await self.request(method, payload=payload, **query, **routing)
                    self.assertEqual(status, 422)
        self.database.__getitem__.assert_not_called()

    async def test_query_sheurl_controls_writes(self) -> None:
        other_url = "https://other.example.com"
        collection = self.other_database.__getitem__.return_value
        collection.insert_one.return_value.inserted_id = self.asset_id
        stored = self.payload
        collection.find_one_and_replace.return_value = {**stored, "_id": self.asset_id}
        for method in ("POST", "PUT"):
            status, body = await self.request(method, payload=self.payload, sheurl=other_url)
            self.assertEqual(status, 201 if method == "POST" else 200)
            self.assertEqual(body, {**stored, "_id": str(self.asset_id)})
        collection.insert_one.assert_called_once_with({**stored, "_id": self.asset_id})
        collection.find_one_and_replace.assert_called_once_with(
            {"asset_name": "Logo"}, stored, upsert=True, return_document=ReturnDocument.AFTER
        )
        self.database.__getitem__.assert_not_called()
