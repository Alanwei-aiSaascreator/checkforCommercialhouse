"""步骤1：LoopNet 商业地产房源采集（搜索页 + 详情页）。

返回候选结构（pipeline 约定）：
  candidates: [{address, price, agent, phone, email, area, listing_url}]
价格/经纪人/面积在卡片缺失时由 pipeline 调 fetch_broker_info 去详情页补抓。
"""
import json
import random
import re
import threading
import time

from .. import browser, config
from . import paginate

_BASE = "https://www.loopnet.com"
# 分页条明写 "Page 4 of 19"（真值，不需要二分探测），只在越界时用它兜第二道底：
# LoopNet 越界通常返回 404 页，但偶发也会把第 1 页再发一遍。
PAGE_OK_SIGNAL = True

_LIKING_JS = """
() => {
    const out = [];
    const seen = new Set();
    // 列表卡片 → 详情页链接
    for (const a of document.querySelectorAll("a[href*='/Listing/']")) {
        const href = a.href.split("?")[0];
        if (!/^https:\\/\\/www\\.loopnet\\.com\\/Listing\\/.+\\/\\d+\\/$/.test(href)) continue;
        if (seen.has(href)) continue;
        seen.add(href);
        // 沿 DOM 向上找卡片容器，取其中的地址与价格文本
        let card = a.closest("div[class*='listing'], article, li") || a.parentElement;
        let text = card ? card.innerText : "";
        out.push({url: href, text: (text || "").slice(0, 600)});
        if (out.length >= 40) break;
    }
    return out;
}
"""


def _slug(city: str, state: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", f"{city} {state}").strip("-").lower()
    return s


_PRICE_TOKEN = re.compile(
    r"\$\s*[\d,]+(?:\.\d+)?"
    r"(?:\s*-\s*\$?\s*[\d,]+(?:\.\d+)?)?"
    r"(?:\s*(?:/\s*(?:month|mo|year|yr)|/?\s*sf\s*/\s*(?:year|yr|month|mo)"
    r"|per\s+month|per\s+year))?",
    re.I)


# "Rental Rate: $6.84 SF/YR" / "Lease Rate - $21.00" 这类带标签的字段最可信
_PRICE_LABEL_RE = re.compile(
    r"(?:rental\s+rate|lease\s+rate|asking\s+rate|\brate\b|\brent\b)\s*[:\-]\s*([^\n]{0,40})",
    re.I)
# 金额后面跟着这些词，说明不是租金而是描述文本/体量数字
_PRICE_BAD_TAIL = re.compile(
    r"^\s*(?:\.\d+\s*)?(?:billion|million|bill\b|thousand|per\s+hour|hour|considera|"
    r"redevelop|[MBKmbk]\b)", re.I)


def _pick_price(text: str) -> str:
    """从一段文本里挑第一个「像租金」的金额，跳过描述性数字。"""
    for m in _PRICE_TOKEN.finditer(text or ""):
        tail = (text or "")[m.end():m.end() + 20]
        if _PRICE_BAD_TAIL.match(tail):
            continue
        return re.sub(r"\s+", " ", m.group(0)).strip()
    return ""


def _parse_price(text: str) -> str:
    """抓价格文案：优先读 "Rental Rate / Lease Rate / Rate / Rent" 后的金额，
    否则取第一个非描述性的「金额 + 单位」。

    排除的坑（都是实测踩到的）：
      - "$1.5 billion in considera..."、"$30M redevelopment"（描述里的体量数字）
      - "$85 per hour."（不是租金）
      - "$91,572\\t\\nWarehouse Employees"（旧正则 \\s* 跨行把标题吞进来）
    """
    text = text or ""
    for m in _PRICE_LABEL_RE.finditer(text):
        token = _pick_price(m.group(1))
        if token:
            return token
    return _pick_price(text)


def monthly_price(price_text: str, area: str = "") -> float | None:
    """把 LoopNet 价格文案换算成「每月总租金」，用于价格区间硬约束。

    LoopNet 出租房源价格有几种口径：
      - $X /SF/YR（每平方英尺每年）→ X × 面积 ÷ 12
      - $X /YR（每年总价）→ X ÷ 12
      - $X /MO 或无单位 → 原值
    返回 None 表示无法判断（不参与区间过滤）。"""
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", price_text or "")
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    low = (price_text or "").lower()
    # 面积只接受「数字 / 数字 + SF 等单位」；OfficeSpace 会把人数写进面积列
    # （"20 PPL"），强行抽数字会变成 20 SF 去乘单价，必须拒绝
    area_n = 0.0
    m_area = re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|sf|square\s*feet)?\s*",
                          str(area or ""), re.I)
    if m_area:
        try:
            area_n = float(m_area.group(1).replace(",", ""))
        except ValueError:
            area_n = 0.0
    # 每平方英尺口径：必须结合面积才能得到「该房源总月租」
    #   总月租 = 每英尺价格 × 面积 ÷ 12（$/SF/YR）
    #   总月租 = 每英尺价格 × 面积      （$/SF/MO）
    # 注意 sqft / sq ft / square feet 都要算进来：只写 "sf/mo" 会漏掉
    # "$4.50/sqft/mo"（CBRE 卡片的默认口径），落到下面的 "$/SF 按年" 分支，
    # 把月租算成 1/12，$6,165/月的房源会被判成 $514/月而误放行（实测踩坑）。
    _sq = r"(?:sq\.?\s*ft|sqft|square\s*(?:feet|foot)|sf)"
    if re.search(rf"{_sq}\s*/\s*(?:mo|month|monthly)(?![a-z])", low):
        return val * area_n if area_n else None
    if re.search(rf"{_sq}\s*/\s*(?:yr|year|annual)", low):
        return val * area_n / 12 if area_n else None
    # "$6 per rentable square" / "$30 per square foot" 同样是每英尺单价（LoopNet 惯例按年）
    if re.search(r"(?:per|/)\s*(?:rentable\s+)?(?:square(?:\s*(?:foot|feet))?|sq\.?\s*ft|sf)\b", low):
        return val * area_n / 12 if area_n else None
    if re.search(r"/\s*sf\b", low):          # 只写 $/SF（LoopNet 惯例是每年）
        return val * area_n / 12 if area_n else None
    if "/yr" in low or "per year" in low or "annual" in low:
        return val / 12
    if "/mo" in low or "per month" in low or "monthly" in low:
        return val
    if val < 100:
        # "$6" 这种没单位的小数字，几乎都是被截断的 $/SF/YR 单价，
        # 当成"月租 $6"会把超预算房源放进来 → 判为无法判断，转人工
        return None
    return val


def _parse_address(text: str, city: str, state: str) -> str:
    # 地址行：数字开头的行，且最好包含方位后缀（St/Ave/Rd/Blvd/Dr/Wy/Hwy/Pkwy/Ln/Ct/Way）
    for line in text.splitlines():
        line = line.strip()
        if re.match(r"^\d{1,6}\s+\w", line) and re.search(
                r"\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|wy|way|"
                r"hwy|highway|pkwy|parkway|ln|lane|ct|court|cir|circle|pl|place|ter)\b",
                line, re.I):
            if state.lower() in line.lower() or city.lower() in line.lower() or "," in line:
                return line
    return ""


_AREA_UNIT = (r"(?:sq\.?\s*ft|sqft|sf|sq\s*ft|square\s*(?:feet|foot)|"
              r"rentable\s+square\s*(?:feet|foot))")
_AREA_RANGE_RE = re.compile(
    rf"(?<![.\d$])(\d[\d,]*)\s*-\s*\$?\s*(\d[\d,]*)\s*{_AREA_UNIT}\b", re.I)
# "14K SF" / "2.5k sq ft"
_AREA_K_RE = re.compile(rf"(?<![.\d$])(\d+(?:\.\d+)?)\s*[kK]\s*{_AREA_UNIT}\b", re.I)
# "0.5 acre" / "2.3 acres"（1 acre ≈ 43,560 SF）
_ACRE_RE = re.compile(r"(?<![.\d$])(\d+(?:\.\d+)?)\s*(?:acres?|ac)\b", re.I)
_SQFT_PER_ACRE = 43560
_AREA_RE = re.compile(rf"(?<![.\d$])(\d[\d,]*)\s*{_AREA_UNIT}\b", re.I)
# 紧跟在数字后的 "/YR"、"SF/YR"、"/MO" 是单价单位，不是面积
_PRICE_SUFFIX_RE = re.compile(r"\s*/\s*(?:sf\s*/?\s*)?(?:yr|year|mo|month)\b", re.I)
# 带标签的面积字段最可信："Available Space: 14,000 SF" / "Building Size - 2.5 acres"
_AREA_LABEL_RE = re.compile(
    r"(?:available\s+space|available\s+sf|total\s+space|building\s+size|"
    r"rentable\s+(?:area|space)|\bsize\b)\s*[:\-]?\s*([^\n]{0,30})", re.I)


def _parse_area(text: str) -> str:
    r"""面积（SF）：从网站文案里抓 "1,234 SF" / "678 SqFt" / "1200 sq ft"。

    重要：这是**网页文本解析**，不是 AI 估算。单价 "$42 SF/YR" 里的 42 必须排除，
    否则 42 会被当成 42 SF 面积，进而把月租算成 42×42/12≈$147，让超预算房源混进来。
    区间 "3,000 - 5,000 SF" 取上限（宁可按大面积算租金，避免低估）。"""
    # 带标签的字段最可信："Available Space: 14,000 SF" / "Building Size - 2.5 acres"
    for lab in _AREA_LABEL_RE.finditer(text or ""):
        val = _parse_area_plain(lab.group(1))
        if val:
            return val
    return _parse_area_plain(text)


def _parse_area_plain(text: str) -> str:
    """不带标签的面积解析（被 _parse_area 复用）。"""
    m = _AREA_RANGE_RE.search(text or "")
    if m:
        a, b = m.group(1).replace(",", ""), m.group(2).replace(",", "")
        try:
            return str(max(int(a), int(b)))
        except ValueError:
            pass
    # "14K SF" → 14000
    m = _AREA_K_RE.search(text or "")
    if m:
        try:
            return str(int(round(float(m.group(1)) * 1000)))
        except ValueError:
            pass
    for m in _AREA_RE.finditer(text or ""):
        head = (text or "")[max(0, m.start() - 2):m.start()]
        tail = (text or "")[m.end():m.end() + 14]
        if head.rstrip().endswith("$"):        # "$42 SF"
            continue
        if _PRICE_SUFFIX_RE.match(tail):       # "42 SF/YR"、"42 SF/MO"
            continue
        return m.group(1).replace(",", "").rstrip(".")
    # "0.5 acre" → 21780 SF（只在地块面积语境里出现，放最后兜底）
    m = _ACRE_RE.search(text or "")
    if m:
        try:
            return str(int(round(float(m.group(1)) * _SQFT_PER_ACRE)))
        except ValueError:
            pass
    return ""


def _parse_phone(text: str) -> str:
    m = re.search(r"(\(\d{3}\)\s?\d{3}[- ]\d{4}|\d{3}-\d{3}-\d{4})", text)
    return m.group(1) if m else ""


def _parse_broker(text: str) -> str:
    m = re.search(r"(?:Broker|Brokered by|Listed by|Contact)\s*[:\-]?\s*([A-Z][A-Za-z .'-]{3,40})", text)
    return m.group(1).strip() if m else ""


def _detail_candidate(page, url: str, city: str = "", state: str = "") -> dict:
    """从已打开的详情页提取候选（地址/价格/经纪人/电话）。"""
    try:
        text = page.evaluate("() => document.body.innerText.slice(0, 6000)")
    except Exception:
        text = ""
    title = ""
    try:
        t = page.locator("h1").first
        if t.count():
            title = t.inner_text(timeout=3000).strip()
    except Exception:
        pass
    og = ""
    try:
        og = page.evaluate(
            "() => { const m = document.querySelector(\"meta[property='og:title']\");"
            " return m ? m.content : ''; }")
    except Exception:
        pass
    addr = _parse_address(text, city, state) or title or og
    addr = re.sub(r"\s+", " ", addr).strip(" |")
    # 同理：真地址一定含门牌号，标题/og 兜底常拿到站点名，不含数字就不采用
    if not re.search(r"\d", addr):
        addr = ""
    price = _parse_price(text)
    return {
        "address": addr,
        "price": price,
        "agent": _parse_broker(text),
        "phone": _parse_phone(text),
        "email": "",
        "area": _parse_area(text),
        # 页面明确写 "Undisclosed"/"Contact for pricing" → 没有数字价格可抓
        "undisclosed": (not price) and ("undisclosed" in (text or "").lower()
                                        or "contact for pricing" in (text or "").lower()),
        "listing_url": url,
    }


# 主配置带 Crexi 登录态、已被 Akamai 记住：实测同一出口 IP 下主配置直接
# Access Denied，干净配置正常 200。LoopNet 全程走独立配置目录。
_PROFILE = "chrome_profile_feeds"

_BLOCK_KEYWORDS = ("are you a robot", "access denied", "unusual traffic", "captcha",
                   "too many requests")
# 页码越界时 LoopNet 的 404 文案
_NOT_FOUND_KEYWORDS = ("we can't find what you're looking for", "might have been moved",
                       "page not found")

# ---- 请求节流（Akamai 速率阈值）----
# 实测现象：前几个列表页/详情页都正常，跑到第 7~10 个请求起稳定 Access Denied。
# 说明触发的不是 IP 段封禁而是**速率/节奏**判定。只在"被拒后重试"里加等待没用，
# 必须在每次请求之前排队，把节奏压到阈值以下。
_MIN_GAP = 12.0       # 相邻两次请求的最小间隔（秒）
_JITTER = 4.0         # 随机抖动，避免固定间隔本身成为机器特征
_BURST = 3            # 连续请求达到该次数后插入一次长冷却
_COOL = 90.0          # 长冷却时长（秒）
# 当日请求/被拦次数落盘：Akamai 的额度按「出口 IP + 天」累计，跨任务、跨重启都算，
# 只放内存的话重启一次就归零，等于反复把已经烧掉的配额再撞一遍。
_STATE = config.DATA_DIR / "loopnet_state.json"
_throttle_lock = threading.Lock()
_last_req = {"t": 0.0, "n": 0}


def _load_state() -> dict:
    today = time.strftime("%Y-%m-%d")
    try:
        d = json.loads(_STATE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("date") == today:
            return d
    except Exception:
        pass
    return {"date": today, "reqs": 0, "blocks": 0}


def _save_state(d: dict) -> None:
    try:
        _STATE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _throttle() -> None:
    """请求前排队：最小间隔 + 随机抖动 + 每 N 次长冷却。

    当天被拦次数越多，节奏放得越慢（Akamai 的惩罚有累积性：同一 IP 反复触发
    后，阈值会越来越低）。time.sleep 放在锁内，多线程并发也能串行化。
    """
    with _throttle_lock:
        st = _load_state()
        factor = 1 + min(int(st.get("blocks", 0)), 3) * 0.6      # 最多放慢到 2.8 倍
        gap = _MIN_GAP * factor + random.uniform(0, _JITTER * factor)
        wait = _last_req["t"] + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_req["n"] += 1
        if _last_req["n"] % _BURST == 0:
            time.sleep((_COOL + random.uniform(0, 15)) * factor)
        _last_req["t"] = time.time()
        st["reqs"] = int(st.get("reqs", 0)) + 1
        _save_state(st)


def _cool_down(sec: float = 180.0) -> None:
    """命中拦截后的冷却。Akamai 的惩罚有衰减期，立刻重试只会继续被拒，
    还会把惩罚窗口拉长；先停够再回去，成功率明显更高。同时记一次 block，
    让后续请求自动进一步降速。"""
    with _throttle_lock:
        _last_req["n"] = 0
        st = _load_state()
        st["blocks"] = int(st.get("blocks", 0)) + 1
        _save_state(st)
        time.sleep(sec + random.uniform(0, 10))
        _last_req["t"] = time.time()


def _blocked(page) -> bool:
    try:
        body = page.evaluate("() => document.body.innerText").lower()
    except Exception:
        return False
    return any(k in body for k in _BLOCK_KEYWORDS)


def _warmup(page) -> None:
    """首页预热：先落主页再进深层 URL，模拟真人浏览路径，
    降低 Akamai 对"直开深层链接"的 bot 评分拦截概率。"""
    try:
        browser.goto_retry(page, _BASE + "/", timeout=45000, wait=4000, retries=1)
        # 轻微滚动，模拟人类停留
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(1500)
    except Exception:
        pass


def _search_url(city: str, state: str, price_min: int, price_max: int, page_no: int) -> str:
    """LoopNet 页码走路径段：/for-lease/2/?pr=0-2000。
    注意 &pg=2 这种 query 参数会被静默忽略（永远返回第 1 页，实测确认）。"""
    base = f"{_BASE}/search/commercial-real-estate/{_slug(city, state)}/for-lease/"
    if page_no > 1:
        base += f"{page_no}/"
    return f"{base}?pr={price_min}-{price_max}"


def _parse_total_pages(page) -> int:
    """从分页条读总页数（"Showing Results 1 - 40, Page 1 of 19"），
    兜底取 data-page 最大值；无分页条则视为只有 1 页。"""
    try:
        txt = page.evaluate(
            "() => { const e = document.querySelector('.pagination__range');"
            " return e ? e.innerText : ''; }") or ""
        m = re.search(r"of\s+([\d,]+)", txt, re.I)
        if m:
            return max(int(m.group(1).replace(",", "")), 1)
    except Exception:
        pass
    try:
        vals = page.eval_on_selector_all(
            "[data-page]", "els => els.map(e => parseInt(e.dataset.page, 10))"
                           ".filter(n => n > 0)")
        if vals:
            return max(vals)
    except Exception:
        pass
    return 1


def search_candidates(city: str, state: str, price_min: int, price_max: int,
                      limit: int = 6, tag: str = "search", page: int = 1) -> dict:
    """按城市/州/价格区间搜索 LoopNet 出租房源（page 为结果页页码）。

    返回 total_pages：LoopNet 该搜索条件下的真实总页数，供流水线随机抽页。
    反爬策略：首页预热 → 搜索页；命中 Access Denied 时预热后重试一次。
    注意：LoopNet（Akamai）会拦截非美国 IP——需开启系统代理（美国节点，
    如 Clash Verge 凤凰城）。
    """
    page_no = max(int(page or 1), 1)
    url = _search_url(city, state, price_min, price_max, page_no)
    # 必须用独立配置：主配置 chrome_profile 带 Crexi 登录态，已被 Akamai 记住，
    # 实测同一代理下主配置 Access Denied / 干净配置正常 200（见 browser.new_page_in）
    page = browser.new_page_in(_PROFILE)
    blocked = False
    total_pages = 0
    try:
        _throttle()
        _warmup(page)
        _throttle()
        browser.goto_retry(page, url, timeout=45000, wait=6000)
        search_shot = browser.shot(page, f"{tag}_loopnet")
        body = page.evaluate("() => document.body.innerText").lower()
        blocked = any(k in body for k in _BLOCK_KEYWORDS)
        if blocked:
            # 速率触发（不是 IP 段封禁）：先冷够再重试，立刻重试只会继续被拒
            _cool_down(180)
            _throttle()
            _warmup(page)
            _throttle()
            browser.goto_retry(page, url, timeout=45000, wait=8000, retries=1)
            search_shot = browser.shot(page, f"{tag}_loopnet_retry") or search_shot
            body = page.evaluate("() => document.body.innerText").lower()
            blocked = any(k in body for k in _BLOCK_KEYWORDS)
        if blocked:
            ip = browser.exit_ip()
            st = _load_state()
            return {"status": "fail",
                    "message": "LoopNet 拦截（Access Denied/Akamai）：冷却 180s 后重试仍被拒"
                               f"（今日已请求 {st.get('reqs', 0)} 次、被拦 {st.get('blocks', 0)} 次）。"
                               "这是按出口 IP 累计的速率/额度限制，不是代码问题，"
                               "继续跑只会把惩罚窗口拉长"
                               + (f"；当前出口 IP {ip}" if ip else "")
                               + "。建议今天先别跑 LoopNet（换 Crexi / CommercialSearch 出量），"
                                 "隔 30 分钟以上或换美国节点再试"
                                 "（状态落在 data/loopnet_state.json，可手动删除以重置计数）",
                    "candidates": [], "screenshot": search_shot or "", "total_pages": 0}
        # 第 1 页 404 先重试一次：Akamai 抖一下也会返回 404，
        # 只有连试两次都 404 才能判定"这个搜索地区不存在"
        if page_no == 1 and any(k in body for k in _NOT_FOUND_KEYWORDS):
            page.wait_for_timeout(2000)
            _throttle()
            _warmup(page)
            _throttle()
            browser.goto_retry(page, url, timeout=45000, wait=6000, retries=1)
            search_shot = browser.shot(page, f"{tag}_loopnet_retry") or search_shot
            body = page.evaluate("() => document.body.innerText").lower()
        # 页码超出真实总页数时 LoopNet 返回 404 页（"we can't find what you're
        # looking for"）：明确告知上层，避免把 404 当成"本页没有候选"继续抽。
        if any(k in body for k in _NOT_FOUND_KEYWORDS):
            # 第 1 页就 404 = 这个搜索地区本身不存在（LoopNet 只支持城市级，
            # 实测 "ALASKA, AK" / "alaska" 都是 404，"anchorage-ak" 才有结果）
            if page_no == 1:
                return {"status": "fail",
                        "message": f"LoopNet 找不到 {city}, {state} 的搜索结果：只支持城市级搜索，"
                                   f"请填具体城市（如 Anchorage），不要填州名/地区名",
                        "candidates": [], "screenshot": search_shot or "",
                        "total_pages": 0, "not_found": True}
            return {"status": "warn",
                    "message": f"LoopNet 第 {page_no} 页不存在（已超出真实总页数）",
                    "candidates": [], "screenshot": search_shot or "",
                    "total_pages": max(page_no - 1, 0), "not_found": True}
        cards = page.evaluate(_LIKING_JS) or []
        total_pages = _parse_total_pages(page) if cards else 0
        info = paginate.check(page, page_no)
        if page_no > 1 and info["page_ok"] is False:
            return {"status": "warn", "not_found": True, "page_ok": False,
                    "message": f"LoopNet 第 {page_no} 页超出末页"
                               f"（站点回落到第 {info['actual']} 页）",
                    "candidates": [], "screenshot": search_shot or "",
                    "total_pages": max(page_no - 1, 0)}
    except Exception as e:
        return {"status": "fail", "message": f"LoopNet 访问失败: {e}", "candidates": [],
                "screenshot": "", "total_pages": 0}
    finally:
        browser.safe_close(page)

    candidates, seen = [], set()
    for c in cards:
        addr = _parse_address(c.get("text", ""), city, state)
        if not addr:
            continue
        key = store_norm(addr)
        if key in seen:
            continue
        seen.add(key)
        card_text = c.get("text", "") or ""
        card_price = _parse_price(card_text)
        candidates.append({
            "address": addr,
            "price": card_price,
            "agent": _parse_broker(card_text),
            "phone": _parse_phone(card_text),
            "email": "",
            "area": _parse_area(card_text),
            # 卡片自己就写"价格面议"→ 详情页也没有数字，不必再开页（每条省 8s）
            "undisclosed": (not card_price) and (
                "undisclosed" in card_text.lower()
                or "contact for pricing" in card_text.lower()),
            "listing_url": c["url"],
        })
        if len(candidates) >= limit:
            break
    msg = (f"LoopNet 第 {page_no}/{total_pages or '?'} 页采集到 {len(candidates)} 个候选房源"
           f"（区间 ${price_min}-${price_max}/月）"
           if candidates else
           f"LoopNet 第 {page_no} 页未解析到候选地址（卡片结构可能变化，已留档搜索页截图）")
    return {"status": "pass" if candidates else "warn", "message": msg,
            "candidates": candidates, "screenshot": search_shot or "",
            "total_pages": total_pages, "page_ok": True}


def candidates_from_urls(urls: list) -> dict:
    """手动房源链接兜底：逐条打开详情页解析候选。"""
    candidates = []
    page = browser.new_page_in(_PROFILE)
    shot = ""
    try:
        _throttle()
        _warmup(page)
        for u in urls:
            u = u.strip()
            if not u:
                continue
            try:
                _throttle()
                browser.goto_retry(page, u, timeout=45000, wait=5000)
                shot = shot or browser.shot(page, "manual_loopnet")
                cand = _detail_candidate(page, u)
                if cand["address"]:
                    candidates.append(cand)
            except Exception:
                continue
    finally:
        browser.safe_close(page)
    return {"status": "pass" if candidates else "warn",
            "message": f"手动链接解析出 {len(candidates)} 个候选房源",
            "candidates": candidates, "screenshot": shot or ""}


_SPACES_JS = r"""
() => {
    const out = [];
    for (const tr of document.querySelectorAll("table tr")) {
        const cells = [...tr.children].map(td => (td.innerText || "").trim());
        if (cells.length < 3) continue;
        const joined = cells.join(" ");
        if (/^space/i.test(cells[0]) && /size/i.test(joined)) continue;   // 表头
        const sizeIdx = cells.findIndex(c => /^[\d,.]+\s*(SF|sq\.?\s*ft|sqft)/i.test(c));
        const rateIdx = cells.findIndex(c => /^\$/.test(c));
        if (sizeIdx < 0 || rateIdx < 0) continue;
        out.push({name: cells[0], size: cells[sizeIdx], rate: cells[rateIdx]});
        if (out.length >= 20) break;
    }
    return out;
}
"""


def _parse_spaces(page) -> list:
    """详情页「可用套间/空间」表：每行 = 套间名 + 面积 + 单价。
    例：["3rd Floor, Ste 302", "Office", "384 SF", "$28.00 /SF/YR", ...]
        ["33rd Floor, Ste 3320", "Office", "200 SF", "--", "$1,750 /MO", ...]"""
    try:
        rows = page.evaluate(_SPACES_JS) or []
    except Exception:
        return []
    spaces = []
    for r in rows:
        size = _parse_area(r.get("size", ""))
        rate = _parse_price(r.get("rate", ""))
        if not size or not rate:
            continue
        spaces.append({"name": r.get("name", ""), "size": size, "rate": rate,
                       "monthly": monthly_price(rate, size)})
    return spaces


def fetch_broker_info(listing_url: str) -> dict:
    """详情页补抓 价格/经纪人/电话/邮箱/面积 + 各套间面积与单价（spaces）。"""
    info = {"price": "", "agent": "", "phone": "", "email": "", "area": "", "spaces": [],
            "blocked": False}
    if not listing_url:
        return info
    page = browser.new_page_in(_PROFILE)
    try:
        # 固定空等 5s 太贵：DOMContentLoaded 后 3s 足够渲染价格/套间表
        # （提速只压"无意义空等"，不提高请求频率，反爬策略不变）
        _throttle()
        browser.goto_retry(page, listing_url, timeout=45000, wait=3000)
        if _blocked(page):
            # 详情页同样会被速率拦截：冷却后重试一次，仍失败就标记，
            # 避免把「被拦」当成「无价格/无面积」直接误判淘汰
            _cool_down(180)
            _throttle()
            browser.goto_retry(page, listing_url, timeout=45000, wait=6000, retries=1)
            if _blocked(page):
                info["blocked"] = True
                return info
        text = page.evaluate("() => document.body.innerText.slice(0, 8000)")
        info["price"] = _parse_price(text)
        info["agent"] = _parse_broker(text)
        info["phone"] = _parse_phone(text)
        info["area"] = _parse_area(text)
        info["spaces"] = _parse_spaces(page)   # 逐套间的面积/单价，价格判定必须用它
        # JSON-LD 里的经纪人信息
        try:
            lds = page.evaluate(
                "() => [...document.querySelectorAll('script[type=\"application/ld+json\"]')"
                "].map(s => s.textContent)")
            for ld in lds or []:
                try:
                    data = json.loads(ld)
                except Exception:
                    continue
                items = data if isinstance(data, list) else [data]
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    broker = it.get("broker") or {}
                    if isinstance(broker, dict) and broker.get("name") and not info["agent"]:
                        info["agent"] = broker["name"]
                    tel = it.get("telephone") or ""
                    if tel and not info["phone"]:
                        info["phone"] = tel
        except Exception:
            pass
        try:
            mail = page.evaluate(
                "() => { const a = document.querySelector(\"a[href^='mailto:']\");"
                " return a ? a.href.replace('mailto:', '').split('?')[0] : ''; }")
            if mail:
                info["email"] = mail
        except Exception:
            pass
    except Exception:
        pass
    finally:
        browser.safe_close(page)
    return info


# 注：房间号挖掘已统一由 Smarty 承担（smarty.api_mine_suites / web_mine_suites），
# LoopNet 只负责房源采集，不再参与房间号挖掘（用户明确要求：在 Smarty 里挖）。


def store_norm(addr: str) -> str:
    from .. import store
    return store.normalize(addr)
