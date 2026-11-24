"""Transaction-bound services: ledger writes and summary rebuilds.

Every write path updates the ledger and the life-table summary in a single
SQLite transaction. If any step fails the transaction is rolled back and the
previous ledger and summary stay intact. Query helpers only touch the
summary tables.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone

from .lifetable import EVENT_TYPES, PumpRecord, build_life_table, incidence_at
from .db import init_db

LIFE_TABLE_COLUMNS = (
    "t_hours", "entered", "at_risk", "failures", "preventive", "censored",
    "hazard_failure", "hazard_preventive", "survival",
    "cum_failure", "cum_preventive",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rebuild_summary(conn: sqlite3.Connection, records: list[PumpRecord]) -> None:
    rows = build_life_table(records)
    conn.execute("DELETE FROM life_table")
    placeholders = ",".join("?" for _ in LIFE_TABLE_COLUMNS)
    conn.executemany(
        f"INSERT INTO life_table ({','.join(LIFE_TABLE_COLUMNS)}) "
        f"VALUES ({placeholders})",
        [tuple(row[col] for col in LIFE_TABLE_COLUMNS) for row in rows],
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('pump_count', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(len(records)),),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('summary_updated_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_now(),),
    )


def _load_all_records(conn: sqlite3.Connection) -> list[PumpRecord]:
    return [
        PumpRecord(r["pump_id"], r["entry_hours"], r["exit_hours"], r["event_type"])
        for r in conn.execute(
            "SELECT pump_id, entry_hours, exit_hours, event_type FROM ledger"
        )
    ]


def _ledger_generation(conn: sqlite3.Connection) -> int:
    """台账代际：整表替换一次递增一次；单泵更正不影响。"""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'ledger_generation'"
    ).fetchone()
    return int(row["value"]) if row else 0


def _pump_fingerprint(conn: sqlite3.Connection, pump_id: str) -> str:
    """该泵台账行当前的指纹；行不存在时为 'ABSENT'。"""
    row = conn.execute(
        "SELECT pump_id, entry_hours, exit_hours, event_type, updated_at"
        " FROM ledger WHERE pump_id = ?",
        (pump_id,),
    ).fetchone()
    if row is None:
        return "ABSENT"
    payload = "|".join(
        str(row[c])
        for c in ("pump_id", "entry_hours", "exit_hours", "event_type", "updated_at")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def replace_ledger(conn: sqlite3.Connection, records: list[PumpRecord],
                   source: str = "csv-upload") -> dict:
    """Replace the whole ledger and rebuild the summary atomically."""
    init_db(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM ledger")
        conn.executemany(
            "INSERT INTO ledger (pump_id, entry_hours, exit_hours, event_type, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(r.pump_id, r.entry_hours, r.exit_hours, r.event_type, _now())
             for r in records],
        )
        _rebuild_summary(conn, records)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_source', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (source,),
        )
        # 整表替换 ⇒ 台账代际递增，此前全部抄表复核结果随之失效
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('ledger_generation', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(_ledger_generation(conn) + 1),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"pump_count": len(records)}


def _upsert_and_rebuild(conn: sqlite3.Connection, record: PumpRecord,
                        source_template: str) -> str:
    """Upsert one pump and rebuild the summary (caller holds the transaction)."""
    existing = conn.execute(
        "SELECT 1 FROM ledger WHERE pump_id = ?", (record.pump_id,)
    ).fetchone()
    action = "updated" if existing else "inserted"
    conn.execute(
        "INSERT INTO ledger (pump_id, entry_hours, exit_hours, event_type, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(pump_id) DO UPDATE SET"
        " entry_hours=excluded.entry_hours,"
        " exit_hours=excluded.exit_hours,"
        " event_type=excluded.event_type,"
        " updated_at=excluded.updated_at",
        (record.pump_id, record.entry_hours, record.exit_hours,
         record.event_type, _now()),
    )
    _rebuild_summary(conn, _load_all_records(conn))
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('last_source', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (source_template.format(action=action),),
    )
    return action


def upsert_pump(conn: sqlite3.Connection, record: PumpRecord) -> dict:
    """Correct a single pump without the caller supplying other pumps.

    The summary is rebuilt inside the same transaction from the full ledger;
    callers never need to read or resubmit the other pumps' details.
    """
    init_db(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        action = _upsert_and_rebuild(conn, record, "single-pump-{action}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"pump_id": record.pump_id, "action": action}


def get_pump(conn: sqlite3.Connection, pump_id: str) -> sqlite3.Row | None:
    init_db(conn)
    return conn.execute(
        "SELECT pump_id, entry_hours, exit_hours, event_type, updated_at"
        " FROM ledger WHERE pump_id = ?",
        (pump_id,),
    ).fetchone()


def list_pumps(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    init_db(conn)
    return list(conn.execute(
        "SELECT pump_id, entry_hours, exit_hours, event_type, updated_at"
        " FROM ledger ORDER BY pump_id"
    ))


# -------------------------------------------------------- meter review --
# 抄表复核：复核通过（入册/出册工时均唯一）后暂存结果，确认时校验复核
# 之后该泵未被更正、台账未被整表替换，然后在同一事务内写台账并重算汇总。

class ReviewStale(Exception):
    """复核结果已失效（该泵已更正或台账已整表替换），须重新复核。"""


def save_review_session(conn: sqlite3.Connection, pump_id: str, m: int,
                        entry_hours: int, exit_hours: int) -> str:
    """暂存一次两端唯一的复核结果；每台泵只保留最新一次复核。"""
    init_db(conn)
    if not pump_id or not pump_id.strip():
        raise ValueError("泵号不能为空")
    if m <= 0:
        raise ValueError("归零周期 M 须大于零")
    if not (0 <= entry_hours <= exit_hours):
        raise ValueError("复核工时非法：须满足 0 ≤ 入册 ≤ 出册")
    token = secrets.token_hex(16)
    conn.execute("DELETE FROM review_sessions WHERE pump_id = ?", (pump_id,))
    conn.execute(
        "INSERT INTO review_sessions (token, pump_id, m, entry_hours, exit_hours,"
        " pump_fingerprint, ledger_generation, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token, pump_id, m, entry_hours, exit_hours,
         _pump_fingerprint(conn, pump_id), _ledger_generation(conn), _now()),
    )
    conn.commit()
    return token


def confirm_review_session(conn: sqlite3.Connection, token: str,
                           event_type: str) -> dict:
    """按复核结果更正单泵：新工时与汇总在同一事务内生效。

    仅当复核所依赖的状态未变时才生效：台账未整表替换（代际相同）、
    该泵台账行与复核时一致（指纹相同）。单独更正其他泵不影响确认。
    任何失败都回滚，保留原有台账与汇总。
    """
    init_db(conn)
    if not token:
        raise ReviewStale("缺少复核凭证，请重新复核")
    if event_type not in EVENT_TYPES:
        raise ValueError("结束方式必须是 故障 / 预防更换 / 观察结束 之一")
    try:
        conn.execute("BEGIN IMMEDIATE")
        sess = conn.execute(
            "SELECT pump_id, m, entry_hours, exit_hours, pump_fingerprint,"
            " ledger_generation FROM review_sessions WHERE token = ?",
            (token,),
        ).fetchone()
        if sess is None:
            raise ReviewStale("复核记录不存在、已使用或已被更新的复核取代，须重新复核")
        pump_id = sess["pump_id"]
        if _ledger_generation(conn) != sess["ledger_generation"]:
            raise ReviewStale(f"泵 {pump_id} 复核后台账已整表替换，须重新复核")
        if _pump_fingerprint(conn, pump_id) != sess["pump_fingerprint"]:
            raise ReviewStale(f"泵 {pump_id} 在复核后已被更正，须重新复核")
        record = PumpRecord(pump_id, float(sess["entry_hours"]),
                            float(sess["exit_hours"]), event_type)
        action = _upsert_and_rebuild(conn, record, "review-confirm-{action}")
        conn.execute("DELETE FROM review_sessions WHERE token = ?", (token,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "pump_id": pump_id,
        "action": action,
        "entry_hours": float(sess["entry_hours"]),
        "exit_hours": float(sess["exit_hours"]),
    }


# ---------------------------------------------------------------- queries --
# Everything below is read-only and only touches the summary tables.

def get_life_table(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    init_db(conn)
    return list(conn.execute(
        "SELECT * FROM life_table ORDER BY t_hours"
    ))


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    init_db(conn)
    return {r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM meta")}


def query_incidence(conn: sqlite3.Connection, hours: float) -> dict:
    """Read-only cumulative incidence query backed by the summary table."""
    if hours < 0:
        raise ValueError("查询小时数不能为负")
    init_db(conn)
    rows = [dict(r) for r in get_life_table(conn)]
    result = incidence_at(rows, hours)
    result["meta"] = get_meta(conn)
    return result
