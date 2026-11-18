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
    email TEXT DEFAULT '',
    source TEXT DEFAULT ''       -- 步骤1 数据源：loopnet / crexi（两条线各自独立，互不替代）
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
CREATE TABLE IF NOT EXISTS searched_pages (
    search_key TEXT NOT NULL,     -- city|state|价格区间
    page INTEGER NOT NULL,
    visited_at TEXT NOT NULL,
    PRIMARY KEY (search_key, page)
);
CREATE TABLE IF NOT EXISTS mined_suites (
    address_norm TEXT PRIMARY KEY,   -- 楼栋级归一化地址
    suites TEXT NOT NULL,            -- JSON 数组：挖到的房号级完整地址
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS page_limits (
    search_key TEXT PRIMARY KEY,     -- 与 searched_pages 同一套 key
    total_pages INTEGER NOT NULL,    -- 实测出来的真实末页（列表页文案的条数估值不可信）
    checked_at TEXT NOT NULL
);
"""

# 允许 update_record 更新的列
_UPDATABLE = {
    "address", "price", "agent", "phone", "email", "listing_url",
    "step1", "step2", "step3", "step4", "conclusion", "notes",
    "reviewed", "area", "screenshots",
}


# 街道后缀归一表：把缩写统一成全称。
# 根因（2026-11-18 实测）：同一栋楼因为写法差异被当成多栋楼重复入库——
# "501 Garasches Ln / 501 Garasches Lane"、"800 N King St / 800 N King Street"、
# "2700 Silverside Rd" 带不带 ZIP 等，records 里 26 个桶出现同楼多条。
# 归一必须消除这些写法差异，否则"一楼只要一个"的规则落不了地。
_SUFFIX_MAP = {
    "st": "street", "str": "street",
    "rd": "road",
    "ave": "avenue", "av": "avenue",
    "blvd": "boulevard",
    "ln": "lane",
    "hwy": "highway",
    "dr": "drive",
    "ct": "court",
    "pl": "place",
    "pkwy": "parkway",
    "cir": "circle",
    "ter": "terrace",
    "expy": "expressway",
    "sq": "square",
    "trl": "trail",
    "plz": "plaza",
    "ctr": "center",
}


def normalize(address: str) -> str:
    """地址归一化（楼栋级）：去房间号/楼层/ZIP + 街道后缀归一 + 去标点小写。

    用于查重比对——同一栋楼的不同写法（St/Street、Ln/Lane、带不带 ZIP+4、
    带不带楼层）必须归成同一个 key，否则一楼会被当成多楼重复入库。
    """
    s = str(address)
    # 1) 去套间/房间/单元号（"Ste 302A"、"Unit 5"、"#12"）
    s = re.sub(r"\b(suite|ste|room|rm|unit|apt|#)\s*\.?\s*#?\s*[\w-]+", " ", s, flags=re.I)
    # 2) 去楼层表达（"10th Floor"、"2nd Fl"、"Floor 3"）
    s = re.sub(r"\b\d+(?:st|nd|rd|th)?\s*(?:floor|fl)\b", " ", s, flags=re.I)
    s = re.sub(r"\b(?:floor|fl)\s*\d+", " ", s, flags=re.I)
    # 3) 去末尾 ZIP / ZIP+4（"19801"、"19801-5524"）——必须在标点清理前做
    s = re.sub(r"\s+\d{5}(?:-\d{4})?\s*$", " ", s.strip())
    # 4) 标点 → 空格、小写
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    # 5) 街道后缀归一（St→street、Ln→lane…），消除缩写差异
    s = " ".join(_SUFFIX_MAP.get(t, t) for t in s.split())
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


def _migrate(con: sqlite3.Connection) -> None:
    """轻量迁移：只加列，不动既有数据（线上 app.db 已存在，不能靠建表语句）。"""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(records)")}
    if "source" not in cols:
        con.execute("ALTER TABLE records ADD COLUMN source TEXT DEFAULT ''")
    # 清理历史残留：结论是"待人工"但没有任何证据（无备注、无截图、非流水线记录）
    # 的行——多为任务中断/异常时留下的半成品，人工无从看起，统一改为不通过。
    # 流水线记录有 source 标记且转人工时必带 notes，不会被误伤。
    con.execute(
        """UPDATE records SET conclusion='不通过',
               notes='历史残留：核查未完成，无人工可看的信息，自动清理'
           WHERE conclusion='待人工' AND notes='' AND screenshots='[]'
             AND IFNULL(source,'')=''"""
    )
    con.execute(
        """UPDATE records SET conclusion='不通过',
               notes=CASE WHEN notes='' THEN '历史残留：核查未完成，自动清理'
                          ELSE notes END
           WHERE conclusion='核查中' AND IFNULL(source,'')!=''"""
    )
    # 流水线半成品：任务中断时还没跑到判定步骤（无备注/无截图/步骤2都没到），
    # 挂着"待人工"却没有任何可看的信息，同样自动清理。
    con.execute(
        """UPDATE records SET conclusion='不通过',
               notes='历史残留：核查未完成（任务中断），自动清理'
           WHERE conclusion='待人工' AND notes='' AND screenshots='[]'
             AND step2='' AND IFNULL(source,'')!=''"""
    )


def init_db() -> None:
    with _lock:
        con = _con()
        try:
            con.executescript(_SCHEMA)
            _migrate(con)
            con.commit()
        finally:
            con.close()


def flush_running_records() -> int:
    """把仍停留在"核查中"的流水线记录统一改判不通过（工人池任务收尾兜底）。

    正常路径：每个工人在自己的 finally 里处理各自在途的记录；
    这里兜底的是异常中断/漏网的，保证半成品不进人工队列。"""
    with _lock:
        con = _con()
        try:
            cur = con.execute(
                """UPDATE records SET conclusion='不通过',
                       notes=CASE WHEN IFNULL(notes,'')=''
                                  THEN '任务结束，该地址未完成核查'
                                  ELSE notes END
                   WHERE conclusion='核查中' AND IFNULL(source,'')!=''""")
            con.commit()
            return cur.rowcount
        finally:
            con.close()


def insert_record(d: dict) -> int:
    addr = d.get("address", "")
    # 新记录默认"核查中"：流水线跑到判定步骤才会写终局结论。
    # 不再把"待人工"当默认值——否则任务运行中/中断时，半成品记录全堆进人工队列。
    conclusion = d.get("conclusion", "核查中")
    with _lock:
        con = _con()
        try:
            cur = con.execute(
                """INSERT INTO records (created_at, address, address_norm, price, agent,
                     phone, email, listing_url, step1, screenshots, area, source, conclusion)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    addr, normalize(addr),
                    d.get("price", ""), d.get("agent", ""), d.get("phone", ""),
                    d.get("email", ""), d.get("listing_url", ""),
                    d.get("step1", ""), json.dumps(d.get("screenshots") or [], ensure_ascii=False),
                    d.get("area", ""), d.get("source", ""), conclusion,
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


def list_records(conclusion: str = "", keyword: str = "",
                 source: str = "") -> list[dict]:
    """核查记录列表（倒序）。conclusion 精确过滤；keyword 模糊匹配地址/中介/电话/邮箱/网址；
    source 按步骤1 数据源过滤（loopnet / crexi，两条线分开查看）。"""
    sql, args = "SELECT * FROM records", []
    conds = []
    if conclusion:
        conds.append("conclusion=?")
        args.append(conclusion)
    if source:
        conds.append("source=?")
        args.append(source.strip().lower())
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


# ---------- 已检查页记忆（同一搜索条件不重复翻同一页） ----------

def page_key(city: str, state: str, price_min: int, price_max: int,
             source: str = "loopnet") -> str:
    """翻页记忆按「数据源」隔离：LoopNet 与 Crexi 的总页数/页码互不相干。"""
    src = str(source or "loopnet").strip().lower()
    return (f"{src}|{str(city).strip().lower()}|{str(state).strip().lower()}"
            f"|{int(price_min)}-{int(price_max)}")


def page_seen_set(key: str) -> set:
    """该搜索条件下已经翻过的页码（跨任务持久化）。"""
    con = _con()
    try:
        rows = con.execute("SELECT page FROM searched_pages WHERE search_key=?", (key,)).fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def page_mark(key: str, page_no: int) -> None:
    with _lock:
        con = _con()
        try:
            con.execute(
                "INSERT OR REPLACE INTO searched_pages (search_key, page, visited_at) VALUES (?,?,?)",
                (key, int(page_no), datetime.now().isoformat(timespec="seconds")))
            con.commit()
        finally:
            con.close()


def page_reset(key: str) -> int:
    """把该条件的翻页记录清空（所有页都查过时重置一轮）。"""
    with _lock:
        con = _con()
        try:
            cur = con.execute("DELETE FROM searched_pages WHERE search_key=?", (key,))
            con.commit()
            return cur.rowcount
        finally:
            con.close()


# ---------- 真实末页探测结果（列表页条数估值靠不住，实测一次就记住） ----------

def page_limit_get(key: str, max_age_days: int = 7) -> int:
    """该搜索条件实测的末页页码；超过 max_age_days 视为过期（房源会增减）→ 返回 0。"""
    con = _con()
    try:
        row = con.execute(
            "SELECT total_pages, checked_at FROM page_limits WHERE search_key=?", (key,)).fetchone()
    finally:
        con.close()
    if not row:
        return 0
    try:
        age = (datetime.now() - datetime.fromisoformat(row[1])).total_seconds() / 86400
    except Exception:
        return 0
    return int(row[0]) if age <= max_age_days else 0


def page_limit_set(key: str, total_pages: int) -> None:
    with _lock:
        con = _con()
        try:
            con.execute(
                "INSERT OR REPLACE INTO page_limits (search_key, total_pages, checked_at) "
                "VALUES (?,?,?)",
                (key, int(total_pages), datetime.now().isoformat(timespec="seconds")))
            con.commit()
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
                "searched_pages": con.execute("DELETE FROM searched_pages").rowcount,
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


def remove_known_address(address: str) -> bool:
    """从收录表移除单条（误收录/误通过时纠错用）。"""
    with _lock:
        con = _con()
        try:
            cur = con.execute("DELETE FROM known_addresses WHERE address_norm=?",
                              (normalize(address),))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def remove_known_by_id(kid: int) -> bool:
    """按 ID 从收录表移除单条。"""
    with _lock:
        con = _con()
        try:
            cur = con.execute("DELETE FROM known_addresses WHERE id=?", (int(kid),))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def remove_known_ids(ids) -> int:
    """按 ID 批量移除。返回实际删除条数。"""
    ids = [int(i) for i in (ids or []) if str(i).strip().lstrip("-").isdigit()]
    if not ids:
        return 0
    with _lock:
        con = _con()
        try:
            cur = con.execute(
                "DELETE FROM known_addresses WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
            con.commit()
            return cur.rowcount
        finally:
            con.close()


def update_known_batch(items) -> dict:
    """按 ID 批量修改地址。

    items: [{"id": 1, "address": "新地址"}, ...]
    先查重再更新（不依赖 IntegrityError，避免整批回滚），
    返回 {"ok": 成功数, "duplicate": 与其他条目重复数, "notfound": ID 不存在数, "empty": 空地址数}。"""
    ok = dup = notfound = empty = 0
    rows = []
    for it in (items or []):
        try:
            kid = int(it.get("id"))
        except (TypeError, ValueError):
            notfound += 1
            continue
        addr = (it.get("address") or "").strip()
        if not addr:
            empty += 1
            continue
        rows.append((kid, normalize(addr), addr))
    if not rows:
        return {"ok": 0, "duplicate": 0, "notfound": 0, "empty": empty}
    with _lock:
        con = _con()
        try:
            for kid, norm, addr in rows:
                clash = con.execute(
                    "SELECT 1 FROM known_addresses WHERE address_norm=? AND id<>?",
                    (norm, kid)).fetchone()
                if clash:
                    dup += 1
                    continue
                cur = con.execute(
                    "UPDATE known_addresses SET address_norm=?, raw=? WHERE id=?",
                    (norm, addr, kid))
                if cur.rowcount:
                    ok += 1
                else:
                    notfound += 1
            con.commit()
        finally:
            con.close()
    return {"ok": ok, "duplicate": dup, "notfound": notfound, "empty": empty}


def update_known_address(kid: int, new_address: str) -> str:
    """按 ID 修改收录地址（同步重算归一化键）。

    返回 "ok" / "notfound" / "duplicate" / "empty"。"""
    new_address = (new_address or "").strip()
    if not new_address:
        return "empty"
    with _lock:
        con = _con()
        try:
            try:
                cur = con.execute(
                    "UPDATE known_addresses SET address_norm=?, raw=? WHERE id=?",
                    (normalize(new_address), new_address, int(kid)))
                con.commit()
            except sqlite3.IntegrityError:
                con.rollback()
                return "duplicate"
            return "ok" if cur.rowcount > 0 else "notfound"
        finally:
            con.close()


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


def mined_get(address: str):
    """取该楼栋已挖到的房号（跨任务缓存，避免反复消耗免费通道的调用次数）。
    返回 (suites列表, note) 或 None。"""
    con = _con()
    try:
        row = con.execute("SELECT suites, note FROM mined_suites WHERE address_norm=?",
                          (normalize(address),)).fetchone()
        if not row:
            return None
        try:
            suites = json.loads(row["suites"])
        except Exception:
            suites = []
        return list(suites or []), (row["note"] or "")
    finally:
        con.close()


def mined_put(address: str, suites: list, note: str = "") -> None:
    with _lock:
        con = _con()
        try:
            con.execute(
                "INSERT OR REPLACE INTO mined_suites (address_norm, suites, note, created_at)"
                " VALUES (?,?,?,?)",
                (normalize(address), json.dumps(suites, ensure_ascii=False), note,
                 datetime.now().isoformat(timespec="seconds")))
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
