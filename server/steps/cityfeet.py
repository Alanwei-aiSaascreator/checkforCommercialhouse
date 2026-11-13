"""步骤1（数据源）：CityFeet 商业地产出租房源采集（搜索页 + 详情页）。

与 LoopNet / Crexi 同构：返回同样的候选结构
  candidates: [{address, price, agent, phone, email, area, listing_url, spaces}]

实测结构（Louisville, KY）：
  * 列表页 /cont/<city-slug>/office-space-for-lease?pgnum=N，每页 30 条
    （`?page=2` 无效、`/2` 报 0 条，真正的翻页参数是 pgnum）
  * 卡片是 <article>，**内部没有链接**，详情 URL 在祖先 <a> 上
    （article.closest('a[href*="/cont/listing/"]')）
  * 卡片文案：街址 / 城市, 州 邮编 / 价格 / 面积 / Built 年份 / 类型
  * 详情页顶部直接给「每月总租金区间」：$938 - $1,500 USD/MO
  * 套间表是 <tr>：1st Floor Ste 12 | Office | 1,500 SF | $12.00 USD/SF/YR
  * 经纪人姓名 + 电话是明文（tel: 链接），比 Crexi 友好（Crexi 打码）

价格口径以详情页为准（卡片多为 $/SF/YR，面积是区间，按它折算会误杀小套间）。
"""
import re

from .. import browser
from .loopnet import _parse_area, monthly_price

_BASE = "https://www.cityfeet.com"
DETAIL_IS_PRICE_SOURCE = True
# 主配置带着 Crexi 登录态，也因此被 Akamai 记住：实测 CityFeet 对主配置直接
# Access Denied，全新配置 200。免登录数据源统一走独立配置目录。
_PROFILE = "chrome_profile_feeds"

_BLOCK_KEYWORDS = ("access denied", "are you a robot", "unusual traffic", "captcha",
                   "verify you are human", "too many requests", "just a moment",
                   "security verification", "checking your browser")

_PTYPE = "office"          # office / industrial / retail / flex

_CARDS_JS = r"""
() => [...document.querySelectorAll("article")].map(a => {
    const link = a.closest('a[href*="/cont/listing/"]');
    return {url: link ? link.href : "", text: (a.innerText || "").slice(0, 400)};
}).filter(c => c.text)
"""

_PAGES_JS = r"""
() => {
    const nums = [...document.querySelectorAll("a[href*='pgnum']")]
        .map(a => parseInt((/pgnum=(\d+)/.exec(a.getAttribute("href") || "") || [])[1] || 0))
        .filter(n => n > 0);
    return nums.length ? Math.max(...nums) : 0;
}
"""

_DETAIL_JS = r"""
() => {
    const body = document.body.innerText || "";
    const rows = [...document.querySelectorAll("tr")].map(t =>
        [...t.querySelectorAll("td,th")].map(c => (c.innerText || "").trim()).filter(Boolean))
        .filter(r => r.length >= 3 && r.some(c => /sq\s*ft|sf/i.test(c)));
    const tel = document.querySelector("a[href^='tel:']");
    const mail = document.querySelector("a[href^='mailto:']");
    return {body: body.slice(0, 6000), rows: rows.slice(0, 20),
            tel: tel ? tel.getAttribute("href").replace("tel:", "") : "",
            mail: mail ? mail.getAttribute("href").replace("mailto:", "") : ""};
}
"""


def _slug(city: str, state: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{city}".strip().lower()).strip("-")


def _list_url(city: str, page: int) -> str:
    url = f"{_BASE}/cont/{_slug(city, '')}/{_PTYPE}-space-for-lease"
    return f"{url}?pgnum={page}" if page > 1 else url


_CITY_LINE = re.compile(r"^(.+?),\s*([A-Z]{2})\s+(\d{5})")
_PRICE_LINE = re.compile(r"\$[\d,.]+\s*(?:-\s*\$?[\d,.]+\s*)?(?:USD)?\s*/?\s*(?:SF/?)?"
                         r"(?:YEAR|YR|MO|MONTH)?", re.I)
_SIZE = re.compile(r"([\d,]+)\s*(?:-\s*([\d,]+))?\s*SF", re.I)


def _parse_card(text: str) -> dict:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    street, addr, price, area = "", "", "", ""
    for i, ln in enumerate(lines):
        if not addr and _CITY_LINE.match(ln) and i > 0:
            street = lines[i - 1]
            addr = f"{street}, {ln}"
            continue
        if not price:
            m = _PRICE_LINE.search(ln)
            if m and "$" in ln:
                price = m.group(0).strip()
                continue
        if not area and "SF" in ln.upper() and "CONTIGUOUS" not in ln.upper():
            m = _SIZE.search(ln)
            if m:
                area = (m.group(1) or "").replace(",", "")
    undisclosed = "rate upon request" in (text or "").lower() or not price
    return {"address": re.sub(r"\s+", " ", addr).strip(" |,"), "price": price,
            "area": area, "undisclosed": undisclosed}


def _monthly_from_text(text: str):
    """详情页 "$938 - $1,500 USD/MO" → 138?? 取区间下限；没有 MO 单位返回 None。"""
    for m in re.finditer(r"\$\s*[\d,]+(?:\.\d+)?\s*(?:-\s*\$?\s*[\d,]+(?:\.\d+)?)?"
                         r"\s*USD\s*/\s*MO", text or "", re.I):
        nums = re.findall(r"[\d,]+(?:\.\d+)?", m.group(0))
        vals = []
        for n in nums:
            try:
                vals.append(float(n.replace(",", "")))
            except ValueError:
                pass
        if vals:
            return min(vals), m.group(0).strip()
    return None, ""


def _spaces_from_rows(rows: list) -> list:
    """tr 行 → [{name,size,rate,monthly}]。行例：[1st Floor Ste 12, Office, 1,500 SF, $12.00 USD/SF/YR]"""
    out = []
    for r in rows or []:
        name = r[0]
        size = ""
        rate = ""
        for c in r[1:]:
            if not size and re.search(r"([\d,]+)\s*SF", c, re.I):
                m = re.search(r"([\d,]+)\s*SF", c, re.I)
                size = m.group(1).replace(",", "")
            elif not rate and "$" in c:
                rate = c.strip()
            elif not rate and "request" in c.lower():
                rate = ""
        if not size:
            continue
        monthly = None
        if rate:
            monthly = monthly_price(rate, size)
        out.append({"name": name, "size": size, "rate": rate, "monthly": monthly})
    return out


def _agent_from_body(body: str) -> str:
    """详情页经纪人区块：姓名在上、经纪公司在下（实测 Sohan Nigam / 502-305-7711 / Nigam Retail Complex）。"""
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.fullmatch(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", ln):
            name = lines[i - 1] if i > 0 else ""
            company = lines[i + 1] if i + 1 < len(lines) else ""
            if company and re.search(r"first name|last name|phone|email", company, re.I):
                company = ""
            return f"{name}（{company}）" if name and company else (name or company)
    return ""


def search_candidates(city: str, state: str, price_min: int, price_max: int,
                      limit: int = 30, tag: str = "search", page: int = 1) -> dict:
    page_no = max(int(page or 1), 1)
    url = _list_url(city, page_no)
    shot = ""
    cards: list = []
    pg = browser.new_page_in(_PROFILE)
    try:
        browser.goto_retry(pg, url, timeout=60000, wait=2000)
        for _ in range(10):
            try:
                cards = pg.evaluate(_CARDS_JS) or []
            except Exception:
                cards = []          # 首次打开配置目录时页面还在跳转，evaluate 会中断
            if cards:
                break
            pg.wait_for_timeout(1500)
        shot = browser.shot(pg, f"{tag}_cityfeet")
        try:
            low = (pg.evaluate("() => document.body.innerText") or "").lower()
        except Exception:
            low = ""
        # 分页条最大页码（?pgnum=N）。读不到会返回 0 → pipeline 会退化为逐页探测
        try:
            total = int(pg.evaluate(_PAGES_JS) or 0)
        except Exception:
            total = 0
        if any(k in low for k in _BLOCK_KEYWORDS):
            return {"status": "fail",
                    "message": "CityFeet 拦截（验证码/限流）：请稍后重试或换节点",
                    "candidates": [], "screenshot": shot, "total_pages": 0}
        if not cards:
            not_found = ("404" in (pg.url or "")) or ("page not found" in low)
            return {"status": "warn",
                    "message": (f"CityFeet 第 {page_no} 页无候选"
                                f"（{city} 可能没有该类型房源，或已超出末页）"),
                    "candidates": [], "screenshot": shot, "total_pages": 0,
                    "not_found": not_found or page_no > 1}
    except Exception as e:
        return {"status": "fail", "message": f"CityFeet 访问失败: {str(e)[:80]}",
                "candidates": [], "screenshot": shot, "total_pages": 0}
    finally:
        browser.safe_close(pg)

    candidates, seen = [], set()
    for c in cards:
        parsed = _parse_card(c.get("text", ""))
        addr = parsed["address"]
        if not addr or re.search(r"^\D*$", addr):
            continue
        key = re.sub(r"[^a-z0-9]+", "", addr.lower())
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
            "undisclosed": parsed["undisclosed"],
            "listing_url": c.get("url", ""),
            "spaces": [],
        })
        if len(candidates) >= limit:
            break

    msg = (f"CityFeet 第 {page_no}/{total or '?'} 页采集到 {len(candidates)} 个候选"
           f"（{city} {_PTYPE} 出租；价格以详情页为准）"
           if candidates else f"CityFeet 第 {page_no} 页未解析到候选")
    return {"status": "pass" if candidates else "warn", "message": msg,
            "candidates": candidates, "screenshot": shot, "total_pages": total}


def fetch_broker_info(listing_url: str) -> dict:
    """详情页补抓：月租区间 / 套间表 / 经纪人 / 电话 / 邮箱。"""
    info = {"price": "", "agent": "", "phone": "", "email": "", "area": "",
            "spaces": [], "photo_urls": []}
    if not listing_url:
        return info
    pg = browser.new_page_in(_PROFILE)
    try:
        browser.goto_retry(pg, listing_url, timeout=60000, wait=1500)
        data = {}
        for _ in range(8):
            data = pg.evaluate(_DETAIL_JS) or {}
            if (data.get("rows") or []) or data.get("body"):
                break
            pg.wait_for_timeout(1000)
        body = data.get("body") or ""
        monthly, price_txt = _monthly_from_text(body)
        info["spaces"] = _spaces_from_rows(data.get("rows") or [])
        if monthly is not None:
            info["price"] = price_txt
        else:
            m = re.search(r"\$[\d,.]+\s*(?:-\s*\$?[\d,.]+\s*)?(?:USD)?\s*/?\s*SF\s*/?\s*YR",
                          body, re.I)
            if m:
                info["price"] = m.group(0).strip()
        info["phone"] = (data.get("tel") or "").strip()
        info["email"] = (data.get("mail") or "").strip()
        info["agent"] = _agent_from_body(body)
        m = re.search(r"Total Building Size\s*:?\s*([\d,]+)\s*SF", body, re.I)
        if m:
            info["area"] = m.group(1).replace(",", "")
    except Exception:
        pass
    finally:
        browser.safe_close(pg)
    return info


def candidates_from_urls(urls: list) -> dict:
    """手动链接兜底：逐条打开 CityFeet 详情页解析候选。"""
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
