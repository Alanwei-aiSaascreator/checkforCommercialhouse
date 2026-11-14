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
# 详情页是唯一可信的价格口径：列表卡片只给「区域/楼栋级」的 $/sqft/mo，
# 真正的可租面积与套间在详情页的 Spaces Available 表里（实测 21976）。
DETAIL_IS_PRICE_SOURCE = True
_PROFILE = "chrome_profile_feeds"

_BLOCK_KEYWORDS = ("access denied", "are you a robot", "captcha", "verify you are human",
                   "unusual traffic", "too many requests", "page not found")

# 卡片链接：CBRE 用 Coveo 列表，每张卡是 <a class="cbre-c-pl-property-card-link">，
# href 形如 /properties/properties-for-lease/commercial-space/details/US-SMPL-217104/...
# 注意别写成 "properties/details"（中间隔着 properties-for-lease/commercial-space，
# 实测这条相邻匹配恒为 0，导致记录里一直没有房源网址）。
_CARDS_JS = r"""
() => {
    const out = [];
    const seen = new Set();
    const add = (url, text) => {
        if (!url || seen.has(url)) return;
        seen.add(url);
        out.push({url, text: (text || "").slice(0, 400)});
    };
    for (const a of document.querySelectorAll(
            "a.cbre-c-pl-property-card-link, a[href*='/commercial-space/details/'],"
            + " a[href*='/properties-for-lease/'][href*='/details/']")) {
        const href = a.getAttribute("href") || "";
        if (!/details\//i.test(href)) continue;
        const card = a.closest("[class*='CoveoResult']") || a.parentElement || a;
        add(new URL(href, location.origin).href, card.innerText);
        if (out.length >= 60) break;
    }
    if (out.length) return out;
    // 兜底：没有卡片链接时退回纯文本（新配置/降级页面可能只剩文本）
    for (const el of document.querySelectorAll("article, li, div")) {
        const t = el.innerText || "";
        if (t.length < 40 || t.length > 400) continue;
        if (!/\bsqft\b|\bSF\b/i.test(t) || !/\$/.test(t)) continue;
        if (!/[A-Z]{2}\s*\d{5}/.test(t)) continue;
        const key = t.slice(0, 80);
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({url: "", text: t.slice(0, 400)});
        if (out.length >= 60) break;
    }
    return out;
}
"""

# 卡片/详情页就绪判定（用于「纯文字页面」自愈：资源被拦时 reload 一次即正常）
_LIST_READY = "() => document.querySelectorAll(\"a.cbre-c-pl-property-card-link\").length > 0"


def _list_ready_js(state: str) -> str:
    """就绪 = 「目标州的卡片已出现」且「Lease 筛选已生效」。

    只判「有卡片」会踩两个坑：
    1) Coveo 先渲染默认城市的结果，location 筛选后到 → 抓到外州房源被全部丢弃；
    2) transactiontype 筛选后到 → 先渲染的是 All Transaction Types，
       会把 For Sale 的房源一起抓进来（实测 LA 先 24 条含在售，筛选生效后 18 条）。
    """
    st = re.sub(r"[^A-Za-z]", "", state or "")
    return ("() => {"
            "const lease = document.querySelector('#pl-facet-select-transactiontype-isLetting');"
            "if (lease && !lease.checked) return false;"
            "return [...document.querySelectorAll(\"a.cbre-c-pl-property-card-link\")]"
            ".some(a => new RegExp(',\\\\s*" + st + "\\\\s*\\\\d{5}')"
            ".test(((a.closest(\"[class*='CoveoResult']\") || a).innerText || '')));"
            "}")
_DETAIL_READY = ("() => document.querySelectorAll(\"button[class*='brokerContactLink']\").length > 0"
                 " || (document.body.innerText || '').includes('Spaces Available')")


def _open(pg, url: str, ready: str, tries: int = 3, wait: int = 7000,
          retry_wait: int = 5000) -> bool:
    """打开页面并确认真的渲染出来了；没渲染就 reload（CBRE 首次访问会拦 CSS/JS）。

    实测：首次 goto 时 mediaassets.cbre.com 的 pre-optimized-min.css/js 被
    ERR_BLOCKED_BY_RESPONSE.NotSameOrigin 拦掉 → 页面只剩纯文字、0 张卡片；
    reload 一次就完全正常（样表 9 → 18）。所以这里必须有自愈重试。
    """
    for i in range(tries):
        try:
            if i == 0:
                pg.goto(url, timeout=60000, wait_until="domcontentloaded")
            else:
                pg.reload(wait_until="domcontentloaded")
        except Exception:
            pass
        pg.wait_for_timeout(wait if i == 0 else retry_wait)
        try:
            if pg.evaluate(ready):
                return True
        except Exception:
            pass
    return False

_CITY_STATE = re.compile(r"([^,\n]+),\s*([A-Z]{2})\s*(\d{5})")
_SIZE = re.compile(r"([\d,]+)\s*(?:-\s*([\d,]+))?\s*(?:sqft|sq\.?\s*ft|SF)", re.I)
_RATE = re.compile(r"\$\s*[\d,]+(?:\.\d+)?\s*(?:-\s*\$?\s*[\d,]+(?:\.\d+)?\s*)?"
                   r"/\s*(?:sqft|sq\.?\s*ft|SF)\s*/\s*(?:mo|yr|month|year)", re.I)


def _parse_card(text: str, city: str, state: str) -> dict:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    # 兜底：卡片首行是交易类型（"Office For Lease"/"Industrial For Sale"）。
    # 即使筛选项失效也绝不把纯「For Sale」当租赁房源核查；
    # 但 "Office For Sale/Lease" 是租售双挂，必须保留。
    head = " ".join(lines[:2]).lower()
    if re.search(r"\bfor sale\b", head) and "lease" not in head:
        return {}
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
        if not agent and _person_like(ln) \
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
    # transactiontype=isLetting 是 CBRE 的「Lease」筛选项；不带这个参数时
    # 页面默认 transactiontype=allTypes，会把「For Sale」的房源一起返回
    # （实测 Nashville 第 1 页混进 Industrial For Sale）。
    url = (f"{_BASE}/properties/properties-for-lease/commercial-space"
           f"?location={city}%2C%20{state}&transactiontype=isLetting")
    if page_no > 1:
        url += f"&page={page_no}"
    shot = ""
    texts: list = []
    pg = browser.new_page_in(_PROFILE)
    try:
        # 首次访问 CBRE 常只拿到「纯文字」骨架（CSS/JS 被跨源拦截），_open 会 reload 自愈；
        # 就绪条件用「目标州的卡片已出现」，避免抓到筛选生效前的默认城市结果
        # 列表首屏实测要 12s 左右（Nashville t=4s 0 张、t=8s 0 张、t=12s 25 张），
        # 等待时间给不够就会误判成「本页没有房源」
        if not _open(pg, url, _list_ready_js(state), tries=3, wait=13000, retry_wait=9000):
            for _ in range(10):
                texts = pg.evaluate(_CARDS_JS) or []
                if texts:
                    break
                pg.wait_for_timeout(2500)
        # 筛选打完会重渲染列表，稳一下再抓（否则可能抓到过渡态）
        pg.wait_for_timeout(1500)
        texts = pg.evaluate(_CARDS_JS) or []
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
    for card in texts:
        text = card.get("text", "") if isinstance(card, dict) else str(card)
        url_ = card.get("url", "") if isinstance(card, dict) else ""
        p = _parse_card(text, city, state)
        if not p:
            continue
        key = re.sub(r"[^a-z0-9]+", "", p["address"].lower())
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "address": p["address"], "price": p["price"], "agent": p["agent"],
            "phone": "", "email": "", "area": p["area"],
            "undisclosed": p["undisclosed"],
            "listing_url": url_,      # 详情页链接：后面抓电话/邮箱/套间表靠它
            "spaces": [],
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
           f"（带详情页链接；价格/面积/电话/邮箱按详情页取，面议房源直接淘汰）")
    return {"status": "pass", "message": msg, "candidates": candidates,
            "screenshot": shot, "total_pages": 0}


# ---------- 详情页解析 ----------
# 实测（2026-11，US-SMPL-21976 Lakeview II）正文结构：
#   Lakeview II                     ← 楼名
#   15 Century Boulevard            ← 街址
#   Nashville, TN 37214             ← 城市 州 邮编
#   For Lease
#   10,746 - 40,000 sqft
#   Lease Price | $24.50            ← 无单位，CBRE 惯例是 $/SF/YR
#   ...
#   Sizes and Measurements
#   Minimum Space Available | 10,746 sqft
#   Total Space Available | 40,000 sqft
#   Spaces Available | Show Space Details
#   Suite 104 | 3,689 sqft | Status | Available | Lease Type | Direct
#   Suite 202 | 2,240 sqft | ...
#   Pricing Information | Lease Type | Direct | Lease Rate | $24.50
#   Get in touch | Frank Thomasson | Phone | Email | Ryan Coulter | ...

_DETAIL_JS = r"""
() => {
    const t = document.body.innerText || "";
    const imgs = [...document.querySelectorAll("img")]
        .map(i => i.currentSrc || i.src || "")
        .filter(s => s.startsWith("http") && s.includes("mediaassets.cbre.com")
                     && !/\/global\/|sustainability|insights|careers/i.test(s));
    return {text: t, imgs: imgs.slice(0, 10)};
}
"""

_AREA_LINE = re.compile(r"^([\d,]+)\s*(?:sq\.?\s*ft|sqft|SF)$", re.I)
_SUITE_LINE = re.compile(r"^(Suite|Ste|Unit|Floor|Building)\b\s*([A-Za-z0-9-]+)", re.I)
_PERSON = re.compile(r"^[A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){1,3}$")
# 经纪人卡片偶尔把楼名/公司名放在姓名位置（"Manhattan Beach Towers"、"The Torrance"），
# 这类词不可能是人名，混进来会让「中介」字段和电话邮箱对不上
_ORG_WORDS = re.compile(
    r"\b(towers?|buildings?|plaza|cent(?:er|re)|group|compan(?:y|ies)|partners?|"
    r"realty|propert(?:y|ies)|associates|brokers?|brokerage|capital|holdings|"
    r"investments?|management|llc|inc|ltd|corp|trust)\b", re.I)


def _person_like(name: str) -> bool:
    return (bool(name) and bool(_PERSON.fullmatch(name))
            and not _ORG_WORDS.search(name)
            and not re.search(r"\b(phone|email|fax|call|pricing|undisclosed|contact)\b",
                              name, re.I))


def _detail_lines(body: str) -> list:
    return [ln.strip() for ln in (body or "").splitlines() if ln.strip()]


_ADDR_TAIL = re.compile(
    r"(\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .,'#-]*?"
    r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Drive|Dr|Parkway|Pkwy|"
    r"Highway|Hwy|Lane|Ln|Place|Pl|Court|Ct|Circle|Cir|Pike|Trail|Trl|Way))", re.I)


def _clean_street(line: str) -> str:
    """从一行里取最靠后的完整街址。

    详情页标题会把楼名和街址粘成一行：CBRE 的 "5514 Satsuma 5514 Satsuma Avenue"
    —— 直接整行当地址会变成 "5514 Satsuma 5514 Satsuma Avenue, Los Angeles, CA"，
    Smarty 查不到。从每个「门牌号」起点试匹配，取最靠右（最短）的那个。
    """
    best = ""
    for m in re.finditer(r"(?<!\d)\d{1,6}\s+", line or ""):
        tail = (line or "")[m.start():].strip()
        mm = _ADDR_TAIL.fullmatch(tail)
        if mm:
            best = mm.group(1).strip(" ,")
    return best


_NAV_WORDS = re.compile(r"search results|skip to|properties for|for lease|about cbre|"
                        r"^\s*(?:office|retail|industrial|medical)\b", re.I)


def _detail_address(lines: list) -> tuple:
    """返回 (楼名, 地址)。锚点是「城市, 州 邮编」行，上一行必须是门牌行。"""
    for i, ln in enumerate(lines):
        if not re.fullmatch(r"[A-Za-z .'-]+,\s*[A-Z]{2}\s*\d{5}", ln):
            continue
        if i < 1:
            continue
        street = _clean_street(lines[i - 1])
        if not street:
            continue
        name = ""
        if i >= 2:
            cand = lines[i - 2]
            if (cand and len(cand) < 60 and not re.search(r"\d{3,}", cand)
                    and not _NAV_WORDS.search(cand)):
                name = cand
        return name, f"{street}, {ln}"
    return "", ""


def _detail_rate(lines: list) -> str:
    """「Lease Rate $24.50」这类无单位报价 → 补 /SF/YR（CBRE 挂牌惯例）。

    为什么必须补：loopnet.monthly_price 把「$24.50」这种 <100 且无单位的数字
    判为无法换算，直接转人工；补上单位后 24.50 × 面积 ÷ 12 才是真实月租。
    """
    for key in ("Lease Rate", "Lease Price", "Rent"):
        if key not in lines:
            continue
        j = lines.index(key)
        for ln in lines[j + 1:j + 4]:
            m = re.search(r"\$\s*[\d,]+(?:\.\d+)?\s*(?:/\s*[A-Za-z]+(?:\s*/\s*[A-Za-z]+)?)?", ln)
            if not m:
                continue
            txt = m.group(0).strip()
            if not re.search(r"/", txt):
                return f"{txt}/SF/YR"
            return txt
    return ""


def _detail_spaces(lines: list, rate: str) -> list:
    """Spaces Available 表：套间名 → 面积 → 状态/租赁类型，逐个换算月租。"""
    if "Spaces Available" not in lines:
        return []
    start = lines.index("Spaces Available") + 1
    out, cur = [], None
    for ln in lines[start:]:
        if ln.startswith(("Pricing Information", "Get in touch", "About ", "Other properties")):
            break
        m = _SUITE_LINE.match(ln)
        if m:
            cur = {"name": ln, "size": "", "rate": rate, "monthly": None}
            out.append(cur)
            continue
        m = _AREA_LINE.match(ln)
        if m and cur is not None and not cur["size"]:
            cur["size"] = m.group(1).replace(",", "")
            mp = monthly_price(rate, cur["size"]) if rate else None
            cur["monthly"] = mp
    return [s for s in out if s["size"]]


def _detail_area(lines: list, spaces: list) -> str:
    """优先 Total Space Available，其次各套间面积之和，最后 Min/Max 区间。"""
    for key in ("Total Space Available", "Minimum Space Available"):
        if key in lines:
            j = lines.index(key)
            for ln in lines[j + 1:j + 3]:
                m = _AREA_LINE.match(ln)
                if m:
                    return m.group(1).replace(",", "")
    if spaces:
        try:
            return str(max(int(s["size"]) for s in spaces))
        except ValueError:
            return ""
    return ""


def _detail_agents(lines: list, limit: int = 2) -> str:
    if "Get in touch" not in lines:
        return ""
    start = lines.index("Get in touch") + 1
    names = []
    for ln in lines[start:start + 14]:
        if ln.startswith(("Search Results", "Contact For Details", "Other properties")):
            break
        if _person_like(ln):
            names.append(ln)
        if len(names) >= limit:
            break
    return " / ".join(names)


def _reveal_contacts(pg) -> list:
    """逐个展开经纪人卡片的电话/邮箱。

    实测（US-SMPL-217104）：电话/信封是 <button class="...brokerContactLink">，
    里面只有 svg 图标、没有文字，而且 `vis=false`（offsetWidth=0），
    普通 click 必然 TimeoutError；必须先 dispatchEvent('click') 才会把
    `<a href="tel:...">` / `<a href="mailto:...">` 填上真实值。
    同一时刻页面上只保留「刚刚展开的那一个」经纪人的链接，所以必须点一个读一个。
    """
    out, seen = [], set()
    try:
        brokers = pg.query_selector_all("[class*='brokerCard__broker']")
    except Exception:
        return out
    for el in brokers:
        try:
            cls = el.get_attribute("class") or ""
            if "__brokers" in cls or "__brokerInfo" in cls:
                continue
            lines = [x.strip() for x in (el.inner_text() or "").splitlines() if x.strip()]
            if not lines:
                continue
            name = lines[0]
            # 嵌套节点会把 "Phone  Email" 当成一行姓名一起抓进来，必须排除
            if not _person_like(name):
                continue
            if name in seen:
                continue
            btn = el.query_selector("button[class*='brokerContactLink']")
            if btn is None:
                continue
            btn.dispatch_event("click")
            pg.wait_for_timeout(700)
            links = pg.evaluate(
                "() => ({tel: [...document.querySelectorAll(\"a[href^='tel']\")]"
                ".map(a => a.getAttribute('href')),"
                " mail: [...document.querySelectorAll(\"a[href^='mailto']\")]"
                ".map(a => a.getAttribute('href'))})")
            tel = next((x.split(":", 1)[1].strip() for x in (links.get("tel") or [])
                        if x and x.split(":", 1)[-1].strip()), "")
            mail = next((x.split(":", 1)[1].strip() for x in (links.get("mail") or [])
                         if x and x.split(":", 1)[-1].strip()), "")
            if tel or mail:
                seen.add(name)
                out.append({"name": name, "phone": tel, "email": mail})
        except Exception:
            continue
    return out


def fetch_broker_info(listing_url: str) -> dict:
    """打开 CBRE 详情页，解析套间表/单价/面积/经纪人 + 点击展开电话邮箱。"""
    info = {"price": "", "agent": "", "phone": "", "email": "", "area": "",
            "spaces": [], "photo_urls": [], "address": "", "building": "",
            "contacts": []}
    if not listing_url:
        return info
    pg = browser.new_page_in(_PROFILE)
    try:
        _open(pg, listing_url, _DETAIL_READY, tries=3, wait=8000)
        # 套间表是异步渲染的：没有 "Spaces Available" 就再等等
        body = ""
        for _ in range(6):
            body = pg.evaluate("() => document.body.innerText") or ""
            if "Spaces Available" in body or "Lease Rate" in body or "Lease Price" in body:
                break
            pg.wait_for_timeout(2000)
        try:
            extra = pg.evaluate(_DETAIL_JS) or {}
        except Exception:
            extra = {}
        lines = _detail_lines(body or extra.get("text", ""))
        if not lines:
            return info
        rate = _detail_rate(lines)
        spaces = _detail_spaces(lines, rate)
        info["building"], info["address"] = _detail_address(lines)
        info["price"] = rate
        info["spaces"] = spaces
        info["area"] = _detail_area(lines, spaces)
        info["photo_urls"] = list(extra.get("imgs") or [])
        # 只有单个套间时，用套间面积当面积（比 Total 更贴合实际要租的房）
        if len(spaces) == 1 and spaces[0]["size"]:
            info["area"] = spaces[0]["size"]

        # 面议房源（Contact Broker For Pricing）：pipeline 会直接判不通过，
        # 没必要再逐个点电话/邮箱（每次点击 +0.7s，纯浪费）
        if not rate and not any(s.get("monthly") is not None for s in spaces):
            info["agent"] = _detail_agents(lines)
            return info

        contacts = _reveal_contacts(pg)[:4]
        if contacts:
            info["contacts"] = contacts
            info["agent"] = " / ".join(c["name"] for c in contacts[:3])
            info["phone"] = " / ".join(dict.fromkeys(c["phone"] for c in contacts if c["phone"]))
            info["email"] = " / ".join(dict.fromkeys(c["email"] for c in contacts if c["email"]))
        else:
            info["agent"] = _detail_agents(lines)
    except Exception:
        pass
    finally:
        browser.safe_close(pg)
    return info


def candidates_from_urls(urls: list) -> dict:
    """手动贴 CBRE 详情页链接：解析出地址/单价/套间表/电话/邮箱后交给 pipeline。

    注意：手动链接不经过列表页，所以价格与联系方式全都从详情页取。
    """
    out, problems = [], []
    for u in urls or []:
        u = (u or "").strip()
        if not u:
            continue
        info = fetch_broker_info(u)
        if not info.get("price") and not info.get("spaces"):
            problems.append(u)
            continue
        # 地址随详情页一起解析（手动链接没有卡片文案，地址必须从页面上取）
        addr = info.get("address", "")
        out.append({"address": addr, "price": info.get("price", ""),
                    "agent": info.get("agent", ""), "phone": info.get("phone", ""),
                    "email": info.get("email", ""), "area": info.get("area", ""),
                    "undisclosed": False, "listing_url": u,
                    "spaces": info.get("spaces") or []})
    if not out:
        return {"status": "fail", "screenshot": "",
                "message": f"CBRE 链接未解析出价格/套间（{len(problems)} 条），"
                           f"请确认是 cbre.com/properties/details 详情页链接",
                "candidates": []}
    msg = f"手动链接解析出 {len(out)} 个 CBRE 候选"
    if problems:
        msg += f"（{len(problems)} 条未解析到价格/套间，已跳过）"
    return {"status": "pass", "message": msg, "candidates": out, "screenshot": ""}
