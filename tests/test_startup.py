import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pymongo import MongoClient
from pymongo.errors import InvalidName, OperationFailure

from shecan_db_access_framework.app import app, health


class StartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sqlite_path = Path(directory.name) / "registry.sqlite3"
        with sqlite3.connect(self.sqlite_path) as registry:
            registry.executescript(
                (Path(__file__).resolve().parents[1] / "init_db.sql").read_text()
            )
        environment = patch.dict(os.environ, {"SQLITE_DB_PATH": str(self.sqlite_path)})
        environment.start()
        self.addCleanup(environment.stop)
        self.client = MongoClient("mongodb://localhost:27017", connect=False)
        self.addCleanup(self.client.close)
        factory = patch("shecan_db_access_framework.app.MongoClient", return_value=self.client)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        indexes = patch("pymongo.collection.Collection.create_index", autospec=True)
        self.indexes = indexes.start()
        self.addCleanup(indexes.stop)

    async def test_loads_all_entries_and_closes_shared_client(self) -> None:
        rows = [
            (1, "https://example.com", "example_db"),
            (2, "https://example.com", "other_db"),
            (3, "https://other.example.com", "example_db"),
        ]
        with sqlite3.connect(self.sqlite_path) as registry:
            registry.executemany("INSERT INTO dbs VALUES (?, ?, ?)", rows)

        with patch.object(self.client, "close", wraps=self.client.close) as close:
            async with app.router.lifespan_context(app):
                self.assertEqual(len(app.state.dbs), len(rows))
                for db_id, sheurl, dbtoken in rows:
                    entry = app.state.dbs[db_id]
                    self.assertEqual(
                        (entry["db_id"], entry["sheurl"], entry["dbtoken"]),
                        (db_id, sheurl, dbtoken),
                    )
                    self.assertEqual(entry["database"].name, dbtoken)
                    self.assertIs(entry["database"].client, self.client)
                self.assertEqual(await health(), {"status": "ok"})
            close.assert_called_once()
        self.assertEqual(app.state.dbs, {})
        self.assertEqual(self.indexes.call_count, 2)
        self.assertEqual(
            {call.args[0].database.name for call in self.indexes.call_args_list},
            {"example_db", "other_db"},
        )
        for call in self.indexes.call_args_list:
            self.assertEqual(call.args[0].name, "assets")
            self.assertEqual(call.args[1:], ("asset_name",))
            self.assertEqual(call.kwargs, {"unique": True})

    async def test_empty_registry(self) -> None:
        async with app.router.lifespan_context(app):
            self.assertEqual(app.state.dbs, {})
        self.indexes.assert_not_called()

    async def test_index_failure_prevents_startup_and_cleans_up(self) -> None:
        with sqlite3.connect(self.sqlite_path) as registry:
            registry.execute(
                "INSERT INTO dbs (sheurl, dbtoken) VALUES (?, ?)",
                ("https://example.com", "example_db"),
            )
        self.indexes.side_effect = OperationFailure("Existing duplicate names")
        with patch.object(self.client, "close", wraps=self.client.close) as close:
            with self.assertRaises(OperationFailure):
                async with app.router.lifespan_context(app):
                    self.fail("Startup accepted an index creation failure")
            close.assert_called_once()
        self.assertEqual(app.state.dbs, {})

    async def test_missing_registry_is_not_created(self) -> None:
        missing_path = self.sqlite_path.with_name("missing.sqlite3")
        with patch.dict(os.environ, {"SQLITE_DB_PATH": str(missing_path)}):
            with self.assertRaises(sqlite3.OperationalError):
                async with app.router.lifespan_context(app):
                    self.fail("Startup accepted a missing registry")
        self.assertFalse(missing_path.exists())
        self.factory.assert_not_called()

    async def test_invalid_database_name_fails_startup_and_closes_client(self) -> None:
        with sqlite3.connect(self.sqlite_path) as registry:
            registry.execute(
                "INSERT INTO dbs (sheurl, dbtoken) VALUES (?, ?)",
                ("https://example.com", "invalid/name"),
            )
        with patch.object(self.client, "close", wraps=self.client.close) as close:
            with self.assertRaises(InvalidName):
                async with app.router.lifespan_context(app):
                    self.fail("Startup accepted an invalid MongoDB database name")
            close.assert_called_once()
