"""步骤1（数据源）：OfficeSpace.com 办公空间出租采集。

实测结构（Louisville, KY）：
  * 列表页 /<state>/<city>，例如 /ky/louisville（页头显示 "9 Results"）
  * 详情链接：/<state>/<city>/<数字ID>-<slug>
      例 /ky/louisville/2339802-10200-linn-station-rd-2nd-floor
  * 卡片文案含 "1 Space, 1 ppl"、面积 SF、价格（$/MO 为主）
  * 站内筛选项本身就用 $X/MO 表达价格 → 卡片价格多为「每月总价」
"""
import re

from .. import browser
from .loopnet import monthly_price

_BASE = "https://www.officespace.com"
DETAIL_IS_PRICE_SOURCE = True
_PROFILE = "chrome_profile_feeds"

_BLOCK_KEYWORDS = ("access denied", "are you a robot", "captcha", "verify you are human",
                   "unusual traffic", "too many requests", "security verification")

_CARDS_JS = r"""
() => {
    const out = [];
    const seen = new Set();
    for (const a of document.querySelectorAll("a[href]")) {
        const h = a.getAttribute("href") || "";
        if (!/^\/[a-z]{2}\/[a-z0-9\-]+\/\d{3,}-/.test(h)) continue;   // /ky/louisville/2339802-xxx
        if (seen.has(h)) continue;
        seen.add(h);
        let el = a, best = "";
        for (let i = 0; i < 6 && el; i++) {
            el = el.parentElement;
            const t = el ? (el.innerText || "") : "";
            if (t.length > best.length && t.length < 800) best = t;
        }
        out.push({url: new URL(h, location.origin).href, text: best.slice(0, 500)});
        if (out.length >= 40) break;
    }
    return out;
}
"""

_PAGES_JS = r"""
() => {
    const nums = [...document.querySelectorAll("a[href*='page=']")]
        .map(a => { const m = /page=(\d+)/.exec(a.getAttribute("href") || ""); return m ? +m[1] : 0; })
        .filter(n => n > 0);
    const body = document.body.innerText || "";
    const m = /([\d,]+)\s*results?/i.exec(body);
    return {maxPage: nums.length ? Math.max(...nums) : 0,
            total: m ? +m[1].replace(/,/g, "") : 0};
}
"""

_PRICE_RE = re.compile(r"\$\s*[\d,]+(?:\.\d+)?\s*(?:-\s*\$?\s*[\d,]+(?:\.\d+)?\s*)?"
                       r"(?:/\s*(?:SF\s*/?\s*)?(?:MO|YR|MONTH|YEAR))?", re.I)
# 必须带周期的报价：$1,000/MO / From $1,294/MO / $18.50/SF/YR
_PRICE_MO_RE = re.compile(r"(?:from\s*)?\$\s*[\d,]+(?:\.\d+)?\s*(?:-\s*\$?\s*[\d,]+(?:\.\d+)?\s*)?"
                          r"/\s*(?:SF\s*/\s*)?(?:MO|YR|MONTH|YEAR)\b", re.I)
_SIZE_RE = re.compile(r"([\d,]+)\s*(?:-\s*([\d,]+))?\s*(?:sq\.?\s*ft|SF)", re.I)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")


def _list_url(city: str, state: str, page: int) -> str:
    url = f"{_BASE}/{_slug(state)}/{_slug(city)}"
    return f"{url}?page={page}" if page > 1 else url


_ADDR_RE = re.compile(
    r"(\d{2,6},?\s+[A-Za-z][A-Za-z0-9 .,'\-]{1,40}?\s(?:St|Street|Ave|Avenue|Dr|Drive|Rd|Road|"
    r"Blvd|Boulevard|Pkwy|Parkway|Way|Ln|Lane|Hwy|Highway|Pl|Place|Ct|Court|Cir|Circle|Pike|"
    r"Trail|Trl)\b)", re.I)

_STREET_RX = re.compile(r"(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|pkwy|way|"
                        r"ln|lane|hwy|highway|pl|place|ct|court|cir|circle)", re.I)


def _addr_from_url(url: str) -> str:
    """卡片文案常被楼名/价格挤成一团，URL slug 反而是最干净的地址来源：
    /ky/louisville/2339802-10200-linn-station-road-2nd-floor → 10200 Linn Station Road 2nd Floor"""
    m = re.search(r"/[a-z]{2}/[a-z0-9\-]+/\d+-([^/?#]+)", url or "")
    if not m:
        return ""
    words = m.group(1).replace("-", " ").split()
    return " ".join(w if w.isdigit() else w.capitalize() for w in words).strip()


def _parse_card(text: str, url: str = "") -> dict:
    text = text or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    addr = price = area = ""
    m_addr = _ADDR_RE.search(text)
    if m_addr:
        addr = re.sub(r"\s+", " ", m_addr.group(1)).strip()
    if not addr or re.fullmatch(r"[\d,]+", addr.strip()):
        # URL slug 只给出门牌号（/tn/nashville/2387843-501 → "501"）时算无效地址
        slug_addr = _addr_from_url(url)
        if slug_addr and not re.fullmatch(r"[\d,]+", slug_addr.strip()):
            addr = slug_addr
    for ln in lines:
        # 地址行：有数字 + 街名后缀，且不能是「1,146 Sq. Ft. 1st floor - $1,300/month」这类
        if (not addr and re.search(r"\d", ln) and _STREET_RX.search(ln)
                and not re.search(r"sq\.?\s*ft|\$|/month|/mo\b", ln, re.I)
                and len(ln) < 80):
            addr = re.sub(r"\s+", " ", ln).strip(" |,")
            continue
        if not price:
            # 只认「$1,000/MO」这种带周期的报价。页面顶部的价格筛选器
            # （"$129 | $180 | $400 | ..."）会被误当成房源价格，必须先排除
            if ln.count("$") > 1 or ln.count("|") > 1:
                continue
            m = _PRICE_MO_RE.search(ln)
            if m:
                price = m.group(0).strip()
                continue
        if not area:
            m = _SIZE_RE.search(ln)
            if m:
                area = (m.group(1) or "").replace(",", "")
    if not addr:
        for ln in lines:
            if re.match(r"^[\d,]+$", ln):
                addr = ln
            elif re.match(r"^[A-Z][\w .'-]{3,40}$", ln) and not addr:
                addr = ln
    undisclosed = not price
    return {"address": addr, "price": price, "area": area, "undisclosed": undisclosed}


def search_candidates(city: str, state: str, price_min: int, price_max: int,
                      limit: int = 40, tag: str = "search", page: int = 1) -> dict:
    page_no = max(int(page or 1), 1)
    url = _list_url(city, state, page_no)
    shot = ""
    pages = 0
    cards: list = []
    pg = browser.new_page_in(_PROFILE)
    try:
        browser.goto_retry(pg, url, timeout=60000, wait=2500)
        for _ in range(8):
            try:
                cards = pg.evaluate(_CARDS_JS) or []
            except Exception:
                cards = []
            if cards:
                break
            pg.wait_for_timeout(2000)
        shot = browser.shot(pg, f"{tag}_officespace")
        try:
            low = (pg.evaluate("() => document.body.innerText") or "").lower()
        except Exception:
            low = ""
        try:
            pgn = pg.evaluate(_PAGES_JS) or {}
        except Exception:
            pgn = {}
        _total = int(pgn.get("total") or 0)
        pages = ((_total + 29) // 30) if _total else int(pgn.get("maxPage") or 0)
        if not cards and any(k in low for k in _BLOCK_KEYWORDS):
            return {"status": "fail", "message": "OfficeSpace 被拦截（验证码/限流）",
                    "candidates": [], "screenshot": shot, "total_pages": 0}
        if not cards:
            nf = "page not found" in low or "404" in low
            return {"status": "warn",
                    "message": f"OfficeSpace 第 {page_no} 页无候选（{city}, {state}）",
                    "candidates": [], "screenshot": shot, "total_pages": pages,
                    "not_found": nf or page_no > 1}
    except Exception as e:
        return {"status": "fail", "message": f"OfficeSpace 访问失败: {str(e)[:80]}",
                "candidates": [], "screenshot": shot, "total_pages": 0}
    finally:
        browser.safe_close(pg)

    candidates, seen = [], set()
    for c in cards:
        p = _parse_card(c.get("text", ""), c.get("url", ""))
        addr = p["address"]
        if not addr or not re.search(r"\d", addr):
            continue
        key = re.sub(r"[^a-z0-9]+", "", addr.lower())
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "address": addr, "price": p["price"], "agent": "", "phone": "", "email": "",
            "area": p["area"], "undisclosed": p["undisclosed"],
            "listing_url": c.get("url", ""), "spaces": [],
        })
        if len(candidates) >= limit:
            break

    msg = (f"OfficeSpace 第 {page_no}/{pages or '?'} 页采集到 {len(candidates)} 个候选"
           if candidates else f"OfficeSpace 第 {page_no} 页未解析到候选")
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
        body = ""
        for _ in range(6):
            body = pg.evaluate("() => document.body.innerText") or ""
            if "$" in body or "sq" in body.lower():
                break
            pg.wait_for_timeout(1500)
        m = re.search(r"(\$[\d,.]+\s*(?:-\s*\$?[\d,.]+\s*)?/\s*(?:SF\s*/?\s*)?(?:MO|YR))", body, re.I)
        if m:
            info["price"] = m.group(1).strip()
        m = re.search(r"([\d,]+)\s*(?:sq\.?\s*ft|SF)", body, re.I)
        if m:
            info["area"] = m.group(1).replace(",", "")
        # 经纪人：姓名 + 经纪公司（OfficeSpace 多为 "Contact" 表单，有则取）
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        for i, ln in enumerate(lines):
            if re.match(r"^contact (?:listing )?(?:broker|agent|owner)$", ln, re.I):
                if i + 2 < len(lines):
                    info["agent"] = f"{lines[i + 1]}（{lines[i + 2]}）"
                break
        info["spaces"] = []
        if info["price"] and info["area"]:
            mp = monthly_price(info["price"], info["area"])
            if mp:
                info["spaces"] = [{"name": "整层/整栋", "size": info["area"],
                                   "rate": info["price"], "monthly": mp}]
        try:
            info["phone"] = (pg.evaluate(
                "() => (document.querySelector(\"a[href^='tel:']\")||{}).getAttribute?.('href')"
                " || ''") or "").replace("tel:", "")
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
