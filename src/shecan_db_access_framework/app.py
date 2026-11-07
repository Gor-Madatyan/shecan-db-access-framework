from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
import os
from pathlib import Path
import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

from shecan_db_access_framework.assets import router as assets_router
from shecan_db_access_framework.products import router as products_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    sqlite_path = Path(os.getenv("SQLITE_DB_PATH", "dbs.db")).resolve()
    with closing(sqlite3.connect(f"{sqlite_path.as_uri()}?mode=ro", uri=True)) as registry:
        rows = registry.execute("SELECT db_id, sheurl, dbtoken FROM dbs").fetchall()

    with MongoClient(
        os.getenv("MONGODB_URI", "mongodb://localhost:27017"), connect=False
    ) as client:
        app.state.dbs = {
            db_id: {
                "db_id": db_id,
                "sheurl": sheurl,
                "dbtoken": dbtoken,
                "database": client[dbtoken],
            }
            for db_id, sheurl, dbtoken in rows
        }
        try:
            for dbtoken in {entry["dbtoken"] for entry in app.state.dbs.values()}:
                client[dbtoken]["assets"].create_index("asset_name", unique=True)
                client[dbtoken]["products"].create_index("product_name", unique=True)
            yield
        finally:
            app.state.dbs.clear()


app = FastAPI(title="Shecan DB Access Framework", version="0.1.0", lifespan=lifespan)
app.include_router(assets_router)
app.include_router(products_router)


@app.exception_handler(DuplicateKeyError)
async def duplicate_asset(request: Request, exc: DuplicateKeyError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "Asset name already exists"})


@app.exception_handler(PyMongoError)
async def mongodb_error(request: Request, exc: PyMongoError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "MongoDB operation failed"})


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
