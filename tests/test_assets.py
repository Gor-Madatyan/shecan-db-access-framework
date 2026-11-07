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
            "sheurl": self.sheurl,
            "asset_name": "Logo",
            "asset_url": "https://example.com/logo.png",
            "metadata": {"width": 128, "labels": ["logo", None, 42]},
        }
        self.document = {**self.payload, "_id": self.asset_id}
        self.filter = {"_id": self.asset_id, "sheurl": self.sheurl}

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

    async def test_crud_preserves_extra_fields_and_scopes_every_query(self) -> None:
        self.collection.insert_one.return_value.inserted_id = self.asset_id
        status, created = await self.request("POST", payload=self.payload)
        self.assertEqual(status, 201)
        self.assertEqual(created, {**self.payload, "_id": str(self.asset_id)})
        self.collection.insert_one.assert_called_once_with(self.document)
        self.database.__getitem__.assert_called_with("assets")

        self.collection.find_one.return_value = self.document
        path = f"/assets/{self.asset_id}"
        self.assertEqual(await self.request("GET", path, sheurl=self.sheurl), (200, created))
        self.collection.find_one.assert_called_once_with(self.filter)

        cursor = self.collection.find.return_value
        cursor.skip.return_value.limit.return_value = [self.document]
        self.assertEqual(
            await self.request("GET", sheurl=self.sheurl, skip=2, limit=5),
            (200, [created]),
        )
        self.collection.find.assert_called_once_with({"sheurl": self.sheurl})
        cursor.skip.assert_called_once_with(2)
        cursor.skip.return_value.limit.assert_called_once_with(5)

        replacement = {key: value for key, value in self.payload.items() if key != "metadata"}
        replacement["asset_name"] = "New logo"
        self.collection.find_one_and_replace.return_value = {**replacement, "_id": self.asset_id}
        self.assertEqual(
            await self.request("PUT", path, replacement),
            (200, {**replacement, "_id": str(self.asset_id)}),
        )
        self.collection.find_one_and_replace.assert_called_once_with(
            self.filter, replacement, return_document=ReturnDocument.AFTER
        )

        self.collection.delete_one.return_value.deleted_count = 1
        self.assertEqual(
            await self.request("DELETE", sheurl=self.sheurl, asset_name=replacement["asset_name"]),
            (204, None),
        )
        self.collection.delete_one.assert_called_once_with(
            {"asset_name": replacement["asset_name"], "sheurl": self.sheurl}
        )
        self.other_database.__getitem__.assert_not_called()

    async def test_routes_to_other_database(self) -> None:
        other_url = "https://other.example.com"
        other_collection = self.other_database.__getitem__.return_value
        other_collection.find_one.return_value = {**self.document, "sheurl": other_url}
        status, body = await self.request("GET", f"/assets/{self.asset_id}", sheurl=other_url)
        self.assertEqual(status, 200)
        self.assertEqual(body["sheurl"], other_url)
        other_collection.find_one.assert_called_once_with({"_id": self.asset_id, "sheurl": other_url})
        self.database.__getitem__.assert_not_called()

    async def test_invalid_payloads_never_reach_mongodb(self) -> None:
        invalid_payloads = [[], None, {**self.payload, "_id": "custom"}, {**self.payload, "$set": {}}]
        for field in ("sheurl", "asset_name", "asset_url"):
            invalid_payloads.append({key: value for key, value in self.payload.items() if key != field})
            invalid_payloads.extend({**self.payload, field: value} for value in (None, "", 42, {}))
        invalid_payloads.extend([{**self.payload, "count": 2**100}, {**self.payload, "bad\x00key": True}])
        for method, path in [("POST", "/assets"), ("PUT", f"/assets/{self.asset_id}")]:
            for payload in invalid_payloads:
                with self.subTest(method=method, payload=payload):
                    status, _ = await self.request(method, path, payload)
                    self.assertEqual(status, 422)
        self.database.__getitem__.assert_not_called()

    async def test_unknown_and_ambiguous_sites_fail_without_database_access(self) -> None:
        for expected_status, site in [(404, "https://unknown.example.com"), (409, self.sheurl)]:
            if expected_status == 409:
                app.state.dbs[3] = {"sheurl": self.sheurl, "dbtoken": "conflict", "database": self.other_database}
            for method, path in [("GET", "/assets"), ("GET", f"/assets/{self.asset_id}"), ("DELETE", "/assets")]:
                query = {"asset_name": "Logo"} if method == "DELETE" else {}
                status, _ = await self.request(method, path, sheurl=site, **query)
                self.assertEqual(status, expected_status)
            for method, path in [("POST", "/assets"), ("PUT", f"/assets/{self.asset_id}")]:
                status, _ = await self.request(method, path, {**self.payload, "sheurl": site})
                self.assertEqual(status, expected_status)
        self.database.__getitem__.assert_not_called()
        self.other_database.__getitem__.assert_not_called()

    async def test_missing_assets_and_invalid_ids(self) -> None:
        self.collection.find_one.return_value = None
        self.collection.find_one_and_replace.return_value = None
        self.collection.delete_one.return_value.deleted_count = 0
        for method in ("GET", "PUT"):
            with self.subTest(method=method):
                body = self.payload if method == "PUT" else None
                query = {} if method == "PUT" else {"sheurl": self.sheurl}
                status, _ = await self.request(method, f"/assets/{self.asset_id}", body, **query)
                self.assertEqual(status, 404)
                self.collection.reset_mock()
                status, _ = await self.request(method, "/assets/invalid", body, **query)
                self.assertEqual(status, 422)
                self.assertEqual(self.collection.mock_calls, [])

        status, _ = await self.request("DELETE", sheurl=self.sheurl, asset_name="Missing")
        self.assertEqual(status, 404)
        self.collection.delete_one.assert_called_once_with(
            {"asset_name": "Missing", "sheurl": self.sheurl}
        )

    async def test_delete_requires_name_and_handles_special_characters(self) -> None:
        for query in [{}, {"sheurl": self.sheurl}, {"sheurl": self.sheurl, "asset_name": ""}, {"asset_name": "Logo"}]:
            status, _ = await self.request("DELETE", **query)
            self.assertEqual(status, 422)
        self.collection.delete_one.assert_not_called()
        name = "Logo / header? & footer"
        self.collection.delete_one.return_value.deleted_count = 1
        self.assertEqual(
            await self.request("DELETE", sheurl=self.sheurl, asset_name=name),
            (204, None),
        )
        self.collection.delete_one.assert_called_once_with(
            {"asset_name": name, "sheurl": self.sheurl}
        )

    async def test_duplicate_asset_name_returns_conflict(self) -> None:
        self.collection.insert_one.side_effect = DuplicateKeyError("private index details")
        self.collection.find_one_and_replace.side_effect = DuplicateKeyError("private index details")
        for method, path in [("POST", "/assets"), ("PUT", f"/assets/{self.asset_id}")]:
            with self.subTest(method=method):
                self.assertEqual(
                    await self.request(method, path, self.payload),
                    (409, {"detail": "Asset name already exists"}),
                )

    async def test_required_query_and_pagination_bounds(self) -> None:
        for query in [{}, {"sheurl": self.sheurl, "limit": 0}, {"sheurl": self.sheurl, "limit": 1001}, {"sheurl": self.sheurl, "skip": -1}]:
            status, _ = await self.request("GET", **query)
            self.assertEqual(status, 422)
        self.database.__getitem__.assert_not_called()

    async def test_mongodb_failure_does_not_expose_connection_details(self) -> None:
        self.collection.insert_one.side_effect = ConnectionFailure("private connection details")
        self.assertEqual(
            await self.request("POST", payload=self.payload),
            (503, {"detail": "MongoDB operation failed"}),
        )
