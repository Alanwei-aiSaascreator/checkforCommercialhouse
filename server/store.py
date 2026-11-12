"""SQLite 数据存取：records / known_addresses / smarty_cache / quota。

表结构以线上 app.db 为准（勿改动已有列，只做 IF NOT EXISTS 兜底建表）。
"""
import json
import re
import sqlite3
import threading
from datetime import date, datetime

from . import config

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    address TEXT NOT NULL,
    address_norm TEXT NOT NULL,
    price TEXT DEFAULT '',
    agent TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    listing_url TEXT DEFAULT '',
    step1 TEXT DEFAULT '',
    step2 TEXT DEFAULT '',
    step3 TEXT DEFAULT '',
    step4 TEXT DEFAULT '',
    conclusion TEXT DEFAULT '待人工',
    screenshots TEXT DEFAULT '[]',
    notes TEXT DEFAULT '',
    reviewed INTEGER DEFAULT 0,
    area TEXT DEFAULT '',
    email TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS known_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address_norm TEXT NOT NULL UNIQUE,
    raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS smarty_cache (
    address_norm TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    standard TEXT DEFAULT '',
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota (
    day TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS failed_addresses (
    address_norm TEXT PRIMARY KEY,
    level TEXT NOT NULL,          -- exact=精确地址(含Suite) / building=楼栋级(去Suite)
    step TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""

# 允许 update_record 更新的列
_UPDATABLE = {
    "address", "price", "agent", "phone", "email", "listing_url",
    "step1", "step2", "step3", "step4", "conclusion", "notes",
    "reviewed", "area", "screenshots",
}


def normalize(address: str) -> str:
    """地址归一化（楼栋级）：去房间号（Ste/Suite/Room/Unit/#）、去标点、小写。用于查重比对。"""
    s = re.sub(r"\b(suite|ste|room|rm|unit|fl|floor|#)\s*[\w-]+", " ", str(address), flags=re.I)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return " ".join(s.split())


def normalize_precise(address: str) -> str:
    """精确归一化：保留房间号（Ste/Suite/Room），仅去标点小写。
    失败档案的 exact 级键——"123 Main St Ste 100" 失败只拉黑该 Suite，
    不影响同楼栋 Ste 200（用户要求：区分楼和房，不要杀错）。"""
    s = re.sub(r"[^a-z0-9]+", " ", str(address).lower())
    return " ".join(s.split())


_HAS_UNIT_RE = re.compile(r"\b(suite|ste|room|rm|unit|#)\s*[\w-]", re.I)


def add_failed_address(address: str, step: str = "", reason: str = "",
                       building: bool = False) -> None:
    """登记失败档案。building=False 只记精确级（含 Suite 的地址，
    同楼其他 Suite 不受影响）；building=True 额外记楼栋级（无房号
    候选下次快速跳过，带房号候选仍放行）。"""
    with _lock:
        con = _con()
        try:
            rows = [(normalize_precise(address), "exact", step, reason)]
            if building:
                rows.append((normalize(address), "building", step, reason))
            for norm, level, st, rs in rows:
                if not norm:
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO failed_addresses"
                    " (address_norm, level, step, reason, created_at) VALUES (?,?,?,?,?)",
                    (norm, level, st, rs, datetime.now().isoformat(timespec="seconds")))
            con.commit()
        finally:
            con.close()


def is_failed_address(address: str):
    """候选是否命中失败档案。返回 (是否跳过, 原因)。
    规则：精确级命中任何候选都跳过；楼栋级只跳过"无房号"候选
    （带 Ste/Suite 的新候选不受楼栋失败牵连——不杀错）。"""
    exact = normalize_precise(address)
    con = _con()
    try:
        row = con.execute("SELECT reason, step FROM failed_addresses"
                          " WHERE address_norm=? AND level='exact'", (exact,)).fetchone()
        if row:
            return True, row["reason"] or row["step"] or "此前验证失败"
        if not _HAS_UNIT_RE.search(str(address)):
            row = con.execute("SELECT reason, step FROM failed_addresses"
                              " WHERE address_norm=? AND level='building'",
                              (normalize(address),)).fetchone()
            if row:
                return True, row["reason"] or row["step"] or "此前验证失败"
        return False, ""
    finally:
        con.close()


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _lock:
        con = _con()
        try:
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()


def insert_record(d: dict) -> int:
    addr = d.get("address", "")
    with _lock:
        con = _con()
        try:
            cur = con.execute(
                """INSERT INTO records (created_at, address, address_norm, price, agent,
                     phone, email, listing_url, step1, screenshots, area)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    addr, normalize(addr),
                    d.get("price", ""), d.get("agent", ""), d.get("phone", ""),
                    d.get("email", ""), d.get("listing_url", ""),
                    d.get("step1", ""), json.dumps(d.get("screenshots") or [], ensure_ascii=False),
                    d.get("area", ""),
                ),
            )
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()


def update_record(record_id: int, **fields) -> None:
    cols, vals = [], []
    for k, v in fields.items():
        if k not in _UPDATABLE:
            continue
        if k == "screenshots":
            v = json.dumps(v or [], ensure_ascii=False)
        if k == "address" and v:
            cols.append("address_norm")
            vals.append(normalize(v))
        cols.append(k)
        vals.append(v)
    if not cols:
        return
    with _lock:
        con = _con()
        try:
            con.execute(f"UPDATE records SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
                        (*vals, record_id))
            con.commit()
        finally:
            con.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["screenshots"] = json.loads(d.get("screenshots") or "[]")
    except Exception:
        d["screenshots"] = []
    return d


def list_records(conclusion: str = "", keyword: str = "") -> list[dict]:
    """核查记录列表（倒序）。conclusion 精确过滤；keyword 模糊匹配地址/中介/电话/邮箱/网址。"""
    sql, args = "SELECT * FROM records", []
    conds = []
    if conclusion:
        conds.append("conclusion=?")
        args.append(conclusion)
    if keyword:
        like = f"%{keyword}%"
        conds.append("(address LIKE ? OR agent LIKE ? OR phone LIKE ? OR email LIKE ?"
                     " OR listing_url LIKE ? OR notes LIKE ?)")
        args += [like] * 6
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    con = _con()
    try:
        rows = con.execute(sql + " ORDER BY id DESC", args).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        con.close()


def get_record(record_id: int):
    con = _con()
    try:
        row = con.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        con.close()


def delete_record(record_id: int) -> bool:
    with _lock:
        con = _con()
        try:
            cur = con.execute("DELETE FROM records WHERE id=?", (record_id,))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


# ---------- 历史清理 ----------

def history_stats() -> dict:
    """各历史表当前条数（给前端清空前确认用）。"""
    con = _con()
    try:
        return {
            "records": con.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            "failed": con.execute("SELECT COUNT(*) FROM failed_addresses").fetchone()[0],
            "known": con.execute("SELECT COUNT(*) FROM known_addresses").fetchone()[0],
            "smarty_cache": con.execute("SELECT COUNT(*) FROM smarty_cache").fetchone()[0],
        }
    finally:
        con.close()


def clear_history(include_known: bool = False, include_smarty_cache: bool = False) -> dict:
    """清空历史查验痕迹：核查记录 + 失败档案（可选 收录地址表 / Smarty 缓存）。

    失败档案不跟着清的话，历史淘汰过的地址仍会被快速跳过，随机抽页拿到
    的候选依旧跑不动；收录表默认保留（那是已通过的成果），要一起清需显式传参。
    """
    with _lock:
        con = _con()
        try:
            out = {
                "records": con.execute("DELETE FROM records").rowcount,
                "failed": con.execute("DELETE FROM failed_addresses").rowcount,
                "known": 0,
                "smarty_cache": 0,
            }
            if include_known:
                out["known"] = con.execute("DELETE FROM known_addresses").rowcount
            if include_smarty_cache:
                out["smarty_cache"] = con.execute("DELETE FROM smarty_cache").rowcount
            # 记录自增 ID 归零，清空后重新从 #1 开始归档
            con.execute("DELETE FROM sqlite_sequence WHERE name='records'")
            con.commit()
            return out
        finally:
            con.close()


# ---------- 收录地址表 ----------

def count_known() -> int:
    con = _con()
    try:
        return con.execute("SELECT COUNT(*) FROM known_addresses").fetchone()[0]
    finally:
        con.close()


def is_known_address(address: str) -> bool:
    con = _con()
    try:
        row = con.execute("SELECT 1 FROM known_addresses WHERE address_norm=?",
                          (normalize(address),)).fetchone()
        return row is not None
    finally:
        con.close()


def import_known_addresses(addresses, source: str = "") -> int:
    """批量收录（source 参数保留兼容旧调用，如 'auto'/'manual'）。返回新增条数。"""
    n = 0
    with _lock:
        con = _con()
        try:
            for raw in addresses:
                raw = (raw or "").strip()
                if not raw:
                    continue
                cur = con.execute(
                    "INSERT OR IGNORE INTO known_addresses (address_norm, raw) VALUES (?,?)",
                    (normalize(raw), raw))
                n += cur.rowcount
            con.commit()
        finally:
            con.close()
    return n


def list_known() -> list[dict]:
    con = _con()
    try:
        rows = con.execute("SELECT * FROM known_addresses ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ---------- Smarty 缓存与每日额度 ----------

def smarty_cache_get(address_norm: str):
    con = _con()
    try:
        row = con.execute("SELECT status, standard FROM smarty_cache WHERE address_norm=?",
                          (address_norm,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def smarty_cache_put(address_norm: str, status: str, standard: str = "") -> None:
    with _lock:
        con = _con()
        try:
            con.execute(
                "INSERT OR REPLACE INTO smarty_cache (address_norm, status, standard, checked_at)"
                " VALUES (?,?,?,?)",
                (address_norm, status, standard, datetime.now().isoformat(timespec="seconds")))
            con.commit()
        finally:
            con.close()


def quota_today() -> int:
    con = _con()
    try:
        row = con.execute("SELECT count FROM quota WHERE day=?", (date.today().isoformat(),)).fetchone()
        return row[0] if row else 0
    finally:
        con.close()


def quota_incr() -> int:
    with _lock:
        con = _con()
        try:
            con.execute(
                "INSERT INTO quota (day, count) VALUES (?,1)"
                " ON CONFLICT(day) DO UPDATE SET count=count+1",
                (date.today().isoformat(),))
            con.commit()
            return con.execute("SELECT count FROM quota WHERE day=?",
                               (date.today().isoformat(),)).fetchone()[0]
        finally:
            con.close()


init_db()
