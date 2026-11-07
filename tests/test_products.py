import json
import unittest
from unittest.mock import MagicMock
from urllib.parse import urlencode

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError

from shecan_db_access_framework.app import app


class ProductTests(unittest.IsolatedAsyncioTestCase):
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
        self.product_id = ObjectId()
        self.payload = {
            "product_name": "Logo",
            "product_description": "Company logo",
            "primary_photo_url": "https://example.com/logo.png",
            "metadata": {"width": 128, "labels": ["logo", None, 42]},
        }
        self.document = {**self.payload, "_id": self.product_id}
        self.filter = {"_id": self.product_id}

    async def request(self, method, path="/products", payload=None, **query):
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

    async def test_crud_preserves_extra_fields_in_selected_database(self) -> None:
        self.collection.insert_one.return_value.inserted_id = self.product_id
        status, created = await self.request("POST", payload=self.payload, sheurl=self.sheurl)
        self.assertEqual(status, 201)
        self.assertEqual(created, {**self.document, "_id": str(self.product_id)})
        self.collection.insert_one.assert_called_once_with(self.document)
        self.database.__getitem__.assert_called_with("products")

        self.collection.find_one.return_value = self.document
        path = f"/products/{self.product_id}"
        self.assertEqual(await self.request("GET", path, sheurl=self.sheurl), (200, created))
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
        replacement["product_name"] = "New logo"
        stored = replacement
        self.collection.find_one_and_replace.return_value = {**stored, "_id": self.product_id}
        self.assertEqual(
            await self.request("PUT", path, replacement, sheurl=self.sheurl),
            (200, {**stored, "_id": str(self.product_id)}),
        )
        self.collection.find_one_and_replace.assert_called_once_with(
            self.filter, stored, return_document=ReturnDocument.AFTER
        )

        self.collection.delete_one.return_value.deleted_count = 1
        self.assertEqual(
            await self.request("DELETE", sheurl=self.sheurl, product_name=replacement["product_name"]),
            (204, None),
        )
        self.collection.delete_one.assert_called_once_with(
            {"product_name": replacement["product_name"]}
        )
        self.collection.delete_one.reset_mock()
        self.assertEqual(await self.request("DELETE", path, sheurl=self.sheurl), (204, None))
        self.collection.delete_one.assert_called_once_with(self.filter)
        self.other_database.__getitem__.assert_not_called()

    async def test_routes_to_other_database(self) -> None:
        other_url = "https://other.example.com"
        other_collection = self.other_database.__getitem__.return_value
        other_collection.find_one.return_value = {**self.document}
        status, body = await self.request("GET", f"/products/{self.product_id}", sheurl=other_url)
        self.assertEqual(status, 200)
        self.assertEqual(body, {**self.document, "_id": str(self.product_id)})
        other_collection.find_one.assert_called_once_with({"_id": self.product_id})
        self.database.__getitem__.assert_not_called()

    async def test_invalid_payloads_never_reach_mongodb(self) -> None:
        invalid_payloads = [[], None, {**self.payload, "_id": "custom"}, {**self.payload, "$set": {}}]
        for field in ("product_name", "product_description", "primary_photo_url"):
            invalid_payloads.append({key: value for key, value in self.payload.items() if key != field})
            invalid_payloads.extend({**self.payload, field: value} for value in (None, "", 42, {}))
        invalid_payloads.extend([{**self.payload, "count": 2**100}, {**self.payload, "bad\x00key": True}])
        for method, path in [("POST", "/products"), ("PUT", f"/products/{self.product_id}")]:
            for payload in invalid_payloads:
                with self.subTest(method=method, payload=payload):
                    status, _ = await self.request(method, path, payload, sheurl=self.sheurl)
                    self.assertEqual(status, 422)
        self.database.__getitem__.assert_not_called()

    async def test_unknown_and_ambiguous_sites_fail_without_database_access(self) -> None:
        for expected_status, site in [(404, "https://unknown.example.com"), (409, self.sheurl)]:
            if expected_status == 409:
                app.state.dbs[3] = {"sheurl": self.sheurl, "dbtoken": "conflict", "database": self.other_database}
            for method, path in [("GET", "/products"), ("GET", f"/products/{self.product_id}"), ("DELETE", "/products"), ("DELETE", f"/products/{self.product_id}")]:
                query = {"product_name": "Logo"} if method == "DELETE" else {}
                status, _ = await self.request(method, path, sheurl=site, **query)
                self.assertEqual(status, expected_status)
            for method, path in [("POST", "/products"), ("PUT", f"/products/{self.product_id}")]:
                status, _ = await self.request(method, path, self.payload, sheurl=site)
                self.assertEqual(status, expected_status)
        self.database.__getitem__.assert_not_called()
        self.other_database.__getitem__.assert_not_called()

    async def test_missing_products_and_invalid_ids(self) -> None:
        self.collection.find_one.return_value = None
        self.collection.find_one_and_replace.return_value = None
        self.collection.delete_one.return_value.deleted_count = 0
        for method in ("GET", "PUT", "DELETE"):
            with self.subTest(method=method):
                body = self.payload if method == "PUT" else None
                query = {"sheurl": self.sheurl}
                status, _ = await self.request(method, f"/products/{self.product_id}", body, **query)
                self.assertEqual(status, 404)
                self.collection.reset_mock()
                status, _ = await self.request(method, "/products/invalid", body, **query)
                self.assertEqual(status, 422)
                self.assertEqual(self.collection.mock_calls, [])

        status, _ = await self.request("DELETE", sheurl=self.sheurl, product_name="Missing")
        self.assertEqual(status, 404)
        self.collection.delete_one.assert_called_once_with(
            {"product_name": "Missing"}
        )

    async def test_shared_database_queries_use_same_collection(self) -> None:
        other_url = "https://shared.example.com"
        app.state.dbs[3] = {"sheurl": other_url, "dbtoken": "example_db", "database": self.database}
        self.collection.find_one.return_value = self.document
        self.collection.find_one_and_replace.return_value = self.document
        self.collection.delete_one.return_value.deleted_count = 1
        path = f"/products/{self.product_id}"
        for method in ("GET", "PUT", "DELETE"):
            payload = self.payload if method == "PUT" else None
            status, body = await self.request(method, path, payload, sheurl=other_url)
            self.assertEqual(status, 204 if method == "DELETE" else 200)
            if method != "DELETE":
                self.assertEqual(body, {**self.document, "_id": str(self.product_id)})
        self.collection.find_one.assert_called_once_with(self.filter)
        self.collection.find_one_and_replace.assert_called_once_with(
            self.filter, self.payload, return_document=ReturnDocument.AFTER
        )
        self.collection.delete_one.assert_called_once_with(self.filter)

    async def test_repeated_mapping_to_same_database_is_accepted(self) -> None:
        app.state.dbs[3] = {"sheurl": self.sheurl, "dbtoken": "example_db", "database": self.database}
        self.collection.find_one.return_value = self.document
        status, body = await self.request("GET", f"/products/{self.product_id}", sheurl=self.sheurl)
        self.assertEqual(status, 200)
        self.assertEqual(body, {**self.document, "_id": str(self.product_id)})

    async def test_delete_requires_name_and_handles_special_characters(self) -> None:
        for query in [{}, {"sheurl": self.sheurl}, {"sheurl": self.sheurl, "product_name": ""}, {"product_name": "Logo"}]:
            status, _ = await self.request("DELETE", **query)
            self.assertEqual(status, 422)
        self.collection.delete_one.assert_not_called()
        name = "Logo / header? & footer"
        self.collection.delete_one.return_value.deleted_count = 1
        self.assertEqual(
            await self.request("DELETE", sheurl=self.sheurl, product_name=name),
            (204, None),
        )
        self.collection.delete_one.assert_called_once_with(
            {"product_name": name}
        )

    async def test_duplicate_product_name_returns_conflict(self) -> None:
        self.collection.insert_one.side_effect = DuplicateKeyError("private index details")
        self.collection.find_one_and_replace.side_effect = DuplicateKeyError("private index details")
        for method, path in [("POST", "/products"), ("PUT", f"/products/{self.product_id}")]:
            with self.subTest(method=method):
                self.assertEqual(
                    await self.request(method, path, self.payload, sheurl=self.sheurl),
                    (409, {"detail": "Product name already exists"}),
                )

    async def test_required_query_and_pagination_bounds(self) -> None:
        for query in [{}, {"sheurl": self.sheurl, "limit": 0}, {"sheurl": self.sheurl, "limit": 1001}, {"sheurl": self.sheurl, "skip": -1}]:
            status, _ = await self.request("GET", **query)
            self.assertEqual(status, 422)
        for method in ("GET", "DELETE"):
            status, _ = await self.request(method, f"/products/{self.product_id}")
            self.assertEqual(status, 422)
        self.database.__getitem__.assert_not_called()

    async def test_mongodb_failure_does_not_expose_connection_details(self) -> None:
        self.collection.insert_one.side_effect = ConnectionFailure("private connection details")
        self.assertEqual(
            await self.request("POST", payload=self.payload, sheurl=self.sheurl),
            (503, {"detail": "MongoDB operation failed"}),
        )

    async def test_sheurl_query_required_on_every_endpoint(self) -> None:
        path = f"/products/{self.product_id}"
        routes = [("POST", "/products"), ("GET", "/products"), ("GET", path), ("PUT", path), ("DELETE", "/products"), ("DELETE", path)]
        for method, path in routes:
            query = {"product_name": "Logo"} if method == "DELETE" and path == "/products" else {}
            payload = self.payload if method in ("POST", "PUT") else None
            for routing in [{}, {"sheurl": ""}]:
                with self.subTest(method=method, path=path, routing=routing):
                    status, _ = await self.request(method, path, payload, **query, **routing)
                    self.assertEqual(status, 422)
        self.database.__getitem__.assert_not_called()

    async def test_query_sheurl_controls_writes(self) -> None:
        other_url = "https://other.example.com"
        collection = self.other_database.__getitem__.return_value
        collection.insert_one.return_value.inserted_id = self.product_id
        stored = self.payload
        collection.find_one_and_replace.return_value = {**stored, "_id": self.product_id}
        for method, path in [("POST", "/products"), ("PUT", f"/products/{self.product_id}")]:
            status, body = await self.request(method, path, self.payload, sheurl=other_url)
            self.assertEqual(status, 201 if method == "POST" else 200)
            self.assertEqual(body, {**stored, "_id": str(self.product_id)})
        collection.insert_one.assert_called_once_with({**stored, "_id": self.product_id})
        collection.find_one_and_replace.assert_called_once_with(
            {"_id": self.product_id}, stored, return_document=ReturnDocument.AFTER
        )
        self.database.__getitem__.assert_not_called()

    async def test_sheurl_in_body_is_rejected(self) -> None:
        for method, path in [("POST", "/products"), ("PUT", f"/products/{self.product_id}")]:
            for site in (self.sheurl, "https://other.example.com", None):
                with self.subTest(method=method, site=site):
                    status, _ = await self.request(
                        method, path, {**self.payload, "sheurl": site}, sheurl=self.sheurl
                    )
                    self.assertEqual(status, 422)
        self.database.__getitem__.assert_not_called()
        self.other_database.__getitem__.assert_not_called()
