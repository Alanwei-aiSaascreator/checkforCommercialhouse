"""步骤1（可选数据源）：Crexi 商业地产出租房源采集（搜索页 + 详情页）。

与 LoopNet 步骤完全同构：返回同样的候选结构
  candidates: [{address, price, agent, phone, email, area, listing_url}]
价格换算/面积解析直接复用 loopnet.monthly_price / _parse_area / _parse_price，
所以「城市+州 + 月租区间」的筛选口径与 LoopNet 一致，结果字段也一致。

与 LoopNet 的实测差异（都写进代码注释，避免后人踩）：
  - 搜索页：/search/properties-for-lease/<city>-<state>?page=N，每页 60 条
  - 租金口径多为 $X/SF/MO（LoopNet 多为 $X/SF/YR）→ monthly_price 已按单位区分
  - 未登录时详情页电话/邮箱被打码（"View phone number" 点击无反应），
    只能拿到经纪人姓名 + 经纪公司 → phone/email 留空，不伪造
  - 网站会把页面自动翻译成中文（Google 翻译脚本）→ 必须拦截，
    否则 "$4/SF/MO" 变成「每平方英尺每月 4 美元」，所有价格正则全废
"""
import re

from .. import browser
from .loopnet import _parse_area, _parse_price, monthly_price

_BASE = "https://www.crexi.com"
_SEARCH = _BASE + "/search/properties-for-lease/"

_BLOCK_KEYWORDS = ("access denied", "are you a robot", "unusual traffic", "captcha",
                   "verify you are human", "too many requests")
_NOT_FOUND_KEYWORDS = ("error 404", "page not found")

# 翻译拦截已统一到 browser.new_page()（所有步骤共用），这里只做别名方便阅读
block_translate = browser.block_translate


def _slug(city: str, state: str) -> str:
    """Los Angeles + CA → los-angeles-ca（Crexi 城市页的 URL 段）。"""
    return re.sub(r"[^a-zA-Z0-9]+", "-", f"{city} {state}").strip("-").lower()


def _search_url(city: str, state: str, page_no: int) -> str:
    """页码走 query 参数：?page=2（第 1 页不带参数）。"""
    url = f"{_SEARCH}{_slug(city, state)}"
    return f"{url}?page={page_no}" if page_no > 1 else url


_CARDS_JS = r"""
() => {
    const out = [];
    const seen = new Set();
    for (const a of document.querySelectorAll("a[href*='/lease/properties/']")) {
        const href = a.href.split("?")[0];
        if (!/^https:\/\/www\.crexi\.com\/lease\/properties\/\d+\//.test(href)) continue;
        if (seen.has(href)) continue;
        seen.add(href);
        // 卡片容器：优先 cui-card，否则向上找 6 层（实测带出完整卡片文案的层级）
        let card = a.closest("cui-card");
        if (!card) {
            let el = a;
            for (let i = 0; i < 6 && el; i++) el = el.parentElement;
            card = el || a;
        }
        out.push({url: href, text: (card.innerText || "").slice(0, 500)});
        if (out.length >= 80) break;
    }
    return out;
}
"""

_PAGES_JS = r"""
() => {
    let max = 0;
    for (const a of document.querySelectorAll("a[href*='page=']")) {
        const m = (a.getAttribute('href') || '').match(/[?&]page=(\d+)/);
        if (m) max = Math.max(max, parseInt(m[1], 10));
    }
    return max;
}
"""

_DETAIL_JS = r"""
() => {
    const txt = e => e ? (e.innerText || "").trim() : "";
    const out = {h1: "", price: "", building: "", brokers: [], brokerage: "", spaces: []};
    out.h1 = txt(document.querySelector("h1"));
    const pp = document.querySelector("[data-cy=primary-price]");
    if (pp) out.price = (pp.innerText || "").split("\n")[0].trim();
    // 建筑信息：label/value 成对出现；面积取 "Total Building SqFt"
    for (const l of document.querySelectorAll("[data-cy=property-detail-summary] [data-cy=label]")) {
        const label = (l.innerText || "").trim();
        if (!/total building|building sqft|total building sqft/i.test(label)) continue;
        let row = l.parentElement;
        for (let i = 0; i < 3 && row; i++) {
            const v = row.querySelector("[data-cy=value]");
            if (v) { out.building = (v.innerText || "").trim(); break; }
            row = row.parentElement;
        }
    }
    out.brokers = [...document.querySelectorAll("[data-cy=broker-name-link]")]
        .map(txt).filter(Boolean);
    const m = (document.body.innerText || "").match(/Listed by\s*([^\n]+)/);
    out.brokerage = m ? m[1].trim() : "";
    // 套间：每块的头部三行 = 套间名 / 单价 / 面积
    out.spaces = [...document.querySelectorAll("[data-cy=space]")].map(e =>
        (e.innerText || "").split("\n").map(s => s.trim()).filter(Boolean).slice(0, 3));
    return out;
}
"""

# "7203 Van Nuys Blvd" 的下一行 "Los Angeles, CA 91405"
_CITY_LINE_RE = re.compile(r"^(.+?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$")
_ADDR_JUNK_RE = re.compile(r"^(for lease|for sale|request info|undisclosed|\$)", re.I)


def _addr_from_line(line: str, prev: str) -> str:
    """把「城市, 州 邮编」行 + 上一行（街址）拼成完整地址。

    实测脏数据：
      - "Eight Thousand Sunset, 8000 Sunset Blvd, Los Angeles, CA 90046"：
        楼盘名混进了地址行 → 只取最后三段（街址, 城市, 州 邮编）
      - "West Valley Medical Center, 5353 - 5363 Balboa Blvd, Encino, CA 91316" 同上
      - 上一行就是完整地址 → 直接用，避免「街址, 街址, 城市」重复
    """
    parts = [p.strip() for p in (line or "").split(",") if p.strip()]
    if len(parts) >= 3:
        return ", ".join(parts[-3:])
    prev = (prev or "").strip(" ,")
    if not prev or _ADDR_JUNK_RE.match(prev):
        return line
    if (line or "").lower().startswith(prev.lower()):
        return line
    # 楼盘名和街址挤在同一行 → 从第一个数字开始截
    m = re.search(r"\d", prev)
    if m and m.start() > 0:
        prev = prev[m.start():].strip(" ,")
    return f"{prev}, {line}"


def _parse_card_text(text: str) -> dict:
    """解析搜索页卡片文案。实测（英文、未翻译）：

        For Lease
        $2.50/SF/MO
        11751 Mississippi
        Office
        3 spaces available
        945 SqFt - 3,668 SqFt
        11751 Mississippi Ave
        Los Angeles, CA 90025
        Request Info

    地址 = 「城市, 州 邮编」行的上一行（街址）+ 该行本身。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    price = ""
    addr = ""
    for i, line in enumerate(lines):
        low = line.lower()
        if not price:
            if "undisclosed" in low and ("rate" in low or "rent" in low):
                price = ""
            elif line.startswith("$"):
                price = _parse_price(line)
        if addr:
            continue
        m = _CITY_LINE_RE.match(line)
        if m and i > 0:
            addr = _addr_from_line(line, lines[i - 1])
    return {"address": re.sub(r"\s+", " ", addr).strip(" |,"),
            "price": price,
            "area": _parse_area("\n".join(lines))}


def _parse_total_pages(page) -> int:
    try:
        pages = page.evaluate(_PAGES_JS) or 0
    except Exception:
        pages = 0
    return max(int(pages), 1)


def _clean_h1(h1: str) -> str:
    """详情页 h1：「17320 Ventura Blvd, Los Angeles, CA 91316 For Lease」→ 地址。"""
    s = re.sub(r"\s+", " ", h1 or "").strip()
    s = re.sub(r"\s+(for lease|for sale|for auction)\s*$", "", s, flags=re.I)
    return s.strip(" |,")


def _plausible_rate(rate: str) -> bool:
    """单价是否可信。实测 Crexi 上有「$1,450/SF/MO × 455 SF」这种把
    「每月总价」误填到单价栏的房源，换算成月租是 $65 万 → 不能当成超预算淘汰，
    标为「无法判定」转人工（宁可疑也不杀错，与 SOP 一致）。"""
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", rate or "")
    if not m:
        return True
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return True
    low = (rate or "").lower()
    if re.search(r"sf\s*/\s*mo|/\s*sf\s*/\s*mo", low):     # 每英尺每月：> $100 不合理
        return val <= 100
    if re.search(r"sf\s*/\s*yr|/\s*sf\s*/\s*yr", low):     # 每英尺每年：> $1,000 不合理
        return val <= 1000
    return True


def _spaces_from_rows(rows: list) -> list:
    """套间行 → [{name, size, rate, monthly}]；缺面积或缺单价的跳过（与 LoopNet 一致）。"""
    spaces = []
    for lines in rows or []:
        if not lines:
            continue
        name = lines[0]
        rate = size = ""
        for ln in lines[1:]:
            low = ln.lower()
            if not rate and ("$" in ln or "undisclosed" in low):
                rate = "" if "undisclosed" in low else _parse_price(ln)
            elif not size and re.search(r"sq\.?\s*ft|sqft|\bsf\b", ln, re.I):
                size = _parse_area(ln)
        if not (size and rate):
            continue
        spaces.append({"name": name, "size": size, "rate": rate,
                       "monthly": monthly_price(rate, size) if _plausible_rate(rate) else None})
    return spaces


def _detail_candidate(page, url: str) -> dict:
    """已打开的详情页 → 候选（地址/价格/经纪人/面积/套间表）。"""
    try:
        info = page.evaluate(_DETAIL_JS) or {}
    except Exception:
        info = {}
    brokers = [b for b in (info.get("brokers") or []) if b]
    brokerage = (info.get("brokerage") or "").strip()
    agent = brokers[0] if brokers else ""
    if agent and brokerage:
        agent = f"{agent}（{brokerage}）"
    elif not agent:
        agent = brokerage
    building = re.sub(r"[^\d]", "", str(info.get("building") or ""))
    return {
        "address": _clean_h1(info.get("h1", "")),
        "price": (info.get("price") or "").strip(),
        "agent": agent,
        "phone": "",            # 未登录打码，不伪造
        "email": "",
        "area": building,
        "listing_url": url,
        "spaces": _spaces_from_rows(info.get("spaces") or []),
    }


def search_candidates(city: str, state: str, price_min: int, price_max: int,
                      limit: int = 60, tag: str = "search", page: int = 1) -> dict:
    """按城市/州搜索 Crexi 出租房源（page 为结果页页码）。

    价格区间不交给 Crexi 的筛选项：它的 rate 过滤是「每平方英尺」口径，
    和 SOP 的「每月总租金」不是一回事。这里保持与 LoopNet 相同的做法——
    搜索页只按 城市/州 取候选，月租是否达标由 pipeline 统一判定。
    """
    page_no = max(int(page or 1), 1)
    url = _search_url(city, state, page_no)
    shot_path = ""
    pg = browser.new_page()
    try:
        browser.goto_retry(pg, url, timeout=60000, wait=7000)
        shot_path = browser.shot(pg, f"{tag}_crexi")
        body = pg.evaluate("() => document.body.innerText")
        low = (body or "").lower()
        if any(k in low for k in _BLOCK_KEYWORDS):
            return {"status": "fail",
                    "message": "Crexi 拦截（Access Denied/验证码）：请开启系统代理并选美国节点"
                               "（SOP：Clash Verge → 系统代理 → 凤凰城）后重试；"
                               "或在界面粘房源链接走手动兜底",
                    "candidates": [], "screenshot": shot_path or "", "total_pages": 0}
        not_found = ("/404" in (pg.url or "")) or any(k in low for k in _NOT_FOUND_KEYWORDS)
        if not_found:
            if page_no == 1:
                return {"status": "fail",
                        "message": f"Crexi 找不到 {city}, {state} 的搜索结果：城市需填具体城市名"
                                   f"（如 Los Angeles），州填两位缩写（如 CA）",
                        "candidates": [], "screenshot": shot_path or "",
                        "total_pages": 0, "not_found": True}
            return {"status": "warn",
                    "message": f"Crexi 第 {page_no} 页不存在（已超出真实总页数）",
                    "candidates": [], "screenshot": shot_path or "",
                    "total_pages": max(page_no - 1, 0), "not_found": True}
        cards = pg.evaluate(_CARDS_JS) or []
        total_pages = _parse_total_pages(pg) if cards else 0
    except Exception as e:
        return {"status": "fail", "message": f"Crexi 访问失败: {e}", "candidates": [],
                "screenshot": shot_path or "", "total_pages": 0}
    finally:
        browser.safe_close(pg)

    candidates, seen = [], set()
    for c in cards:
        parsed = _parse_card_text(c.get("text", ""))
        addr = parsed["address"]
        if not addr:
            continue
        key = store_norm(addr)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "address": addr,
            "price": parsed["price"],
            "agent": "",
            "phone": "",
            "email": "",
            "area": parsed["area"],
            "listing_url": c["url"],
        })
        if len(candidates) >= limit:
            break
    msg = (f"Crexi 第 {page_no}/{total_pages or '?'} 页采集到 {len(candidates)} 个候选房源"
           f"（区间 ${price_min}-${price_max}/月，价格稍后逐条判定）"
           if candidates else
           f"Crexi 第 {page_no} 页未解析到候选地址（卡片结构可能变化，已留档搜索页截图）")
    return {"status": "pass" if candidates else "warn", "message": msg,
            "candidates": candidates, "screenshot": shot_path or "",
            "total_pages": total_pages}


def candidates_from_urls(urls: list) -> dict:
    """手动房源链接兜底：逐条打开 Crexi 详情页解析候选。"""
    candidates = []
    pg = browser.new_page()
    shot_path = ""
    try:
        for u in urls:
            u = (u or "").strip()
            if not u:
                continue
            try:
                browser.goto_retry(pg, u, timeout=60000, wait=6000)
                shot_path = shot_path or browser.shot(pg, "manual_crexi")
                cand = _detail_candidate(pg, u)
                if cand["address"]:
                    cand.pop("spaces", None)
                    candidates.append(cand)
            except Exception:
                continue
    finally:
        browser.safe_close(pg)
    return {"status": "pass" if candidates else "warn",
            "message": f"手动链接解析出 {len(candidates)} 个候选房源",
            "candidates": candidates, "screenshot": shot_path or ""}


def fetch_broker_info(listing_url: str) -> dict:
    """详情页补抓 价格/经纪人/面积 + 各套间面积与单价（spaces）。
    电话/邮箱未登录打码 → 返回空串（不猜、不伪造）。"""
    info = {"price": "", "agent": "", "phone": "", "email": "", "area": "", "spaces": []}
    if not listing_url:
        return info
    pg = browser.new_page()
    try:
        browser.goto_retry(pg, listing_url, timeout=60000, wait=6000)
        cand = _detail_candidate(pg, listing_url)
        for k in ("price", "agent", "area"):
            info[k] = cand.get(k, "")
        info["spaces"] = cand.get("spaces") or []
    except Exception:
        pass
    finally:
        browser.safe_close(pg)
    return info


def store_norm(addr: str) -> str:
    from .. import store
    return store.normalize(addr)
