"""步骤1（数据源）：CommercialCafe 出租房源采集（列表页 + 详情页）。

实测结构（Louisville, KY，2026-11）：
  * 列表页 /commercial-real-estate/us/<st>/<city>/?listingType=lease
    （带 for-lease 路径的 /commercial-real-estate/.../for-lease/ 会被 Cloudflare 拦）
  * 首次访问常拿到 "Just a moment..." 挑战页，等 5~15s 会自动放行（cookie 存 profile）
  * 卡片（详情链接 a[href*='/commercial-property/'] 的祖先容器）文案：
        One Oxmoor Place
        101 Bullitt Lane, Oxmoor, Louisville, KY
        Space Type | Office
        For Lease | $26.00/SF/YR
        3 spaces available / 2,933 - 4,836 SF
  * 详情页套间块：
        Suite 107 / Space Type / Office / Lease Rate / $26.00/SF/YR / Total Space / 4,836 SF
  * 经纪人：Contacts 后两行（姓名 + 公司），**没有电话**（表单询价）
"""
import re

from .. import browser
from .loopnet import monthly_price

_BASE = "https://www.commercialcafe.com"
DETAIL_IS_PRICE_SOURCE = True
_PROFILE = "chrome_profile_feeds"

_BLOCK_KEYWORDS = ("access denied", "are you a robot", "unusual traffic", "captcha",
                   "verify you are human", "too many requests", "security verification",
                   "performing security verification")

_CARDS_JS = r"""
() => {
    const out = [];
    const seen = new Set();
    for (const a of document.querySelectorAll("a[href*='/commercial-property/']")) {
        const href = a.href.split("?")[0];
        if (seen.has(href)) continue;
        seen.add(href);
        let el = a, best = "";
        for (let i = 0; i < 6 && el; i++) {
            el = el.parentElement;
            const t = el ? (el.innerText || "") : "";
            if (t.length > best.length && t.length < 700) best = t;
        }
        out.push({url: href, text: best.slice(0, 500)});
        if (out.length >= 40) break;
    }
    return out;
}
"""

_DETAIL_JS = r"""
() => {
    const body = document.body.innerText || "";
    return {body: body.slice(0, 8000),
            tel: "",
            mail: ""};
}
"""


def _city_path(city: str, state: str, base: str = "") -> str:
    c = re.sub(r"[^a-z0-9]+", "-", city.strip().lower()).strip("-")
    return f"{base or _BASE}/commercial-real-estate/us/{state.strip().lower()}/{c}/"


def _list_url(city: str, state: str, page: int, base: str = "") -> str:
    """分页参数是 **Page（大写 P）**：?listingType=lease&Page=2。
    小写 page 无效，会永远返回第 1 页（实测翻页后内容完全一样）。"""
    url = _city_path(city, state, base)
    q = "?listingType=lease" + (f"&Page={page}" if page > 1 else "")
    return url + q


_PAGES_JS = r"""
() => {
    const nums = [...document.querySelectorAll("a[href]")]
        .map(a => a.getAttribute("href") || "")
        .map(h => { const m = /[?&]page=(\d+)/i.exec(h); return m ? +m[1] : 0; })
        .filter(n => n > 0);
    const body = document.body.innerText || "";
    let total = 0;
    // "1-30 of 459" 形式最准
    const rng = /([\d,]+)\s*[-–]\s*([\d,]+)\s*of\s*([\d,]+)/i.exec(body);
    if (rng) total = +rng[3].replace(/,/g, "");
    if (!total) {
        // "365 listings" / "459 Properties" / "128 results"：
        // 页面上可能同时有「30 results（本页条数）」和「459 listings（总数）」→ 取最大
        const all = [...body.matchAll(/([\d,]+)\s*(?:listings?|properties|results)\b/gi)]
            .map(m => +m[1].replace(/,/g, "")).filter(n => n > 0);
        if (all.length) total = Math.max(...all);
    }
    return {maxPage: nums.length ? Math.max(...nums) : 0, totalListings: total};
}
"""


_ADDR_RE = re.compile(r"(.+?),\s*([A-Za-z .]+),\s*([A-Z]{2})\s*$")
_PRICE_RE = re.compile(r"\$[\d,.]+\s*(?:-\s*\$?[\d,.]+\s*)?(?:/SF/(?:YR|MO)|/MO|/YR)", re.I)
_SIZE_RE = re.compile(r"([\d,]+)\s*(?:-\s*([\d,]+))?\s*SF")


def _parse_card(text: str) -> dict:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    addr = price = area = ""
    for i, ln in enumerate(lines):
        if not addr and re.search(r",\s*[A-Z]{2}\s*(\d{5})?$", ln) and "," in ln:
            addr = ln
            continue
        if not price and ("$" in ln or "contact for pricing" in ln.lower()):
            m = _PRICE_RE.search(ln)
            price = m.group(0) if m else ("" if "contact" in ln.lower() else "")
            continue
        if not area and "SF" in ln.upper():
            m = _SIZE_RE.search(ln)
            if m:
                area = (m.group(1) or "").replace(",", "")
    undisclosed = "contact for pricing" in (text or "").lower() or not price
    return {"address": re.sub(r"\s+", " ", addr).strip(" |,"),
            "price": price, "area": area, "undisclosed": undisclosed}


def _spaces_from_body(body: str) -> list:
    """套间块：Suite 107 / Lease Rate / $26.00/SF/YR / Total Space / 4,836 SF"""
    out = []
    lines = [ln.strip() for ln in (body or "").splitlines()]
    for i, ln in enumerate(lines):
        if not re.match(r"^(suite|ste|unit|space)\b", ln, re.I):
            continue
        # "Space Type" / "Spaces Available" 是字段标题，不是套间名
        if re.match(r"^(space types?|spaces? available)", ln, re.I):
            continue
        name = ln
        rate = size = ""
        for nxt in lines[i + 1:i + 12]:
            if not rate:
                m = _PRICE_RE.search(nxt)
                if m:
                    rate = m.group(0).strip()
                    continue
            if not size:
                m = _SIZE_RE.search(nxt)
                if m and re.search(r"sf", nxt, re.I):
                    size = (m.group(1) or "").replace(",", "")
            if rate and size:
                break
        if not size:
            continue
        monthly = monthly_price(rate, size) if rate else None
        out.append({"name": name, "size": size, "rate": rate, "monthly": monthly})
    return out


def _agent_from_body(body: str) -> str:
    """Contacts 区块取姓名 + 公司；跳过 Location / POI / Property 这些标签行
    （实测直接取 i+1/i+2 会得到「Location（POI）」这种脏结果）。"""
    junk = re.compile(r"^(location|poi|property information|property|property type|contacts?|"
                      r"contact listing broker|for lease|space|spaces|suite|see details|"
                      r"contact|ebrochure|\$)", re.I)
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    # "Contact Listing Broker" 区块最可靠（后面紧跟姓名 + 公司）；
    # 退回 "Contacts"（列表页详情里也有一处）
    for anchor in ("contact listing broker", "contacts"):
        for i, ln in enumerate(lines):
            if ln.lower().strip(":") != anchor:
                continue
            for j in range(i + 1, min(i + 6, len(lines))):
                name = lines[j]
                if junk.match(name) or len(name) < 3:
                    continue
                company = ""
                for k in range(j + 1, min(j + 3, len(lines))):
                    cand = lines[k]
                    if junk.match(cand) or re.search(r"first name|email|phone", cand, re.I):
                        break
                    company = cand
                    break
                return f"{name}（{company}）" if company else name
    return ""


def _wait_challenge(pg, tries: int = 10) -> None:
    """Cloudflare 挑战页会自己放行，等标题恢复正常即可。"""
    for _ in range(tries):
        try:
            if "just a moment" not in (pg.title() or "").lower():
                return
        except Exception:
            pass
        pg.wait_for_timeout(1500)


def search_candidates(city: str, state: str, price_min: int, price_max: int,
                      limit: int = 30, tag: str = "search", page: int = 1,
                      base: str = "") -> dict:
    page_no = max(int(page or 1), 1)
    url = _list_url(city, state, page_no, base)
    shot = ""
    pages = 0
    cards: list = []
    pg = browser.new_page_in(_PROFILE)
    try:
        # Cloudflare 会随机给挑战页（同一 URL 第二次打开就正常）→ 最多重试 2 次
        for attempt in range(2):
            browser.goto_retry(pg, url, timeout=60000, wait=2000)
            _wait_challenge(pg)
            for _ in range(6):
                try:
                    cards = pg.evaluate(_CARDS_JS) or []
                except Exception:
                    cards = []
                if cards:
                    break
                pg.wait_for_timeout(2000)
            if cards:
                break
            pg.wait_for_timeout(3000)
        shot = browser.shot(pg, f"{tag}_commercialcafe")
        try:
            low = (pg.evaluate("() => document.body.innerText") or "").lower()
        except Exception:
            low = ""
        # 总页数：优先用「365 listings / 每页 30」算，退回分页条最大页码
        try:
            pgn = pg.evaluate(_PAGES_JS) or {}
        except Exception:
            pgn = {}
        _total_listings = int(pgn.get("totalListings") or 0)
        pages = ((_total_listings + 29) // 30) if _total_listings else int(pgn.get("maxPage") or 0)
        if not cards and any(k in low for k in _BLOCK_KEYWORDS):
            return {"status": "fail",
                    "message": "CommercialCafe 被 Cloudflare 拦截：稍后重试（挑战页需等 10~15s）",
                    "candidates": [], "screenshot": shot, "total_pages": 0}
        if not cards:
            return {"status": "warn",
                    "message": f"CommercialCafe 第 {page_no} 页无候选（{city}, {state}）",
                    "candidates": [], "screenshot": shot, "total_pages": pages,
                    "not_found": page_no > 1}
    except Exception as e:
        return {"status": "fail", "message": f"CommercialCafe 访问失败: {str(e)[:80]}",
                "candidates": [], "screenshot": shot, "total_pages": 0}
    finally:
        browser.safe_close(pg)

    candidates, seen = [], set()
    for c in cards:
        parsed = _parse_card(c.get("text", ""))
        addr = parsed["address"]
        if not addr or not re.search(r"\d", addr):
            continue
        key = re.sub(r"[^a-z0-9]+", "", addr.lower())
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "address": addr, "price": parsed["price"], "agent": "", "phone": "",
            "email": "", "area": parsed["area"], "undisclosed": parsed["undisclosed"],
            "listing_url": c.get("url", ""), "spaces": [],
        })
        if len(candidates) >= limit:
            break

    msg = (f"CommercialCafe 第 {page_no}/{pages or '?'} 页采集到 {len(candidates)} 个候选"
           if candidates else f"CommercialCafe 第 {page_no} 页未解析到候选")
    return {"status": "pass" if candidates else "warn", "message": msg,
            "candidates": candidates, "screenshot": shot, "total_pages": pages}


def fetch_broker_info(listing_url: str) -> dict:
    info = {"price": "", "agent": "", "phone": "", "email": "", "area": "",
            "spaces": [], "photo_urls": []}
    if not listing_url:
        return info
    pg = browser.new_page_in(_PROFILE)
    try:
        browser.goto_retry(pg, listing_url, timeout=60000, wait=1500)
        _wait_challenge(pg)
        body = ""
        for _ in range(6):
            body = pg.evaluate("() => document.body.innerText") or ""
            if "spaces available" in body.lower() or "for lease" in body.lower():
                break
            pg.wait_for_timeout(1500)
        info["spaces"] = _spaces_from_body(body)
        info["agent"] = _agent_from_body(body)
        m = re.search(r"(?:For Lease|Lease Rate)\s*\n*\s*(\$[\d,.]+\s*/SF/(?:YR|MO))", body, re.I)
        if not m:
            m = re.search(r"(\$[\d,.]+\s*(?:-\s*\$?[\d,.]+\s*)?/SF/(?:YR|MO))", body, re.I)
        if m:
            info["price"] = m.group(1).strip()
        m = re.search(r"Property Size\s*\n*\s*([\d,]+)\s*SF", body, re.I)
        if m:
            info["area"] = m.group(1).replace(",", "")
        try:
            info["photo_urls"] = pg.evaluate(
                "() => [...new Set([...document.querySelectorAll('img')].map(i=>i.currentSrc||i.src)"
                ".filter(s=>s.startsWith('http') && !/logo|icon|sprite/i.test(s)))].slice(0,8)") or []
        except Exception:
            pass
    except Exception:
        pass
    finally:
        browser.safe_close(pg)
    return info


def candidates_from_urls(urls: list) -> dict:
    out = []
    for u in urls or []:
        u = (u or "").strip()
        if not u:
            continue
        info = fetch_broker_info(u)
        if not info.get("price") and not info.get("agent"):
            continue
        out.append({"address": "", "price": info.get("price", ""), "agent": info.get("agent", ""),
                    "phone": info.get("phone", ""), "email": info.get("email", ""),
                    "area": info.get("area", ""), "undisclosed": False,
                    "listing_url": u, "spaces": info.get("spaces") or []})
    return {"status": "pass" if out else "warn",
            "message": f"手动链接解析出 {len(out)} 个候选", "candidates": out, "screenshot": ""}
