import sqlite3

from app.modules import calendar_day_force_cut as day_cut
from app.services import ingest as ingest_svc


def test_cross_day_cut_below_idle(db, write_batch):
    """末击点 D0、新点 D1，间隙 20 分钟（< 1800s），仍须强制切开。"""
    write_batch([
        {"id": "C1", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T15:50:00Z", "page_path": "/home", "event_name": "view"},
        {"id": "C2", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T16:10:00Z", "page_path": "/item/1", "event_name": "click"},
    ])
    result = ingest_svc.run_batch()
    assert result["ok"], result
    assert len(result["report"]["cut_edges"]) == 1

    sessions = db.execute(
        "SELECT * FROM sessions WHERE visitor_id='V-CROSS' ORDER BY started_at"
    ).fetchall()
    assert len(sessions) == 2
    assert [s["status"] for s in sessions] == ["closed", "open"]

    old, new = sessions
    old_members = [r["id"] for r in db.execute(
        "SELECT id FROM hits WHERE session_id=?", (old["id"],)
    )]
    new_members = [r["id"] for r in db.execute(
        "SELECT id FROM hits WHERE session_id=?", (new["id"],)
    )]
    assert old_members == ["C1"]
    assert new_members == ["C2"], "新点只能进新会话，不能进旧会话"

    edge = db.execute("SELECT * FROM day_boundary_edges").fetchone()
    assert edge["from_session_id"] == old["id"]
    assert edge["to_session_id"] == new["id"]
    assert edge["from_day"] == "2026-11-17"
    assert edge["to_day"] == "2026-11-18"
    assert edge["timezone"] == "Asia/Shanghai"
    assert edge["trigger_hit_id"] == "C2"

    verified = day_cut.verify_edge(db, edge)
    assert verified["ok"], verified["checks"]


def test_same_day_no_cut(db, write_batch):
    write_batch([
        {"id": "S1", "visitor_id": "V-SAME", "event_ts": "2026-11-17T02:00:00Z", "page_path": "/home", "event_name": "view"},
        {"id": "S2", "visitor_id": "V-SAME", "event_ts": "2026-11-17T02:20:00Z", "page_path": "/cart", "event_name": "view"},
    ])
    result = ingest_svc.run_batch()
    assert result["ok"], result
    assert result["report"]["cut_edges"] == []

    sessions = db.execute(
        "SELECT * FROM sessions WHERE visitor_id='V-SAME'"
    ).fetchall()
    assert len(sessions) == 1
    assert sessions[0]["hit_count"] == 2
    assert db.execute("SELECT COUNT(*) AS c FROM day_boundary_edges").fetchone()["c"] == 0


def test_single_open_invariant(db, write_batch):
    write_batch([
        {"id": "C1", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T15:50:00Z", "page_path": "/home", "event_name": "view"},
        {"id": "C2", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T16:10:00Z", "page_path": "/item/1", "event_name": "click"},
        {"id": "C3", "visitor_id": "V-CROSS", "event_ts": "2026-11-18T15:50:00Z", "page_path": "/home", "event_name": "view"},
    ])
    assert ingest_svc.run_batch()["ok"]
    dup = db.execute(
        """
        SELECT visitor_id, COUNT(*) AS c FROM sessions
        WHERE status='open' GROUP BY visitor_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    assert dup == []


def test_replay_is_idempotent(db, write_batch):
    write_batch([
        {"id": "C1", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T15:50:00Z", "page_path": "/home", "event_name": "view"},
        {"id": "C2", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T16:10:00Z", "page_path": "/item/1", "event_name": "click"},
    ])
    first = ingest_svc.run_batch()
    assert first["ok"]
    sessions_after_first = db.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
    edges_after_first = db.execute("SELECT COUNT(*) AS c FROM day_boundary_edges").fetchone()["c"]

    second = ingest_svc.run_batch()
    assert second["ok"]
    assert second["report"]["replayed"] == 2
    assert second["report"]["written"] == 0
    assert second["report"]["cut_edges"] == []
    assert db.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"] == sessions_after_first
    assert db.execute("SELECT COUNT(*) AS c FROM day_boundary_edges").fetchone()["c"] == edges_after_first


def test_edge_check_rejects_same_day(db):
    db.execute("INSERT INTO visitors(id,label,created_at) VALUES('V','v','x')")
    db.execute(
        "INSERT INTO sessions(id,visitor_id,started_at,ended_at,hit_count,status) VALUES('A','V','x','x',1,'closed')"
    )
    db.execute(
        "INSERT INTO sessions(id,visitor_id,started_at,ended_at,hit_count,status) VALUES('B','V','x','x',1,'open')"
    )
    db.execute(
        "INSERT INTO hits(id,visitor_id,session_id,event_ts,page_path,event_name) VALUES('h','V','B','x','/','v')"
    )
    try:
        db.execute(
            """
            INSERT INTO day_boundary_edges(
              visitor_id, from_session_id, to_session_id, from_day, to_day,
              timezone, trigger_hit_id, trigger_event_ts, created_at
            ) VALUES ('V','A','B','2026-11-17','2026-11-17','Asia/Shanghai','h','x','x')
            """
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("CHECK(from_day<>to_day) 未生效")


def test_plan_cut_timezone_sensitivity():
    assert day_cut.plan_cut("2026-11-17T15:50:00Z", "2026-11-17T16:10:00Z", "Asia/Shanghai") == {
        "from_day": "2026-11-17",
        "to_day": "2026-11-18",
    }
    assert day_cut.plan_cut("2026-11-17T15:50:00Z", "2026-11-17T16:10:00Z", "UTC") is None


def test_seed_splits_cross_day_hits(write_seed):
    """种子入口同样按「访客 × 日」建会话，绝不让夹具跨日点落进同一会话。"""
    conn = write_seed(
        [{"id": "V-S", "label": "种子跨日", "created_at": "2026-11-17T08:00:00Z"}],
        [
            {"id": "D1", "visitor_id": "V-S", "event_ts": "2026-11-17T15:50:00Z", "page_path": "/home", "event_name": "view"},
            {"id": "D2", "visitor_id": "V-S", "event_ts": "2026-11-17T16:10:00Z", "page_path": "/cart", "event_name": "view"},
        ],
    )
    try:
        sessions = conn.execute(
            "SELECT id, status FROM sessions WHERE visitor_id='V-S' ORDER BY started_at"
        ).fetchall()
        assert [(s["status"]) for s in sessions] == ["closed", "open"]
        edge = conn.execute("SELECT * FROM day_boundary_edges").fetchone()
        assert edge["from_session_id"] == sessions[0]["id"]
        assert edge["to_session_id"] == sessions[1]["id"]
        assert edge["from_day"] == "2026-11-17"
        assert edge["to_day"] == "2026-11-18"
        assert edge["trigger_hit_id"] == "D2"
        assert day_cut.verify_edge(conn, edge)["ok"] is True
    finally:
        conn.close()


def test_three_day_chain_display_days(db, write_batch):
    """连续三日：中间会话既是上一条边的「新」、又是下一条边的「旧」，展示日不得串。"""
    from app.services import sessions as sess_svc

    write_batch([
        {"id": "T1", "visitor_id": "V-CHAIN", "event_ts": "2026-11-17T15:50:00Z", "page_path": "/a", "event_name": "view"},
        {"id": "T2", "visitor_id": "V-CHAIN", "event_ts": "2026-11-18T15:50:00Z", "page_path": "/b", "event_name": "view"},
        {"id": "T3", "visitor_id": "V-CHAIN", "event_ts": "2026-11-19T15:50:00Z", "page_path": "/c", "event_name": "view"},
    ])
    assert ingest_svc.run_batch()["ok"]

    rows = sess_svc.list_sessions(db, "V-CHAIN")
    assert [r["status"] for r in rows] == ["closed", "closed", "open"]
    assert [r["calendar_day"] for r in rows] == ["2026-11-17", "2026-11-18", "2026-11-19"]
    assert len(rows[1]["day_boundaries"]) == 2

    edges = db.execute("SELECT * FROM day_boundary_edges ORDER BY id").fetchall()
    assert len(edges) == 2
    for edge in edges:
        assert day_cut.verify_edge(db, edge)["ok"]


def test_cut_failure_rolls_back_whole_batch(db, write_batch, monkeypatch):
    """日界切换任一步失败 → 整批回滚，正式会话/边表保持原样（失败闭）。"""
    write_batch([
        {"id": "F1", "visitor_id": "V-FAIL", "event_ts": "2026-11-17T15:50:00Z", "page_path": "/a", "event_name": "view"},
        {"id": "F2", "visitor_id": "V-FAIL", "event_ts": "2026-11-17T16:10:00Z", "page_path": "/b", "event_name": "view"},
    ])

    def _boom(*args, **kwargs):
        raise RuntimeError("edge insert failed")

    monkeypatch.setattr(day_cut, "record_edge", _boom)
    result = ingest_svc.run_batch()
    assert result["ok"] is False
    assert "edge insert failed" in result["error"]

    assert db.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM hits").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM day_boundary_edges").fetchone()["c"] == 0
