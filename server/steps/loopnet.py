"""步骤1：LoopNet 商业地产房源采集（搜索页 + 详情页）。

返回候选结构（pipeline 约定）：
  candidates: [{address, price, agent, phone, email, area, listing_url}]
价格/经纪人/面积在卡片缺失时由 pipeline 调 fetch_broker_info 去详情页补抓。
"""
import json
import re

from .. import browser

_BASE = "https://www.loopnet.com"

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
        if (out.length >= 24) break;
    }
    return out;
}
"""


def _slug(city: str, state: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", f"{city} {state}").strip("-").lower()
    return s


def _parse_price(text: str) -> str:
    m = re.search(r"(\$[\d,]+(\.\d+)?\s*(/mo|/month|/yr|/SF/YR|SF/YR)?[^\n]{0,20})", text, re.I)
    return m.group(1).strip() if m else ""


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


def _parse_area(text: str) -> str:
    r"""面积（SF）：匹配 "1,234 SF" / "678 SqFt" / "1200 sq ft" 等。
    注意 (?<![.\d])：排除 "$20.40 SF/YR"（单价里的 40 SF）这种误匹配。"""
    m = re.search(r"(?<![.\d])(\d[\d,]*)\s*(?:sq\.?\s*ft|sqft|sf|sq\s*ft)\b", text, re.I)
    if not m:
        return ""
    return m.group(1).replace(",", "").rstrip(".")


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
    return {
        "address": addr,
        "price": _parse_price(text),
        "agent": _parse_broker(text),
        "phone": _parse_phone(text),
        "email": "",
        "area": _parse_area(text),
        "listing_url": url,
    }


_BLOCK_KEYWORDS = ("are you a robot", "access denied", "unusual traffic", "captcha")


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
    page = browser.new_page()
    blocked = False
    total_pages = 0
    try:
        _warmup(page)
        browser.goto_retry(page, url, timeout=45000, wait=6000)
        search_shot = browser.shot(page, f"{tag}_loopnet")
        body = page.evaluate("() => document.body.innerText").lower()
        blocked = any(k in body for k in _BLOCK_KEYWORDS)
        if blocked:
            # Access Denied（多为 IP 层拦截）：预热后重试一次
            page.wait_for_timeout(3000)
            _warmup(page)
            browser.goto_retry(page, url, timeout=45000, wait=8000, retries=1)
            search_shot = browser.shot(page, f"{tag}_loopnet_retry") or search_shot
            body = page.evaluate("() => document.body.innerText").lower()
            blocked = any(k in body for k in _BLOCK_KEYWORDS)
        if blocked:
            return {"status": "fail",
                    "message": "LoopNet 拦截（Access Denied/反爬）：请开启系统代理并选美国节点"
                               "（SOP：Clash Verge → 系统代理 → 凤凰城）后重试；"
                               "或在界面粘房源链接走手动兜底",
                    "candidates": [], "screenshot": search_shot or "", "total_pages": 0}
        cards = page.evaluate(_LIKING_JS) or []
        total_pages = _parse_total_pages(page) if cards else 0
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
        candidates.append({
            "address": addr,
            "price": _parse_price(c.get("text", "")),
            "agent": _parse_broker(c.get("text", "")),
            "phone": _parse_phone(c.get("text", "")),
            "email": "",
            "area": _parse_area(c.get("text", "")),
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
            "total_pages": total_pages}


def candidates_from_urls(urls: list) -> dict:
    """手动房源链接兜底：逐条打开详情页解析候选。"""
    candidates = []
    page = browser.new_page()
    shot = ""
    try:
        _warmup(page)
        for u in urls:
            u = u.strip()
            if not u:
                continue
            try:
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


def fetch_broker_info(listing_url: str) -> dict:
    """详情页补抓 价格/经纪人/电话/邮箱/面积（含 JSON-LD 与 mailto）。"""
    info = {"price": "", "agent": "", "phone": "", "email": "", "area": ""}
    if not listing_url:
        return info
    page = browser.new_page()
    try:
        browser.goto_retry(page, listing_url, timeout=45000, wait=5000)
        text = page.evaluate("() => document.body.innerText.slice(0, 8000)")
        info["price"] = _parse_price(text)
        info["agent"] = _parse_broker(text)
        info["phone"] = _parse_phone(text)
        info["area"] = _parse_area(text)
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
