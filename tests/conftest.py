"""pytest 公共夹具：每个测试使用独立临时 SQLite 数据库。"""
import os
import pathlib
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="sample_test_")
os.environ.setdefault("SAMPLE_DB_PATH", str(pathlib.Path(_tmp) / "boot.db"))

from app import database  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    db_path = os.path.join(_tmp, f"t_{next(tempfile._get_candidate_names())}.db")
    database.init_db(db_path)

    def _override():
        conn = database.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[database.get_conn] = _override
    with TestClient(app) as c:
        c.db_path = db_path
        yield c
    app.dependency_overrides.clear()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass
