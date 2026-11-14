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
from . import paginate
from .loopnet import monthly_price

_BASE = "https://www.officespace.com"
DETAIL_IS_PRICE_SOURCE = True
# 越界页码不会 404/空页，而是把第 1 页再发一遍（实测 Chicago 第 999 页 = 第 1 页），
# 靠 paginate.check 的页内当前页标记识别。
PAGE_OK_SIGNAL = True
_PROFILE = "chrome_profile_feeds"
# OfficeSpace 只提供官方联系渠道（页面无经纪人直连电话/邮箱），
# 记录里电话/邮箱统一写这句话，避免留空被误判成"漏抓"
OFFICIAL_CONTACT = "请联系该官网工作人员"

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
# 注意结尾不能用 \b：卡片文案是拼接的（"...From $1,000/MOExecutive suites..."），
# \b 在 "MO" 与 "Executive" 之间不成立，会漏掉全部价格（实测踩坑）。
_PRICE_MO_RE = re.compile(r"(?:from|starting at|starts at)?\s*\$\s*[\d,]+(?:\.\d+)?\s*"
                          r"(?:-\s*\$?\s*[\d,]+(?:\.\d+)?\s*)?"
                          r"/\s*(?:SF\s*/\s*)?(?:MONTHLY|MONTH|YEARLY|YEAR|YR|MO)", re.I)
_SIZE_RE = re.compile(r"([\d,]+)\s*(?:-\s*([\d,]+))?\s*(?:sq\.?\s*ft|SF)", re.I)
# 卡片上的面积徽标：「(up to 3,188 SF)」——最可靠，优先于任何散落的 "N SF"
_AREA_UPTO = re.compile(r"up to\s*([\d,]+)\s*(?:sq\.?\s*ft|SF)", re.I)
# 联合办公房源页面根本不写平方英尺，只写「1 Space, 6 - 20 ppl」。
# 既然月租已经直接给了，就不需要面积去乘单价——面积列照实写人数。
# 结尾不能加 \b：拼接文案里是 "6 - 20 pplContact us" / "1 pplFrom $1,000"，ppl 后面直接跟字母
_PPL_RE = re.compile(r"(\d{1,3})\s*(?:-\s*(\d{1,3}))?\s*ppl", re.I)


def _ppl_of(text: str) -> int:
    """卡片/详情页上的可容纳人数（取区间上限）。"""
    m = _PPL_RE.search(text or "")
    if not m:
        return 0
    try:
        ppl = int(m.group(2) or m.group(1) or 0)
    except ValueError:
        return 0
    return ppl if 0 < ppl <= 200 else 0


def _ppl_label(ppl: int) -> str:
    """面积列的写法：「20 PPL」。"""
    return f"{ppl} PPL" if ppl else ""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")


def _list_url(city: str, state: str, page: int) -> str:
    url = f"{_BASE}/{_slug(state)}/{_slug(city)}"
    return f"{url}?page={page}" if page > 1 else url


_STREET_SUFFIX = (r"Street|St|Avenue|Ave|Drive|Dr|Road|Rd|Boulevard|Blvd|Parkway|Pkwy|"
                  r"Highway|Hwy|Lane|Ln|Place|Pl|Court|Ct|Circle|Cir|Pike|Trail|Trl|Way")
# 地址在拼接文案里的两种形态：「209, 10th Avenue South」（门牌后带逗号，最可信）
# 和「501 Union Street」（无逗号）。带逗号的优先，否则会把楼名一起吞进来
# （实测 "560 Flex by Cummins Station209, 10th Avenue"）。
_ADDR_COMMA_RE = re.compile(
    rf"(\d{{1,6}},\s+[A-Za-z0-9][A-Za-z0-9 .,'\-]{{1,45}}?\s(?:{_STREET_SUFFIX})(?![A-Za-z]))",
    re.I)
_ADDR_RE = re.compile(
    rf"(\d{{1,6}},?\s+[A-Za-z0-9][A-Za-z0-9 .,'\-]{{1,45}}?\s(?:{_STREET_SUFFIX})(?![A-Za-z]))",
    re.I)

_STREET_RX = re.compile(r"\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|pkwy|"
                        r"ln|lane|hwy|highway|pl|place|ct|court|cir|circle|pike|trail|tr)\b", re.I)


def _addr_from_url(url: str) -> str:
    """卡片文案常被楼名/价格挤成一团，URL slug 反而是最干净的地址来源：
    /ky/louisville/2339802-10200-linn-station-road-2nd-floor → 10200 Linn Station Road 2nd Floor"""
    m = re.search(r"/[a-z]{2}/[a-z0-9\-]+/\d+-([^/?#]+)", url or "")
    if not m:
        return ""
    words = m.group(1).replace("-", " ").split()
    return " ".join(w if w.isdigit() else w.capitalize() for w in words).strip()


# 「3rd Floor / Floor 2 / 2nd Fl」不是通信地址的一部分：留在街址里会让 Smarty
# 把它当街道后缀解析（实测报「街道后缀发生变化（Floor → St）」，把 500 Church
# Street 这种真实写字楼误判为无效地址）。
_FLOOR_RE = re.compile(
    r",?\s*(?:\d{1,2}(?:st|nd|rd|th)\s+(?:floor|fl)\.?|floor\s+\d{1,2})\b", re.I)


def _strip_floor(addr: str) -> str:
    return re.sub(r"\s{2,}", " ", _FLOOR_RE.sub("", addr or "")).strip(" ,")


def _parse_card(text: str, url: str = "", city: str = "", state: str = "") -> dict:
    """解析列表卡片。

    卡片文案是**拼接**出来的（无分隔符），例如：
        "Strive Nashville501, Union Street Suite 4003 Spaces, 1 - 6 ppl|
         (up to 3,188 SF)Coworking from $180/MO..."
    所以地址优先取 URL slug（/…/2236088-4235-hillsboro-pike-suite-300 →
    "4235 Hillsboro Pike Suite 300"，最干净且带 Suite），slug 只有门牌号时
    （/…/2387843-501 → "501"）再退回正文正则。
    城市/州在卡片上根本不显示，必须由调用方补上（否则 Smarty 必然查不到）。
    """
    text = text or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    addr = price = area = ""
    slug_addr = _addr_from_url(url)
    if slug_addr and _STREET_RX.search(slug_addr) and re.match(r"^\d", slug_addr):
        addr = slug_addr
    if not addr:
        m_addr = _ADDR_COMMA_RE.search(text) or _ADDR_RE.search(text)
        if m_addr:
            addr = re.sub(r"\s+", " ", m_addr.group(1)).strip(" ,")
            addr = _ADDR_FIX.sub(r"\1 ", addr)
    for ln in lines:
        # 注意：面积和价格常常在**同一行**（"3 Spaces, 1-6 ppl|(up to 3,188 SF)Coworking
        # from $180/MO"）。以前取到价格就 continue，导致这一行的面积永远读不到 ——
        # 这就是 OfficeSpace 记录面积一直是空的原因。
        if not area:
            m_area = _AREA_UPTO.search(ln)
            if not m_area and re.search(r"\b(?:space|spaces|ppl|person|people|desk)\b", ln, re.I):
                m_area = _SIZE_RE.search(ln)
            if m_area:
                area = (m_area.group(1) or "").replace(",", "")
        if not price:
            # 只认「$1,000/MO」这种带周期的报价。页面顶部的价格筛选器
            # （"$129 | $180 | $400 | ..."）会被误当成房源价格，必须先排除
            if ln.count("$") <= 1 and ln.count("|") <= 1:
                m = _PRICE_MO_RE.search(ln)
                if m:
                    price = m.group(0).strip()
    addr = _strip_floor(addr)
    ppl = 0 if area else _ppl_of(text)
    if ppl:
        area = _ppl_label(ppl)      # 没给平方英尺就写人数，不换算
    if addr and state and not re.search(rf",\s*{re.escape(state)}\b", addr, re.I):
        addr = f"{addr}, {city}, {state}" if city else f"{addr}, {state}"
    undisclosed = not price
    return {"address": addr, "price": price, "area": area,
            "area_ppl": ppl, "undisclosed": undisclosed}


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
        # 页码越界时站点把第 1 页原样再发一遍 → 只能用页内「当前页」标记识别
        info = paginate.check(pg, page_no)
        if page_no > 1 and info["page_ok"] is False:
            r = paginate.out_of_range(page_no, info["actual"])
            r["message"] = (f"OfficeSpace 第 {page_no} 页超出末页"
                            f"（站点回落到第 {info['actual']} 页）")
            r["screenshot"] = shot
            r["total_pages"] = pages
            return r
        if not cards and any(k in low for k in _BLOCK_KEYWORDS):
            return {"status": "fail", "message": "OfficeSpace 被拦截（验证码/限流）",
                    "candidates": [], "screenshot": shot, "total_pages": 0}
        if not cards:
            nf = "page not found" in low or "404" in low
            return {"status": "warn",
                    "message": f"OfficeSpace 第 {page_no} 页无候选（{city}, {state}）",
                    "candidates": [], "screenshot": shot, "total_pages": pages,
                    "not_found": nf or page_no > 1, "page_ok": True}
    except Exception as e:
        return {"status": "fail", "message": f"OfficeSpace 访问失败: {str(e)[:80]}",
                "candidates": [], "screenshot": shot, "total_pages": 0}
    finally:
        browser.safe_close(pg)

    candidates, seen = [], set()
    for c in cards:
        p = _parse_card(c.get("text", ""), c.get("url", ""), city, state)
        addr = p["address"]
        if not addr or not re.search(r"\d", addr):
            continue
        key = re.sub(r"[^a-z0-9]+", "", addr.lower())
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "address": addr, "price": p["price"], "agent": "",
            "phone": OFFICIAL_CONTACT, "email": OFFICIAL_CONTACT,
            "area": p["area"], "area_ppl": p.get("area_ppl", 0),
            "undisclosed": p["undisclosed"],
            "listing_url": c.get("url", ""), "spaces": [],
        })
        if len(candidates) >= limit:
            break

    msg = (f"OfficeSpace 第 {page_no}/{pages or '?'} 页采集到 {len(candidates)} 个候选"
           if candidates else f"OfficeSpace 第 {page_no} 页未解析到候选")
    return {"status": "pass" if candidates else "warn", "message": msg,
            "candidates": candidates, "screenshot": shot, "total_pages": pages,
            "page_ok": True}


# 详情页：只取 <main>，并在 Nearby Listings 处截断。
# 为什么不能全文扫 $：详情页底部 Nearby Listings 会列出 3~5 个无关房源
# （实测 2352980-810 抓到 $55-$500/MO、$89-$235/MO、$300-$675/MO 全是别人的），
# 全文正则会把这些价格当成目标房源，直接算错预算判定。
_DETAIL_JS = r"""
() => {
    const m = document.querySelector("main");
    let t = m ? (m.innerText || "") : (document.body.innerText || "");
    const cut = t.search(/Nearby Listings|Similar Listings|Nearest Districts|People also viewed/i);
    if (cut > 200) t = t.slice(0, cut);
    const imgs = [...document.querySelectorAll("main img")]
        .map(i => i.currentSrc || i.src || "")
        .filter(s => s.startsWith("http"));
    return {text: t, imgs: imgs.slice(0, 10),
            phone: ((document.querySelector("a[href^='tel:']") || {}).href || "").replace("tel:", "")};
}
"""

# "810, Dominican Drive, Nashville, TN 37228" → "810 Dominican Drive, Nashville, TN 37228"
_ADDR_FIX = re.compile(r"^(\d{1,6})\s*,\s*")
# 站点把面积写成 "11 SF"（1 人套间被算成 11 平方英尺）——低于此值视为脏数据
_MIN_SANE_AREA = 50


def _label_value(lines: list, label: str, pattern: str) -> str:
    """取「Size / 11 SF」「Price / $1,000 /MO」这类「标签在上一行」的字段。"""
    for i, ln in enumerate(lines):
        if ln.strip().lower() != label.lower():
            continue
        for nxt in lines[i + 1:i + 4]:
            m = re.search(pattern, nxt, re.I)
            if m:
                return m.group(1).strip()
    return ""


def fetch_broker_info(listing_url: str) -> dict:
    info = {"price": "", "agent": "", "phone": "", "email": "", "area": "",
            "area_ppl": 0, "spaces": [], "photo_urls": [], "address": "",
            "area_raw": ""}
    if not listing_url:
        return info
    pg = browser.new_page_in(_PROFILE)
    try:
        browser.goto_retry(pg, listing_url, timeout=60000, wait=2000)
        data, text = {}, ""
        for _ in range(6):
            data = pg.evaluate(_DETAIL_JS) or {}
            text = data.get("text") or ""
            if "$" in text and re.search(r"\bsize\b", text, re.I):
                break
            pg.wait_for_timeout(1500)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return info

        # 地址：详情页首部的「门牌, 街名, 城市, 州 邮编」
        for ln in lines[:12]:
            if re.fullmatch(r"\d{1,6}.*,\s*[A-Z]{2}\s+\d{5}", ln):
                info["address"] = _strip_floor(_ADDR_FIX.sub(r"\1 ", ln).strip())
                break

        info["price"] = _label_value(
            lines, "Price",
            r"(\$[\d,.]+\s*(?:-\s*\$?[\d,.]+\s*)?/\s*(?:SF\s*/\s*)?(?:MO|YR|month|year))",
        ) or (_PRICE_MO_RE.search(text).group(0).strip() if _PRICE_MO_RE.search(text) else "")

        raw_area = _label_value(lines, "Size", r"([\d,]+)\s*(?:sq\.?\s*ft|SF)") \
            or _label_value(lines, "Available Space", r"([\d,]+)\s*(?:sq\.?\s*ft|SF)")
        if not raw_area:
            # 很多 OfficeSpace 详情页根本不渲染 Size 字段，只在套间块里写
            # 「N Space, X PPL」或「(up to N SF)」；这里补两种兜底
            m_up = _AREA_UPTO.search(text)
            if m_up:
                raw_area = m_up.group(1).replace(",", "")
            else:
                for ln in lines:
                    if re.search(r"\b(?:space|spaces|sq\.?\s*ft|SF)\b", ln, re.I):
                        m_sz = _SIZE_RE.search(ln)
                        if m_sz and not re.search(r"\$", ln):
                            raw_area = (m_sz.group(1) or "").replace(",", "")
                            break
        info["area_raw"] = raw_area
        if raw_area and raw_area.isdigit() and int(raw_area) >= _MIN_SANE_AREA:
            info["area"] = raw_area
        if not info["area"]:
            # 页面只写人数（"1 Space, 6 - 20 ppl"）：月租已经给了，不需要面积乘单价，
            # 面积列直接照实写人数（"20 PPL"）
            ppl = _ppl_of(text)
            if ppl:
                info["area"] = _ppl_label(ppl)
                info["area_ppl"] = ppl
        # 面积脏数据（<50 SF）不写进面积字段，避免"1,000 SF 的房按 11 SF 归档"

        # 经纪人 / 联系专家：Contact an expert → 姓名 → 公司
        for i, ln in enumerate(lines):
            if re.match(r"^contact an? (?:expert|broker|agent)$", ln, re.I):
                if i + 2 < len(lines):
                    info["agent"] = f"{lines[i + 1]}（{lines[i + 2]}）"
                break

        # OfficeSpace 不给经纪人直连方式（页面只有平台联系表单/官方热线），
        # 所以电话/邮箱统一写提示语，不写空也不写平台客服号（那是无效线索）
        info["phone"] = OFFICIAL_CONTACT
        info["email"] = OFFICIAL_CONTACT
        info["photo_urls"] = [u for u in (data.get("imgs") or [])
                              if "logo" not in u.lower() and "icon" not in u.lower()]

        # 单价是 $/SF 口径时才需要面积换算；$/MO 直接作为该套间月租。
        # 面积列写的是人数（"20 PPL"）时不能当面积用，套间 size 只放真实平方英尺。
        if info["price"]:
            mp = monthly_price(info["price"], info["area"])
            if mp:
                info["spaces"] = [{"name": "整层/整栋",
                                   "size": info["area"] if str(info["area"]).isdigit() else "",
                                   "rate": info["price"], "monthly": mp}]
    except Exception:
        pass
    finally:
        browser.safe_close(pg)
    return info


def candidates_from_urls(urls: list) -> dict:
    out, problems = [], []
    for u in urls or []:
        u = (u or "").strip()
        if not u:
            continue
        info = fetch_broker_info(u)
        if not info.get("address") or not (info.get("price") or info.get("spaces")):
            problems.append(u)
            continue
        out.append({"address": info["address"], "price": info.get("price", ""),
                    "agent": info.get("agent", ""), "phone": info.get("phone", ""),
                    "email": info.get("email", ""), "area": info.get("area", ""),
                    "area_ppl": info.get("area_ppl", 0),
                    "undisclosed": not info.get("price"),
                    "listing_url": u, "spaces": info.get("spaces") or []})
    if not out:
        return {"status": "fail", "screenshot": "",
                "message": f"OfficeSpace 链接未解析出地址/价格（{len(problems)} 条），"
                           f"请确认是 officespace.com/<州>/<城市>/<id> 详情页链接",
                "candidates": []}
    msg = f"手动链接解析出 {len(out)} 个 OfficeSpace 候选"
    if problems:
        msg += f"（{len(problems)} 条未解析出地址或价格，已跳过）"
    return {"status": "pass", "message": msg, "candidates": out, "screenshot": ""}
