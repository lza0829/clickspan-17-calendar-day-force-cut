"""自然日界强制切开：按可配置 IANA 时区把 event_ts 投影成日历日。

新点日历日 != 当前 open 末击点日历日时，**不论间隙是否已超 idle**，关闭旧
open 并用新会话承接新点；裁决落 ``day_boundary_edges``（独立日界边表，
记录旧/新会话、D0/D1、时区快照与触发 hit）。新点只写进新会话，旧会话从未
接收次日成员，因此不存在"先写后挪"的中间态。

同日点不走本刀，仍交给既有空闲切窗。
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"
STATUS = "implemented"


def validate_timezone(name: str) -> str:
    """校验 IANA 时区名；非法一律抛 ValueError（路由层转 400）。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("时区名不能为空")
    key = name.strip()
    try:
        ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"未知 IANA 时区: {key}") from exc
    return key


def load_timezone(conn) -> str:
    """读裁决时区；缺省 Asia/Shanghai。非法配置直接失败闭，不静默改默认。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key='calendar_timezone'"
    ).fetchone()
    if not row:
        return DEFAULT_TIMEZONE
    return validate_timezone(row["value"])


def parse_event_ts(event_ts: str) -> datetime:
    """解析 ISO8601 时刻为 UTC；无偏移按 UTC 解释。解析失败抛 ValueError。"""
    if not isinstance(event_ts, str) or not event_ts.strip():
        raise ValueError("event_ts 不能为空")
    raw = event_ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"无法解析 event_ts: {event_ts}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def calendar_day(event_ts: str, tz_name: str) -> str:
    """把时刻投影为指定时区下的日历日（YYYY-MM-DD）。"""
    tz = ZoneInfo(validate_timezone(tz_name))
    return parse_event_ts(event_ts).astimezone(tz).date().isoformat()


def plan_cut(last_hit_ts: str, new_hit_ts: str, tz_name: str) -> dict | None:
    """日历日不同即切；返回 D0/D1，同日返回 None。"""
    from_day = calendar_day(last_hit_ts, tz_name)
    to_day = calendar_day(new_hit_ts, tz_name)
    if from_day == to_day:
        return None
    return {"from_day": from_day, "to_day": to_day}


def split_hits_by_day(hits: list[dict], tz_name: str) -> dict[str, list[tuple[str, list[dict]]]]:
    """按「访客 × 日历日」分组，供种子与夹具复用，避免任何跨日会话。

    返回 {visitor_id: [(day, [hits...]), ...]}，日组按时间先后排列。
    """
    ordered = sorted(hits, key=lambda h: (parse_event_ts(h["event_ts"]), h["id"]))
    grouped: dict[str, list[tuple[str, list[dict]]]] = {}
    for hit in ordered:
        day = calendar_day(hit["event_ts"], tz_name)
        days = grouped.setdefault(hit["visitor_id"], [])
        if not days or days[-1][0] != day:
            days.append((day, []))
        days[-1][1].append(hit)
    return grouped


def record_edge(
    conn,
    *,
    visitor_id: str,
    from_session_id: str,
    to_session_id: str,
    from_day: str,
    to_day: str,
    timezone_name: str,
    trigger_hit_id: str,
    trigger_event_ts: str,
    created_at: str,
) -> dict:
    """写日界边。刻意用裸 INSERT：OR IGNORE 会吞掉 CHECK(from_day<>to_day)。"""
    if from_day == to_day:
        raise ValueError("日界两端必须是不同的日历日")
    cur = conn.execute(
        """
        INSERT INTO day_boundary_edges(
          visitor_id, from_session_id, to_session_id, from_day, to_day,
          timezone, trigger_hit_id, trigger_event_ts, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            visitor_id,
            from_session_id,
            to_session_id,
            from_day,
            to_day,
            timezone_name,
            trigger_hit_id,
            trigger_event_ts,
            created_at,
        ),
    )
    row = conn.execute(
        "SELECT * FROM day_boundary_edges WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    return dict(row)


def _session_last_day(conn, session_id: str, tz_name: str) -> str | None:
    rows = conn.execute(
        "SELECT event_ts FROM hits WHERE session_id=? AND channel='primary'",
        (session_id,),
    ).fetchall()
    best_dt = None
    best_day = None
    for row in rows:
        try:
            dt = parse_event_ts(row["event_ts"])
            day = calendar_day(row["event_ts"], tz_name)
        except ValueError:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_day = day
    return best_day


def verify_edge(conn, edge, current_timezone: str | None = None) -> dict:
    """只读核证：一律按边表快照的 timezone 重算，不看当前设置。"""
    edge = dict(edge)
    tz_name = edge["timezone"]
    checks: dict[str, bool] = {}

    try:
        validate_timezone(tz_name)
        checks["timezone_valid"] = True
    except ValueError:
        checks["timezone_valid"] = False

    checks["days_distinct"] = edge["from_day"] != edge["to_day"]

    def day_of(ts: str) -> str | None:
        try:
            return calendar_day(ts, tz_name)
        except ValueError:
            return None

    last_day = None
    if checks["timezone_valid"]:
        last_day = _session_last_day(conn, edge["from_session_id"], tz_name)
    checks["from_day_matches_last_hit"] = (
        last_day is not None and last_day == edge["from_day"]
    )

    trigger = conn.execute(
        "SELECT id, session_id, event_ts FROM hits WHERE id=?",
        (edge["trigger_hit_id"],),
    ).fetchone()
    trigger_day = day_of(trigger["event_ts"]) if trigger else None
    checks["trigger_in_to_session"] = bool(
        trigger and trigger["session_id"] == edge["to_session_id"]
    )
    checks["to_day_matches_trigger"] = trigger_day == edge["to_day"]

    from_sess = conn.execute(
        "SELECT status FROM sessions WHERE id=?", (edge["from_session_id"],)
    ).fetchone()
    checks["from_session_closed"] = bool(from_sess and from_sess["status"] == "closed")

    stale = conn.execute(
        "SELECT event_ts FROM hits WHERE session_id=?",
        (edge["from_session_id"],),
    ).fetchall()
    checks["from_session_has_no_to_day_hit"] = all(
        day_of(r["event_ts"]) != edge["to_day"] for r in stale
    )

    open_count = conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE visitor_id=? AND status='open'",
        (edge["visitor_id"],),
    ).fetchone()["c"]
    checks["single_open_per_visitor"] = open_count <= 1

    if current_timezone is None:
        try:
            current_timezone = load_timezone(conn)
        except ValueError:
            current_timezone = None
    drift = {
        "snapshot": tz_name,
        "current": current_timezone,
        "differs": current_timezone is not None and current_timezone != tz_name,
    }

    return {
        "id": edge["id"],
        "visitor_id": edge["visitor_id"],
        "from_session_id": edge["from_session_id"],
        "to_session_id": edge["to_session_id"],
        "from_day": edge["from_day"],
        "to_day": edge["to_day"],
        "timezone": tz_name,
        "trigger_hit_id": edge["trigger_hit_id"],
        "trigger_event_ts": edge["trigger_event_ts"],
        "created_at": edge["created_at"],
        "checks": checks,
        "ok": all(checks.values()),
        "timezone_drift": drift,
    }
