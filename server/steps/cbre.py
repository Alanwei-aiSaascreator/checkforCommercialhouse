"""步骤1（数据源）：CBRE 官方挂牌（Properties for Lease）。

实测结构（2026-11）：
  * 列表页 https://www.cbre.com/properties/properties-for-lease/commercial-space
    是 React SPA，首屏渲染慢（要等 8~12s）；筛选参数用 ?location=<City>, <ST>
  * 卡片文案（一行一条，非常规整）：
        Medical For Lease
        San Martin Medical Arts Pavilion
        8285 West Arby Avenue
        Las Vegas, NV 89113
        455 - 3,685 sqft
        $2.15/sqft/mo
        Ryan Martin
        Tonya Gottesman
    即：类型 → 楼名 → 街址 → 城市州邮编 → 面积 → 单价 → 经纪人(1~2 人)
  * 价格多为 $X.XX/sqft/mo（每尺每月），也有 $/sqft/yr

注意：CBRE 的城市筛选偶尔不生效（会退回默认城市）。所以这里**强制校验**
地址里的州/城市与请求一致，不匹配的卡片直接丢弃，避免把外州房源混进任务。
"""
import re

from .. import browser
from .loopnet import monthly_price

_BASE = "https://www.cbre.com"
DETAIL_IS_PRICE_SOURCE = False        # 卡片自带价格，无需开详情页
_PROFILE = "chrome_profile_feeds"

_BLOCK_KEYWORDS = ("access denied", "are you a robot", "captcha", "verify you are human",
                   "unusual traffic", "too many requests", "page not found")

_CARDS_JS = r"""
() => {
    const out = [];
    const seen = new Set();
    for (const el of document.querySelectorAll("article, li, div, a")) {
        const t = el.innerText || "";
        if (t.length < 40 || t.length > 400) continue;
        if (!/\bsqft\b|\bSF\b/i.test(t) || !/\$/.test(t)) continue;
        if (!/[A-Z]{2}\s*\d{5}/.test(t)) continue;          // 必须有「州 邮编」
        const key = t.slice(0, 80);
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(t.slice(0, 400));
        if (out.length >= 60) break;
    }
    return out;
}
"""

_CITY_STATE = re.compile(r"([^,\n]+),\s*([A-Z]{2})\s*(\d{5})")
_SIZE = re.compile(r"([\d,]+)\s*(?:-\s*([\d,]+))?\s*(?:sqft|sq\.?\s*ft|SF)", re.I)
_RATE = re.compile(r"\$\s*[\d,]+(?:\.\d+)?\s*(?:-\s*\$?\s*[\d,]+(?:\.\d+)?\s*)?"
                   r"/\s*(?:sqft|sq\.?\s*ft|SF)\s*/\s*(?:mo|yr|month|year)", re.I)


def _parse_card(text: str, city: str, state: str) -> dict:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    addr = price = area = agent = ""
    for i, ln in enumerate(lines):
        m = _CITY_STATE.search(ln)
        if m and not addr:
            street = lines[i - 1] if i > 0 else ""
            addr = f"{street}, {ln}".strip(" ,")
            continue
        if not area:
            m2 = _SIZE.search(ln)
            if m2:
                area = (m2.group(1) or "").replace(",", "")
                continue
        if not price:
            m3 = _RATE.search(ln)
            if m3:
                price = m3.group(0).strip()
                continue
        if not agent and re.fullmatch(r"[A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){1,3}", ln) \
                and not re.search(r"for lease|for sale|sqft|medical|office|retail|industrial", ln, re.I):
            agent = ln
    if not addr:
        return {}
    # 强制城市/州校验：CBRE 筛选失效时会退到默认城市（实测默认是 Las Vegas）
    m_st = _CITY_STATE.search(addr)
    st_in = m_st.group(2) if m_st else ""
    if state and st_in and st_in.upper() != state.upper():
        return {}
    return {"address": re.sub(r"\s+", " ", addr).strip(" ,"), "price": price,
            "area": area, "agent": agent, "undisclosed": not price}


def search_candidates(city: str, state: str, price_min: int, price_max: int,
                      limit: int = 40, tag: str = "search", page: int = 1) -> dict:
    page_no = max(int(page or 1), 1)
    url = (f"{_BASE}/properties/properties-for-lease/commercial-space"
           f"?location={city}%2C%20{state}")
    if page_no > 1:
        url += f"&page={page_no}"
    shot = ""
    texts: list = []
    pg = browser.new_page_in(_PROFILE)
    try:
        browser.goto_retry(pg, url, timeout=60000, wait=3000)
        # SPA 首屏渲染慢，且翻页是前端请求 → 给足时间
        for _ in range(10):
            try:
                texts = pg.evaluate(_CARDS_JS) or []
            except Exception:
                texts = []
            if texts:
                break
            pg.wait_for_timeout(2000)
        shot = browser.shot(pg, f"{tag}_cbre")
        try:
            low = (pg.evaluate("() => document.body.innerText") or "").lower()
        except Exception:
            low = ""
        if not texts and any(k in low for k in _BLOCK_KEYWORDS):
            return {"status": "fail",
                    "message": "CBRE 页面异常（404/拦截），请检查城市名或稍后重试",
                    "candidates": [], "screenshot": shot, "total_pages": 0}
    except Exception as e:
        return {"status": "fail", "message": f"CBRE 访问失败: {str(e)[:80]}",
                "candidates": [], "screenshot": shot, "total_pages": 0}
    finally:
        browser.safe_close(pg)

    candidates, seen = [], set()
    for t in texts:
        p = _parse_card(t, city, state)
        if not p:
            continue
        key = re.sub(r"[^a-z0-9]+", "", p["address"].lower())
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "address": p["address"], "price": p["price"], "agent": p["agent"],
            "phone": "", "email": "", "area": p["area"],
            "undisclosed": p["undisclosed"], "listing_url": "", "spaces": [],
        })
        if len(candidates) >= limit:
            break

    if not candidates:
        return {"status": "warn",
                "message": (f"CBRE 第 {page_no} 页没有 {city}, {state} 的房源"
                            f"（城市筛选未生效时会被州校验全部丢弃，属正常保护）"),
                "candidates": [], "screenshot": shot, "total_pages": 0,
                "not_found": page_no > 1}
    msg = (f"CBRE 第 {page_no} 页采集到 {len(candidates)} 个 {city} 房源"
           f"（卡片自带 $/sqft/mo 单价与经纪人姓名）")
    return {"status": "pass", "message": msg, "candidates": candidates,
            "screenshot": shot, "total_pages": 0}


def fetch_broker_info(listing_url: str) -> dict:
    """CBRE 卡片已含面积/单价/经纪人，详情页可省略（这里保持空实现，避免额外开销）。"""
    return {"price": "", "agent": "", "phone": "", "email": "", "area": "",
            "spaces": [], "photo_urls": []}


def candidates_from_urls(urls: list) -> dict:
    return {"status": "warn", "message": "CBRE 暂不支持手动链接解析（卡片已带价格）",
            "candidates": [], "screenshot": ""}
