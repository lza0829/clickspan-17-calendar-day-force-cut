import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules import calendar_day_force_cut as day_cut


def run_batch() -> dict:
    """装载一批击点：同日挂现有 open；跨日强制切（未超 idle 也切）。

    整批一个事务：任一访客切开失败（边表约束/单 open 索引/解析失败）整批回滚，
    不留下"部分访客已切"的半成品。
    """
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    hits = payload.get("hits", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        tz_name = day_cut.load_timezone(conn)
        ordered = _order_hits(hits)
        written = 0
        replayed = 0
        rejected = []
        cut_edges = []

        for hit in ordered:
            vid = hit["visitor_id"]
            _ensure_visitor(conn, vid, hit.get("visitor_label"))

            try:
                day_cut.parse_event_ts(hit["event_ts"])
            except (ValueError, KeyError):
                rejected.append({"id": hit.get("id"), "reason": "event_ts 无法解析"})
                continue

            if _already_seen(conn, hit):
                replayed += 1
                continue

            sess = conn.execute(
                """
                SELECT id, started_at, ended_at FROM sessions
                WHERE visitor_id=? AND status='open'
                ORDER BY started_at DESC LIMIT 1
                """,
                (vid,),
            ).fetchone()

            edge = None
            sid = None
            if sess:
                last_ts = _last_primary_ts(conn, sess["id"]) or sess["ended_at"] or sess["started_at"]
                cut = day_cut.plan_cut(last_ts, hit["event_ts"], tz_name)
                if cut:
                    sid = _force_cut(
                        conn,
                        visitor_id=vid,
                        old_session_id=sess["id"],
                        new_event_ts=hit["event_ts"],
                    )
                    edge = {"cut": cut, "from_session_id": sess["id"]}
                else:
                    sid = sess["id"]
            else:
                sid = f"S-{vid}-{uuid.uuid4().hex[:6]}"
                conn.execute(
                    """
                    INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
                    VALUES (?,?,?,?,0,'open')
                    """,
                    (sid, vid, hit["event_ts"], hit["event_ts"]),
                )

            attached = _attach_hit(conn, vid, sid, hit)
            if edge and not attached:
                # 已经切了旧会话却没接住新点，会留下空的新 open 且无日界边；
                # 宁可整批回滚也不留半成品。
                raise RuntimeError(f"日界切开后新点 {hit.get('id')} 未能落库")
            if attached:
                written += 1
                _refresh_session_and_proof(conn, sid, vid, hit["event_ts"])
                if edge:
                    # 边表 trigger_hit_id 外键指向 hits，必须在新点落库之后再写；
                    # 仍在同一事务内，失败即整批回滚。
                    day_cut.record_edge(
                        conn,
                        visitor_id=vid,
                        from_session_id=edge["from_session_id"],
                        to_session_id=sid,
                        from_day=edge["cut"]["from_day"],
                        to_day=edge["cut"]["to_day"],
                        timezone_name=tz_name,
                        trigger_hit_id=hit["id"],
                        trigger_event_ts=hit["event_ts"],
                        created_at=_now(),
                    )
                    cut_edges.append(
                        {
                            "visitor_id": vid,
                            "from_session_id": edge["from_session_id"],
                            "to_session_id": sid,
                            **edge["cut"],
                            "timezone": tz_name,
                            "trigger_hit_id": hit["id"],
                        }
                    )

        report = {
            "timezone": tz_name,
            "batch_id": batch_id,
            "written": written,
            "replayed": replayed,
            "rejected": rejected,
            "cut_edges": cut_edges,
            "input": len(hits),
        }
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _order_hits(hits: list[dict]) -> list[dict]:
    def key(h):
        try:
            ts = day_cut.parse_event_ts(h.get("event_ts", ""))
        except ValueError:
            return (datetime.max.replace(tzinfo=timezone.utc), str(h.get("id")))

        return (ts, str(h.get("id")))

    return sorted(hits, key=key)


def _ensure_visitor(conn, visitor_id: str, label: str | None):
    exists = conn.execute("SELECT 1 FROM visitors WHERE id=?", (visitor_id,)).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
            (visitor_id, label or visitor_id, _now()),
        )


def _already_seen(conn, hit: dict) -> bool:
    by_id = conn.execute("SELECT 1 FROM hits WHERE id=?", (hit["id"],)).fetchone()
    if by_id:
        return True
    by_key = conn.execute(
        """
        SELECT 1 FROM hits
        WHERE visitor_id=? AND event_ts=? AND page_path=? AND event_name=?
        """,
        (hit["visitor_id"], hit["event_ts"], hit["page_path"], hit["event_name"]),
    ).fetchone()
    return bool(by_key)


def _last_primary_ts(conn, session_id: str) -> str | None:
    rows = conn.execute(
        "SELECT event_ts FROM hits WHERE session_id=? AND channel='primary'",
        (session_id,),
    ).fetchall()
    best_dt = None
    best_ts = None
    for row in rows:
        try:
            dt = day_cut.parse_event_ts(row["event_ts"])
        except ValueError:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_ts = row["event_ts"]
    return best_ts


def _force_cut(conn, *, visitor_id, old_session_id, new_event_ts) -> str:
    """关旧、开新；两步同事务，中间态对外不可见。边行由调用方在新点落库后写。"""
    cur = conn.execute(
        "UPDATE sessions SET status='closed' WHERE id=? AND status='open'",
        (old_session_id,),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"旧会话 {old_session_id} 已非 open，拒绝硬切")

    new_sid = f"S-{visitor_id}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        """
        INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
        VALUES (?,?,?,?,0,'open')
        """,
        (new_sid, visitor_id, new_event_ts, new_event_ts),
    )
    return new_sid


def _attach_hit(conn, visitor_id: str, session_id: str, hit: dict) -> bool:
    conn.execute(
        """
        INSERT OR IGNORE INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
        VALUES (?,?,?,?,?,?, 'primary')
        """,
        (hit["id"], visitor_id, session_id, hit["event_ts"], hit["page_path"], hit["event_name"]),
    )
    return conn.execute("SELECT changes()").fetchone()[0] > 0


def _refresh_session_and_proof(conn, session_id: str, visitor_id: str, event_ts: str):
    rows = conn.execute(
        "SELECT event_ts FROM hits WHERE session_id=? AND channel='primary'",
        (session_id,),
    ).fetchall()
    ended_at = event_ts
    best = None
    for row in rows:
        try:
            dt = day_cut.parse_event_ts(row["event_ts"])
        except ValueError:
            continue
        if best is None or dt > best:
            best = dt
            ended_at = row["event_ts"]
    conn.execute(
        """
        UPDATE sessions
        SET hit_count = hit_count + 1,
            ended_at = ?
        WHERE id=?
        """,
        (ended_at, session_id),
    )
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
            (session_id,),
        ).fetchall()
    ]
    sess_row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    conn.execute(
        """
        INSERT INTO session_proofs(
          session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            session_id,
            visitor_id,
            json.dumps(ids, ensure_ascii=False),
            sess_row["hit_count"],
            sess_row["started_at"],
            sess_row["ended_at"],
            _now(),
        ),
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
