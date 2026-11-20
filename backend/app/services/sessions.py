from app.modules import calendar_day_force_cut as day_cut

SESSION_COLUMNS = "id, visitor_id, started_at, ended_at, hit_count, status"


def _edges_for(conn, session_ids: list[str]) -> dict[str, list[dict]]:
    """取会话相关的日界边（权威快照），按 from/to 归到对应会话。"""
    if not session_ids:
        return {}
    placeholders = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        f"""
        SELECT * FROM day_boundary_edges
        WHERE from_session_id IN ({placeholders}) OR to_session_id IN ({placeholders})
        ORDER BY id
        """,
        (*session_ids, *session_ids),
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        base = {
            "edge_id": r["id"],
            "from_day": r["from_day"],
            "to_day": r["to_day"],
            "timezone": r["timezone"],
            "trigger_hit_id": r["trigger_hit_id"],
            "trigger_event_ts": r["trigger_event_ts"],
        }
        out.setdefault(r["from_session_id"], []).append(
            {**base, "role": "from", "peer_session_id": r["to_session_id"]}
        )
        out.setdefault(r["to_session_id"], []).append(
            {**base, "role": "to", "peer_session_id": r["from_session_id"]}
        )
    return out


def _calendar_day(row, edges: list[dict], tz_name: str) -> str | None:
    """展示用日：有边快照用快照（权威、不随时区设置漂移），否则按当前时区投影。

    open 会话代表「新」端取 D1，closed 会话代表「旧」端取 D0；角色缺失时退到
    可见的那一端，仍不读当前设置，避免展示日漂移。
    """
    is_open = row["status"] == "open"
    preferred = "to" if is_open else "from"
    for edge in reversed(edges):
        if edge["role"] == preferred:
            return edge["to_day"] if is_open else edge["from_day"]
    if edges:
        last = edges[-1]
        return last["to_day"] if is_open else edges[0]["from_day"]
    ts = row["ended_at"] or row["started_at"]
    try:
        return day_cut.calendar_day(ts, tz_name)
    except ValueError:
        return None


def list_sessions(conn, visitor_id: str | None = None):
    tz_name = day_cut.load_timezone(conn)
    if visitor_id:
        rows = conn.execute(
            f"SELECT {SESSION_COLUMNS} FROM sessions WHERE visitor_id=? ORDER BY started_at",
            (visitor_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {SESSION_COLUMNS} FROM sessions ORDER BY started_at"
        ).fetchall()
    edges = _edges_for(conn, [r["id"] for r in rows])
    return [
        {
            **dict(r),
            "day_boundaries": edges.get(r["id"], []),
            "calendar_day": _calendar_day(r, edges.get(r["id"], []), tz_name),
        }
        for r in rows
    ]


def session_detail(conn, session_id: str):
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        return None
    hits = conn.execute(
        """
        SELECT id, visitor_id, session_id, event_ts, page_path, event_name, channel
        FROM hits WHERE session_id=? AND channel='primary'
        ORDER BY event_ts, id
        """,
        (session_id,),
    ).fetchall()
    proof = conn.execute(
        "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    tz_name = day_cut.load_timezone(conn)
    edges = _edges_for(conn, [session_id]).get(session_id, [])
    return {
        "session": {
            **dict(sess),
            "day_boundaries": edges,
            "calendar_day": _calendar_day(sess, edges, tz_name),
        },
        "hits": [dict(h) for h in hits],
        "proof": dict(proof) if proof else None,
    }
