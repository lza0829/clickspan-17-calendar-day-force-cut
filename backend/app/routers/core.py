from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules import calendar_day_force_cut as day_cut
from app.seed import idle_timeout_sec
from app.services import ingest as ingest_svc
from app.services import sessions as sess_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "visitors": conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"],
            "sessions": conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"],
            "hits_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hits WHERE channel='primary'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
            "idle_timeout_sec": idle_timeout_sec(conn),
            "calendar_timezone": day_cut.load_timezone(conn),
            "day_boundaries": conn.execute(
                "SELECT COUNT(*) AS c FROM day_boundary_edges"
            ).fetchone()["c"],
        }
    finally:
        conn.close()


@router.get("/visitors")
def list_visitors():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, label, created_at FROM visitors ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sessions")
def list_sessions(visitor_id: str | None = None):
    conn = connect()
    try:
        return sess_svc.list_sessions(conn, visitor_id)
    finally:
        conn.close()


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    conn = connect()
    try:
        detail = sess_svc.session_detail(conn, session_id)
        if not detail:
            raise HTTPException(404, "会话不存在")
        return detail
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, visitor_id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/session-proofs")
def session_proofs(session_id: str | None = None):
    conn = connect()
    try:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM session_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {
            "idle_timeout_sec": idle_timeout_sec(conn),
            "calendar_timezone": day_cut.load_timezone(conn),
        }
    finally:
        conn.close()


@router.put("/settings/idle-timeout")
def put_idle_timeout(body: dict):
    sec = int(body.get("idle_timeout_sec", 1800))
    if sec < 60:
        raise HTTPException(400, "空闲超时至少 60 秒")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            (str(sec),),
        )
        conn.commit()
        return {"idle_timeout_sec": sec}
    finally:
        conn.close()


@router.put("/settings/calendar-timezone")
def put_calendar_timezone(body: dict):
    try:
        tz_name = day_cut.validate_timezone(body.get("calendar_timezone", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('calendar_timezone', ?)",
            (tz_name,),
        )
        conn.commit()
        return {"calendar_timezone": tz_name, "retroactive": False}
    finally:
        conn.close()


def _edge_payload(conn, row) -> dict:
    return day_cut.verify_edge(conn, row)


@router.get("/day-boundaries")
def list_day_boundaries(visitor_id: str | None = None, session_id: str | None = None):
    conn = connect()
    try:
        clauses = []
        params: list[str] = []
        if visitor_id:
            clauses.append("visitor_id=?")
            params.append(visitor_id)
        if session_id:
            clauses.append("(from_session_id=? OR to_session_id=?)")
            params.extend([session_id, session_id])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM day_boundary_edges {where} ORDER BY id",
            params,
        ).fetchall()
        return [_edge_payload(conn, r) for r in rows]
    finally:
        conn.close()


@router.get("/day-boundaries/{edge_id}")
def get_day_boundary(edge_id: int):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM day_boundary_edges WHERE id=?", (edge_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "日界边不存在")
        return _edge_payload(conn, row)
    finally:
        conn.close()
