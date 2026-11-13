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

# 价格只认详情页（套间表）：搜索卡片上的 "$X/SF/MO" 常是整层口径，
# 按它粗判会误杀小套间。pipeline 读到本标记后，卡片阶段不再做价格淘汰，
# 一律进详情页取价（含卡片写 Undisclosed 的；详见 pipeline need_detail）。
DETAIL_IS_PRICE_SOURCE = True

_BLOCK_KEYWORDS = ("access denied", "are you a robot", "unusual traffic", "captcha",
                   "verify you are human", "too many requests",
                   # Cloudflare 挑战页（实测出现过：截图为 "Performing security verification"）
                   "security verification", "verifying you are not a bot",
                   "just a moment", "checking your browser")
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
    // 套间：取整块文案（前 3 行 = 套间名/单价/面积，块内后面还有
    // Rentable SqFt、Total Monthly Rent —— 单价写 "Undisclosed" 时唯一能拿到月租的地方）
    out.spaces = [...document.querySelectorAll("[data-cy=space]")].map(e =>
        (e.innerText || "").slice(0, 900));
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
    # 卡片写 "Undisclosed Rate" 只代表「卡片没价格」，不代表详情页没有：
    # 实测 6210 Charlotte Pike 详情页头部同样是 Undisclosed，但套间块里有
    # "Total Monthly Rent $2,500" —— 所以这个标记只用于日志提示，不再阻止开页。
    undisclosed = "undisclosed" in (text or "").lower()
    return {"address": re.sub(r"\s+", " ", addr).strip(" |,"),
            "price": price,
            "area": _parse_area("\n".join(lines)),
            "undisclosed": undisclosed and not price}


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


def _labeled_value(lines: list, label: str) -> str:
    """套间块里取「标签 → 下一行值」（实测：Rentable SqFt\\n2,000）。"""
    for i, ln in enumerate(lines):
        if label in (ln or "").lower():
            for nxt in lines[i + 1:i + 3]:
                v = re.sub(r"[^\d,]", "", nxt or "")
                if v:
                    return v.replace(",", "")
    return ""


def _total_monthly(lines: list) -> float | None:
    """套间块里的「Total Monthly Rent」总月租（单价写 Undisclosed 时唯一的价格来源）。"""
    for i, ln in enumerate(lines):
        if re.search(r"total\s+monthly\s+rent", ln or "", re.I):
            for nxt in lines[i + 1:i + 3]:
                m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", nxt or "")
                if m:
                    try:
                        return float(m.group(1).replace(",", ""))
                    except ValueError:
                        return None
    return None


def _spaces_from_rows(rows: list) -> list:
    """套间块 → [{name, size, rate, monthly}]。

    块结构（实测 Crexi 6210 Charlotte Pike）：
        Space 6210            ← 套间名
        Undisclosed           ← 单价（可以未披露）
        2,000 SqFt            ← 面积
        ...
        Total Monthly Rent
        $2,500                ← 单价未披露时，这里仍有真实月租，必须用上

    只要拿到面积就保留该套间（单价缺失时 monthly=None，交给 pipeline 转人工），
    不再因为「缺单价」整条丢弃——丢弃会让月租为 $2,500 的房源被当成"无套间"。
    """
    spaces = []
    for raw in rows or []:
        if not raw:
            continue
        lines = (raw if isinstance(raw, list) else
                 [ln.strip() for ln in str(raw).splitlines() if ln.strip()])
        if not lines:
            continue
        name = lines[0]
        rate = size = ""
        for ln in lines[1:]:
            low = ln.lower()
            if not rate and not size and ("$" in ln or "undisclosed" in low):
                rate = "" if "undisclosed" in low else _parse_price(ln)
            elif not size and re.search(r"sq\.?\s*ft|sqft|\bsf\b", ln, re.I):
                size = _parse_area(ln)
        size = size or _labeled_value(lines, "rentable sqft") or _labeled_value(lines, "usable sqft")
        if not size:
            continue
        monthly = _total_monthly(lines)
        # 单价栏被误填成「每月总价」的房源（实测 76 Spring St：$5,200/SF/MO × 1,171 SF
        # 会算出 600 万/月）→ monthly 留 None（不当单价用），另记一个疑似总价，
        # 交给 pipeline：疑似总价都超预算时即可判死，落在预算内仍转人工复核
        suspected = None
        if monthly is None and rate:
            if _plausible_rate(rate):
                monthly = monthly_price(rate, size)
            else:
                m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", rate)
                if m:
                    try:
                        suspected = float(m.group(1).replace(",", ""))
                    except ValueError:
                        suspected = None
        spaces.append({"name": name, "size": size, "rate": rate, "monthly": monthly,
                       "suspected_total": suspected})
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
    addr = _clean_h1(info.get("h1", ""))
    # 页面没渲染出来时 h1 会是站点名（实测拿到 "www.crexi.com"），
    # 真地址一定含门牌号——不含数字就判为解析失败，宁可空也不要脏地址进库
    if not re.search(r"\d", addr):
        addr = ""
    return {
        "address": addr,
        "price": (info.get("price") or "").strip(),
        "agent": agent,
        "phone": "",            # 未登录打码，不伪造
        "email": "",
        "area": building,
        "undisclosed": "undisclosed" in str(info.get("price") or "").lower(),
        "listing_url": url,
        "spaces": _spaces_from_rows(info.get("spaces") or []),
    }


def _search_candidates_html(city: str, state: str, price_min: int, price_max: int,
                            limit: int = 60, tag: str = "search", page: int = 1) -> dict:
    """（旧实现，仅作接口失效时的兜底）按城市/州搜索 Crexi 出租房源。

    价格区间不交给 Crexi 的筛选项：它的 rate 过滤是「每平方英尺」口径，
    和 SOP 的「每月总租金」不是一回事。这里保持与 LoopNet 相同的做法——
    搜索页只按 城市/州 取候选，月租是否达标由 pipeline 统一判定。
    """
    page_no = max(int(page or 1), 1)
    url = _search_url(city, state, page_no)
    shot_path = ""
    pg = browser.new_page()
    try:
        # 卡片渲染时间波动大（实测 5~10s，固定 7s 空等会拿到空页误判"无候选"）：
        # 先短等，再轮询等卡片真的出现（最多 24s），出现即走 —— 比固定空等更稳也更快
        browser.goto_retry(pg, url, timeout=60000, wait=2000)
        cards: list = []
        for _ in range(12):
            cards = pg.evaluate(_CARDS_JS) or []
            if cards:
                break
            pg.wait_for_timeout(2000)
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
            "undisclosed": parsed.get("undisclosed", False),
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


# ---------------------------------------------------------------------------
# 搜索页改走 Crexi 自己的 JSON 接口（api.crexi.com/universal-search/v2/search）
#
# 为什么必须换（实测 Louisville, KY）：
#   * 搜索页是「地图 + 列表」布局，DOM 里只渲染首屏 8 张卡片，滚动 window /
#     mouse.wheel 都不会加载更多 → 400 条房源我们只看到 8 条，而且总页数
#     也只能从懒渲染的分页条里读到 2 页，最后误报"共 2 页已全部遍历"
#   * 卡片文案只有 $/SF 单价，没有月租
#   * 接口一次给 60 条，每条自带 leaseRateRange.totalMonthly（"$1,200.14"）
#     与 leaseDetails[].leaseRate.totalMonthly（$1,995.00 这种），
#     月租直接可读 —— 判价根本不用开详情页
# ---------------------------------------------------------------------------
_SEARCH_API = "https://api.crexi.com/universal-search/v2/search"
_API_PAGE_SIZE = 60

_API_FETCH_JS = r"""
async ([url, payload]) => {
    const r = await fetch(url, {method: "POST",
        headers: {"content-type": "application/json", "accept": "application/json"},
        body: JSON.stringify(payload)});
    return {status: r.status, text: await r.text()};
}
"""

_api_pg = {"pg": None}


def _api_page():
    """复用同一个 tab 发接口请求（每次新建要多等一次首页渲染，约 8s）。

    必须停在 crexi.com 域名下且过了 Cloudflare 挑战：否则页内 fetch 是跨域/被拦，
    报 "TypeError: Failed to fetch"（实测杀过 Chrome 进程后必现）。
    """
    pg = _api_pg.get("pg")
    if pg is not None:
        try:
            if not pg.is_closed() and "crexi.com" in (pg.url or ""):
                if "just a moment" not in (pg.title() or "").lower():
                    return pg
        except Exception:
            pass
    pg = browser.new_page()
    browser.goto_retry(pg, _BASE, timeout=60000, wait=2500)
    for _ in range(8):
        try:
            if "just a moment" not in (pg.title() or "").lower():
                break
        except Exception:
            pass
        pg.wait_for_timeout(1500)
    _api_pg["pg"] = pg
    return pg


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _money(txt) -> str:
    """统一成带 $ 的文案（接口有的字段自带 $，直接拼会出现 "$$1,200"）。"""
    t = str(txt or "").strip()
    if not t:
        return ""
    return t if t.startswith("$") else f"${t}"


# 单价合理性上限：$/SF/YR > 200 或 $/SF/MO > 20 都说明「每月总价」被填进了单价栏
_RATE_MAX_YR = 200.0
_RATE_MAX_MO = 20.0


def _item_spaces(item: dict) -> list:
    """接口的 leaseDetails[] → pipeline 的套间结构 [{name,size,rate,monthly}]。

    月租优先级：totalMonthly → totalAnnual/12 → 单价×面积（带合理性阈值）。
    单价离谱（如 $54,000/SF/YR）时不换算，记 suspected_total 交 pipeline 处理，
    宁可转人工也不算出个天文数字去误杀。
    """
    out = []
    for i, d in enumerate(item.get("leaseDetails") or [], 1):
        if not isinstance(d, dict):
            continue
        size = d.get("rentableSqft") or d.get("rentableSqftMin") or d.get("rentableSqftMax")
        if not size:
            continue
        size = int(float(size))
        lr = d.get("leaseRate") or {}
        per_yr = _num(lr.get("perSqftAnnualMin"))
        per_mo = _num(lr.get("perSqftMonthlyMin"))
        monthly = _num(lr.get("totalMonthly"))
        if monthly is None:
            annual = _num(lr.get("totalAnnual"))
            if annual:
                monthly = round(annual / 12, 2)
        if monthly is not None and monthly <= 0:
            monthly = None        # 接口偶尔给 0（占位）→ 当无价，别当"$0/月"算合规
        suspected = None
        if monthly is None:
            if per_mo is not None and per_mo <= _RATE_MAX_MO:
                monthly = round(per_mo * size, 2)
            elif per_yr is not None and per_yr <= _RATE_MAX_YR:
                monthly = round(per_yr * size / 12, 2)
            elif per_mo is not None:
                suspected = per_mo
            elif per_yr is not None:
                suspected = per_yr
        rate = ""
        if per_yr is not None:
            rate = f"${per_yr:g}/SF/YR"
        elif per_mo is not None:
            rate = f"${per_mo:g}/SF/MO"
        out.append({
            "name": d.get("spaceName") or d.get("name") or f"套间 {i}",
            "size": str(size),
            "rate": rate,
            "monthly": monthly,
            "suspected_total": suspected,
        })
    return out


def _candidate_from_item(item: dict) -> dict:
    """接口 item → pipeline 候选结构（地址/月租/面积/经纪人/套间表/详情页链接）。"""
    addrs = item.get("address") or []
    addr = (addrs[0].get("fullAddress") if addrs and isinstance(addrs[0], dict) else "") or ""
    brokers = item.get("brokers") or []
    b = brokers[0] if brokers and isinstance(brokers[0], dict) else {}
    agent = (b.get("name") or "").strip()
    agency = (b.get("brokerage") or "").strip()
    if agent and agency:
        agent = f"{agent}（{agency}）"
    elif not agent:
        agent = agency

    spaces = _item_spaces(item)
    rng = item.get("leaseRateRange") or {}
    total_monthly = (rng.get("totalMonthly") or "").strip()
    # "0" / "$0" 是接口的占位值，不是真的免租 → 当无价处理
    if re.fullmatch(r"\$?\s*0(?:\.0+)?", total_monthly.replace(",", "")):
        total_monthly = ""
    per_sf = (rng.get("perSqftAnnual") or "").strip()

    # 面积：优先最小的可租套间（整栋面积会把预算内小套间算死）
    sizes = [int(s["size"]) for s in spaces if str(s.get("size", "")).isdigit()]
    area = str(min(sizes)) if sizes else ""

    # 价格文案优先用「套间级月租」（可靠）：range.totalMonthly 有脏数据
    # —— 实测 4016 Bishop Ln 的 range 写 $27,405,000/月，而同一条的套间月租是 $4,500
    ms = sorted({s["monthly"] for s in spaces if s.get("monthly")})
    price = ""
    if ms:
        price = (f"${ms[0]:,.0f}–${ms[-1]:,.0f}/月" if len(ms) > 1
                 else f"${ms[0]:,.0f}/月")
    elif per_sf:
        price = f"{_money(per_sf)}/SF/YR"
    elif total_monthly:
        price = total_monthly if "/" in total_monthly else f"{_money(total_monthly)}/月"

    # 接口的 id 带类型前缀（"lease-1257003"），详情页路由只认纯数字：
    # 直接用会拼成 /lease/properties/lease-1257003/... → Crexi 把它当搜索词，
    # 跳到一个 0 结果的搜索页（实测绘图上就是 "Lease 1257003 0 Properties"）
    pid, slug = str(item.get("id") or ""), item.get("urlSlug") or ""
    m = re.search(r"(\d+)\s*$", pid)
    pid = m.group(1) if m else pid
    url = f"{_BASE}/lease/properties/{pid}/{slug}" if pid.isdigit() and slug else ""

    return {
        "address": addr,
        "price": price,
        "agent": agent,
        "phone": "",
        "email": "",
        "area": area,
        "undisclosed": not bool(spaces and any(s.get("monthly") for s in spaces)
                                or total_monthly),
        "listing_url": url,
        "spaces": spaces,          # 接口已带套间级月租 → 无需开详情页
    }


def _api_search(pg, city: str, state: str, page_no: int, size: int) -> dict:
    payload = {
        "excludeFilters": [], "excludeSort": [], "ids": [], "boundingBox": None,
        "filters": {"address": {"mode": "Include",
                                "structuredValues": [f"{city}, {state}"],
                                "type": "Plain", "values": []}},
        "from": (page_no - 1) * size, "searchTypes": ["Lease"], "size": size,
        "sorting": {"searchAttributes.crexiSearchRank": "Descending",
                    "listingAttributes.dateActivated": "Descending"},
    }
    last: dict = {}
    for attempt in range(2):
        try:
            last = pg.evaluate(_API_FETCH_JS, [_SEARCH_API, payload]) or {}
        except Exception as e:
            last = {"status": 0, "text": str(e)[:120]}
        if last.get("status") == 200 and last.get("text"):
            return last
        # 页内 fetch 失败多半是页面被挑战页顶掉/跨域 → 重建一次页面上下文再试
        try:
            browser.goto_retry(pg, _BASE, timeout=60000, wait=3000)
            pg.wait_for_timeout(2000)
        except Exception:
            pass
    return last


def search_candidates(city: str, state: str, price_min: int, price_max: int,
                      limit: int = 60, tag: str = "search", page: int = 1) -> dict:
    """按城市/州采集 Crexi 出租房源（走官方 JSON 接口，一页 60 条）。

    接口失效（非 200 / 解析失败）时自动回退到旧搜索页解析，保证不断流。
    """
    import json as _json
    import math

    page_no = max(int(page or 1), 1)
    size = _API_PAGE_SIZE
    try:
        pg = _api_page()
        res = _api_search(pg, city, state, page_no, size)
        if res.get("status") != 200:
            raise RuntimeError(f"接口返回 {res.get('status')}")
        data = _json.loads(res.get("text") or "{}")
    except Exception as e:
        fb = _search_candidates_html(city, state, price_min, price_max,
                                     limit=limit, tag=tag, page=page_no)
        fb["message"] = f"（接口不可用：{e}，已回退搜索页解析）" + fb.get("message", "")
        return fb

    items = data.get("items") or []
    total = int(data.get("totalCount") or 0)
    total_pages = max(math.ceil(total / size), 1) if total else 0
    candidates, seen = [], set()
    for it in items:
        # Crexi 接口会混入外州的推荐/赞助房源（实测搜 Louisville 出现 Newark, NJ）→
        # 按州过滤，避免跨州房源混进任务
        addrs = it.get("address") or []
        st = (addrs[0].get("stateCode") if addrs and isinstance(addrs[0], dict) else "") or ""
        if st and state and st.upper() != state.upper():
            continue
        c = _candidate_from_item(it)
        addr = c["address"]
        if not addr:
            continue
        key = store_norm(addr)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(c)
        if len(candidates) >= limit:
            break

    if not items:
        return {"status": "warn",
                "message": f"Crexi 接口第 {page_no} 页无候选（共 {total} 条 / "
                           f"{total_pages} 页），已超出真实页数" if total else
                           f"Crexi 接口在 {city}, {state} 没搜到出租房源（城市名需具体）",
                "candidates": [], "screenshot": "", "total_pages": total_pages,
                "not_found": page_no > 1}

    tested = sum(1 for c in candidates if any(s.get("monthly") for s in c["spaces"]))
    msg = (f"Crexi 接口第 {page_no}/{total_pages} 页采集到 {len(candidates)} 个候选"
           f"（该条件共 {total} 条房源；其中 {tested} 个自带月租，可直接判价）")
    return {"status": "pass", "message": msg, "candidates": candidates,
            "screenshot": "", "total_pages": total_pages}


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


_PHOTOS_JS = r"""
(limit) => {
    const urls = [];
    const og = document.querySelector("meta[property='og:image']");
    if (og && og.content) urls.push(og.content);
    for (const img of document.querySelectorAll('img')) {
        const s = img.currentSrc || img.src || '';
        if (!s.startsWith('http')) continue;
        if (/logo|icon|sprite|avatar|placeholder|favicon/i.test(s)) continue;
        if (img.naturalWidth && img.naturalWidth < 500) continue;
        urls.push(s);
        if (urls.length >= limit) break;
    }
    return [...new Set(urls)];
}
"""


def _photo_urls(pg, limit: int = 8) -> list:
    """在当前详情页取房源大图 URL（不下载，只记 URL，价格过关后再下）。"""
    try:
        urls = pg.evaluate(_PHOTOS_JS, limit) or []
    except Exception:
        return []
    return [u for u in urls if isinstance(u, str) and u.startswith("http")]


def fetch_broker_info(listing_url: str) -> dict:
    """详情页补抓 价格/经纪人/面积 + 各套间面积与单价（spaces）+ 房源图 URL。
    电话/邮箱未登录打码 → 返回空串（不猜、不伪造）。

    顺带取图：价格过关后直接下这些 URL，省掉再开一次同一张详情页（约 10s/条）。
    """
    info = {"price": "", "agent": "", "phone": "", "email": "", "area": "",
            "spaces": [], "photo_urls": []}
    if not listing_url:
        return info
    pg = browser.new_page()
    try:
        # 不再固定空等：实测价格/套间表约 2.4s 就绪，改为"等元素出现即走"
        # （最多再等 8s；拦截页会一直等不到，仍走原有失败路径）
        browser.goto_retry(pg, listing_url, timeout=60000, wait=800)
        # 只等「套间表」元素：价格元素先出现时套间/经纪人还没渲染完（实测会解析出空中介、
        # 价格里混进 "| 507 days on market"），所以以 space 为准，等到就走（最多约 6.4s）
        for _ in range(8):
            try:
                ready = pg.evaluate("() => document.querySelectorAll('[data-cy=space]').length")
            except Exception:
                ready = 0
            if ready:
                break
            pg.wait_for_timeout(800)
        cand = _detail_candidate(pg, listing_url)
        for k in ("price", "agent", "area"):
            info[k] = cand.get(k, "")
        info["spaces"] = cand.get("spaces") or []
        # 图片多为懒加载：轻滚一屏再取 URL（实测 800ms 大图基本齐）
        try:
            pg.mouse.wheel(0, 900)
            pg.wait_for_timeout(800)
        except Exception:
            pass
        info["photo_urls"] = _photo_urls(pg)
    except Exception:
        pass
    finally:
        browser.safe_close(pg)
    return info


def store_norm(addr: str) -> str:
    from .. import store
    return store.normalize(addr)
