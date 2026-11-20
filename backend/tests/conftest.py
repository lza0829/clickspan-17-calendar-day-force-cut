import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="clickspan-test-"))
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["FIXTURES_DIR"] = str(_TMP / "fixtures")
(_TMP / "fixtures").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from app.seed import init_db  # noqa: E402


@pytest.fixture()
def db():
    path = Path(os.environ["DATA_DIR"]) / "clickspan.db"
    if path.exists():
        path.unlink()
    # 种子里若残留别的用例写下的 seed_hits.json，会污染基线；统一清掉。
    seed_fixture = Path(os.environ["FIXTURES_DIR"]) / "seed_hits.json"
    if seed_fixture.exists():
        seed_fixture.unlink()
    init_db()
    conn = connect()
    yield conn
    conn.close()


@pytest.fixture()
def write_batch():
    def _write(hits, batch_id="B-test"):
        path = Path(os.environ["FIXTURES_DIR"]) / "ingest_batch.json"
        path.write_text(
            json.dumps({"batch_id": batch_id, "hits": hits}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    return _write


@pytest.fixture()
def write_seed():
    """写 seed_hits.json 并从空库重建，用于验证种子入口同样按日切分。"""

    def _write(visitors, hits):
        (Path(os.environ["FIXTURES_DIR"]) / "seed_hits.json").write_text(
            json.dumps({"visitors": visitors, "hits": hits}, ensure_ascii=False),
            encoding="utf-8",
        )
        path = Path(os.environ["DATA_DIR"]) / "clickspan.db"
        if path.exists():
            path.unlink()
        init_db()
        conn = connect()
        # 交付连接前清掉夹具，避免污染后续用例的基线种子。
        (Path(os.environ["FIXTURES_DIR"]) / "seed_hits.json").unlink(missing_ok=True)
        return conn

    return _write
