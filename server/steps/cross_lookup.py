"""跨数据源交叉查询：按地址在其它平台找同楼盘挂牌，补经纪人电话/邮箱。

背景：Crexi / CommercialCafe 等源未登录或站点结构性原因拿不到电话，而
LoopNet / CityFeet / CBRE 的经纪人电话是公开的。同一栋楼常在多家平台同时
挂牌 → 拿标准化地址去别家搜同楼盘，命中就用别家的联系方式。

匹配规则（防错杀）：
  * 结果卡片文本必须同时包含「门牌号」和「街名前两个词」才算同一楼盘；
  * 命中后优先用卡片上的电话（LoopNet 卡片自带），没有再开详情页补抓。

代价：每条命中失败约 10-25s（一次搜索页导航），命中后不开详情页几乎无额外
耗时。由 pipeline 在「电话缺失/占位语」时才调用。
"""
import re

from .. import browser
from . import crexi, loopnet


def _slug(address: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", str(address)).strip("-").lower()


def _addr_tokens(address: str) -> tuple:
    """("14946", ["camden", "avenue"]) —— 门牌号 + 街名词干。"""
    s = str(address or "").strip()
    m = re.match(r"\s*(\d{1,6})\s+(.+?)(?:,|$)", s)
    if not m:
        return "", []
    num = m.group(1)
    words = [w for w in re.split(r"[^a-zA-Z]+", m.group(2)) if w]
    # 去掉方向词/单元词，降低匹配噪声
    skip = {"n", "s", "e", "w", "north", "south", "east", "west",
            "suite", "ste", "unit", "room"}
    words = [w.lower() for w in words if w.lower() not in skip]
    return num, [w.lower().rstrip("e") for w in words[:2]]


def _card_matches(card_text: str, num: str, words: list) -> bool:
    t = (card_text or "").lower()
    if not num or not t:
        return False
    if num not in t:
        return False
    return all(w[:4] in t for w in words)   # 词干前 4 字符，容忍 Ave/Avenue


def _via_loopnet(address: str, tag: str) -> dict:
    """LoopNet 地址关键词搜索 → 同楼盘卡片/详情页 → 电话邮箱。"""
    num, words = _addr_tokens(address)
    if not num:
        return {}
    url = (f"https://www.loopnet.com/search/{_slug(address)}/for-lease/")
    page = browser.new_page_in(loopnet._PROFILE)
    try:
        browser.goto_retry(page, url, timeout=45000, wait=5000, retries=1)
        body = (page.evaluate("() => document.body.innerText") or "").lower()
        if any(k in body for k in ("access denied", "unusual activity",
                                   "are you a robot")):
            return {}
        cards = page.evaluate(loopnet._LIKING_JS) or []
        hit = None
        for c in cards:
            if _card_matches(c.get("text", ""), num, words):
                hit = c
                break
        if hit is None:
            return {}
        phone = loopnet._parse_phone(hit.get("text", ""))
        agent = loopnet._parse_broker(hit.get("text", ""))
        email = ""
        listing_url = hit["url"]
        if not phone:
            # 卡片没电话 → 开详情页补（LoopNet 详情页电话/邮箱公开）
            info = loopnet.fetch_broker_info(listing_url)
            phone = info.get("phone") or ""
            agent = agent or info.get("agent") or ""
            email = info.get("email") or ""
        return {"source": "loopnet", "listing_url": listing_url,
                "agent": agent, "phone": phone, "email": email}
    except Exception:
        return {}
    finally:
        browser.safe_close(page)


def _via_crexi(address: str) -> dict:
    """Crexi 按地址查同楼盘 → 详情页（登录态下电话可见）。"""
    hit = crexi.lookup_suites_by_address(address)
    url = (hit or {}).get("listing_url") or ""
    if not url:
        return {}
    try:
        info = crexi.fetch_broker_info(url)
    except Exception:
        return {}
    return {"source": "crexi", "listing_url": url,
            "agent": info.get("agent") or "", "phone": info.get("phone") or "",
            "email": info.get("email") or ""}


def lookup_contact(address: str, exclude_source: str = "", tag: str = "cross") -> dict:
    """按地址交叉查经纪人联系方式。返回 {} 表示没查到。

    顺序：LoopNet（电话公开、卡片自带）→ Crexi（登录态详情页）。
    exclude_source：跳过与当前记录相同的源（本源自己都拿不到，别再查一遍）。
    """
    addr = (address or "").strip()
    if not addr or not re.search(r"\d", addr):
        return {}
    if exclude_source != "loopnet":
        r = _via_loopnet(addr, tag)
        if r.get("phone"):
            return r
    if exclude_source != "crexi":
        r = _via_crexi(addr)
        if r.get("phone"):
            return r
    return {}
