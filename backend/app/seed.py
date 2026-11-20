import json

from app.db import FIXTURES_DIR, connect
from app.modules import calendar_day_force_cut as day_cut

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visitors (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  hit_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS hits (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  session_id TEXT REFERENCES sessions(id),
  event_ts TEXT NOT NULL,
  page_path TEXT NOT NULL,
  event_name TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'primary',
  UNIQUE(visitor_id, event_ts, page_path, event_name)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  visitor_id TEXT NOT NULL,
  hit_ids_json TEXT NOT NULL,
  hit_count INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  created_at TEXT NOT NULL
);

-- 独立日界边表：日界裁决的唯一权威落库形态。
-- 记录旧/新会话、D0/D1、裁决所用 IANA 时区快照与触发 hit。
CREATE TABLE IF NOT EXISTS day_boundary_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  from_session_id TEXT NOT NULL REFERENCES sessions(id),
  to_session_id TEXT NOT NULL REFERENCES sessions(id),
  from_day TEXT NOT NULL,
  to_day TEXT NOT NULL,
  timezone TEXT NOT NULL,
  trigger_hit_id TEXT NOT NULL REFERENCES hits(id),
  trigger_event_ts TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK (from_day <> to_day),
  UNIQUE (visitor_id, trigger_hit_id)
);

-- 单 open 兜底：任何写路径都不允许同访客两条 open（日界切换先关旧再开新）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_one_open
  ON sessions(visitor_id) WHERE status='open';
"""


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    for key, val in (
        ("idle_timeout_sec", "1800"),
        ("calendar_timezone", day_cut.DEFAULT_TIMEZONE),
    ):
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)", (key, val))
    n = conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
    conn.commit()
    conn.close()


def _seed_fixtures(conn):
    fixture = FIXTURES_DIR / "seed_hits.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for v in data.get("visitors", []):
        conn.execute(
            "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
            (v["id"], v["label"], v["created_at"]),
        )
    tz_name = day_cut.load_timezone(conn)
    # 基座：按「访客 × 日历日」建会话。相邻日组之间落日界边，任何入口都不产生跨日会话。
    for visitor_id, day_groups in day_cut.split_hits_by_day(data.get("hits", []), tz_name).items():
        session_ids = []
        for idx, (day, group) in enumerate(day_groups, start=1):
            sid = f"S-{visitor_id}-{idx}"
            session_ids.append(sid)
            is_last = idx == len(day_groups)
            conn.execute(
                "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status) VALUES (?,?,?,?,?,?)",
                (
                    sid,
                    visitor_id,
                    group[0]["event_ts"],
                    group[-1]["event_ts"],
                    len(group),
                    "open" if is_last else "closed",
                ),
            )
            hit_ids = []
            for h in group:
                conn.execute(
                    """
                    INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        h["id"],
                        visitor_id,
                        sid,
                        h["event_ts"],
                        h["page_path"],
                        h["event_name"],
                        h.get("channel", "primary"),
                    ),
                )
                hit_ids.append(h["id"])
            conn.execute(
                """
                INSERT INTO session_proofs(session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at)
                VALUES (?,?,?,?,?,?,datetime('now'))
                """,
                (
                    sid,
                    visitor_id,
                    json.dumps(hit_ids, ensure_ascii=False),
                    len(hit_ids),
                    group[0]["event_ts"],
                    group[-1]["event_ts"],
                ),
            )
        for idx in range(1, len(day_groups)):
            prev_day = day_groups[idx - 1][0]
            day, group = day_groups[idx]
            day_cut.record_edge(
                conn,
                visitor_id=visitor_id,
                from_session_id=session_ids[idx - 1],
                to_session_id=session_ids[idx],
                from_day=prev_day,
                to_day=day,
                timezone_name=tz_name,
                trigger_hit_id=group[0]["id"],
                trigger_event_ts=group[0]["event_ts"],
                created_at=_utc_now(),
            )


def idle_timeout_sec(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    return int(row["value"]) if row else 1800


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
