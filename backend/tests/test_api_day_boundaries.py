from fastapi.testclient import TestClient

from app.main import app


def _client():
    return TestClient(app)


def test_invalid_timezone_rejected(db):
    client = _client()
    for bad in ["", "Not/AZone", "../etc/passwd"]:
        res = client.put("/api/settings/calendar-timezone", json={"calendar_timezone": bad})
        assert res.status_code == 400, (bad, res.status_code, res.text)


def test_valid_timezone_saved(db):
    client = _client()
    res = client.put("/api/settings/calendar-timezone", json={"calendar_timezone": "UTC"})
    assert res.status_code == 200
    assert res.json()["calendar_timezone"] == "UTC"
    assert client.get("/api/settings").json()["calendar_timezone"] == "UTC"


def test_day_boundaries_endpoint_and_verify(db, write_batch):
    write_batch([
        {"id": "C1", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T15:50:00Z", "page_path": "/home", "event_name": "view"},
        {"id": "C2", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T16:10:00Z", "page_path": "/item/1", "event_name": "click"},
    ])
    client = _client()
    from app.services import ingest as ingest_svc

    assert ingest_svc.run_batch()["ok"]

    rows = client.get("/api/day-boundaries").json()
    assert len(rows) == 1
    row = rows[0]
    for key in (
        "visitor_id",
        "from_session_id",
        "to_session_id",
        "from_day",
        "to_day",
        "timezone",
        "trigger_hit_id",
        "trigger_event_ts",
    ):
        assert row[key], key
    assert row["ok"] is True
    assert client.get(f"/api/day-boundaries/{row['id']}").json()["ok"] is True

    sessions = client.get("/api/sessions", params={"visitor_id": "V-CROSS"}).json()
    assert len(sessions) == 2
    assert {s["calendar_day"] for s in sessions} == {"2026-11-17", "2026-11-18"}
    assert all(s["day_boundaries"] for s in sessions)


def test_timezone_snapshot_tamper_fails_verify(db, write_batch):
    write_batch([
        {"id": "C1", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T15:50:00Z", "page_path": "/home", "event_name": "view"},
        {"id": "C2", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T16:10:00Z", "page_path": "/item/1", "event_name": "click"},
    ])
    from app.services import ingest as ingest_svc

    assert ingest_svc.run_batch()["ok"]
    client = _client()
    edge = client.get("/api/day-boundaries").json()[0]
    assert edge["ok"] is True

    db.execute("UPDATE day_boundary_edges SET timezone='UTC' WHERE id=?", (edge["id"],))
    db.commit()
    tampered = client.get(f"/api/day-boundaries/{edge['id']}").json()
    assert tampered["ok"] is False
    assert tampered["checks"]["to_day_matches_trigger"] is False


def test_tz_change_does_not_retro_cut_or_hide_drift(db, write_batch):
    write_batch([
        {"id": "C1", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T15:50:00Z", "page_path": "/home", "event_name": "view"},
        {"id": "C2", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T16:10:00Z", "page_path": "/item/1", "event_name": "click"},
    ])
    from app.services import ingest as ingest_svc

    assert ingest_svc.run_batch()["ok"]
    client = _client()
    edge_id = client.get("/api/day-boundaries").json()[0]["id"]

    assert client.put("/api/settings/calendar-timezone", json={"calendar_timezone": "UTC"}).status_code == 200

    edge = client.get(f"/api/day-boundaries/{edge_id}").json()
    assert edge["ok"] is True, "改设置不得让旧边核证失败"
    assert edge["timezone_drift"]["differs"] is True

    # 2026-11-17T20:00:00Z = 上海 11-18 04:00（跨日），UTC 仍是 11-17（同日）。
    # 切成 UTC 之后，同一新点不应再触发日界切开。
    write_batch([
        {"id": "C3", "visitor_id": "V-CROSS", "event_ts": "2026-11-17T20:00:00Z", "page_path": "/home", "event_name": "view"},
    ], batch_id="B-test-2")
    result = ingest_svc.run_batch()
    assert result["ok"]
    assert result["report"]["cut_edges"] == []
