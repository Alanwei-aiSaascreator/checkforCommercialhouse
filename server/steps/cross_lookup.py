"""跨数据源交叉查询：按地址在其它平台找同楼盘挂牌，补经纪人电话/邮箱。

背景：Crexi / CommercialCafe 等源未登录或站点结构性原因拿不到电话（Crexi
免费账号给的是平台统一号 888-273-0423，已按占位语过滤），而 LoopNet 的
经纪人电话是公开的。同一栋楼常在多家平台同时挂牌 → 拿地址去别家搜同楼盘，
命中就用别家的联系方式。

LoopNet 通道实现（实测踩坑记录）：
  * 直接拼 /search/{地址}/for-lease/ 是 404（LoopNet 搜索路径只认城市段）；
  * 城市搜索页的 ?sk= 关键词参数被静默忽略；
  * 所以走 DuckDuckGo HTML 端点做 site:loopnet.com 站内搜索（返回真实直链、
    对自动化友好），命中后用 loopnet.fetch_broker_info 抓详情页电话——
    实测 "223A 9th St" 一次命中真实号码 (415) 592-4862。

匹配规则（防错杀）：
  * DDG 结果的标题/摘要/URL 必须同时含「门牌号」和「街名词干」才算同楼盘；
  * 命中后走详情页解析（LoopNet 详情页电话/邮箱公开，fetch_broker_info 可用）。

代价：每条约 8-20s（一次 DDG 搜索 + 一次详情页）。多工人共享一条节流队列。
"""
import base64
import re
import threading
import time
from urllib.parse import quote, unquote

from .. import browser
from . import crexi, loopnet

# 多工人共享节流：所有工人同出口 IP，DDG/Bing 查询过密会触发验证码。
# 4 工人任务实测把两家引擎都打到验证码 → 间隔必须足够大（8s/次全队共享，
# 约合 7.5 次/分钟——引擎限流阈值之下）。
_ddg_lock = threading.Lock()
_ddg_last = 0.0
_DDG_MIN_INTERVAL = 8.0
# 引擎熔断：引擎弹验证码后停用一段时间（期间交叉查询直接跳过该引擎，
# 不再白开浏览器），冷却后自动恢复
_engine_down = {"ddg": 0.0, "bing": 0.0}
_ENGINE_COOLDOWN = 1800.0
# DDG 的验证页文案实测是 "Unfortunately, bots use DuckDuckGo too. Please
# complete the following challenge..."（选鸭子），与 Bing 的措辞不同——
# 词表必须两家都覆盖，否则熔断不触发、引擎被持续白打
_CHALLENGE_MARKS = ("solve the challenge", "unusual traffic",
                    "are you a robot", "verify you are human",
                    "bots use duckduckgo", "confirm this search was made",
                    "select all squares")


def _engine_ok(name: str) -> bool:
    return time.time() >= _engine_down.get(name, 0.0)


def _page_is_challenge(body: str) -> bool:
    low = (body or "").lower()
    return any(k in low for k in _CHALLENGE_MARKS)


def _ddg_search(query: str) -> list:
    """DuckDuckGo HTML 端点搜索，返回 [{t, u, snip}]（u 已还原真实直链）。"""
    if not _engine_ok("ddg"):
        return []
    page = browser.new_page_in(loopnet._PROFILE)
    try:
        url = "https://html.duckduckgo.com/html/?q=" + quote(query)
        browser.goto_retry(page, url, timeout=45000, wait=2500, retries=1)
        body = page.evaluate("() => document.body.innerText") or ""
        if _page_is_challenge(body):
            _engine_down["ddg"] = time.time() + _ENGINE_COOLDOWN
            return []
        results = page.evaluate(
            """() => [...document.querySelectorAll('div.result, div.web-result')]
                .map(d => {
                    const a = d.querySelector('a.result__a');
                    const s = d.querySelector('a.result__snippet');
                    return {t: (a ? a.innerText : ''), u: (a ? a.href : ''),
                            snip: (s ? s.innerText : '')};
                }).slice(0, 8)""") or []
        out = []
        for r in results:
            u = str(r.get("u") or "")
            if "duckduckgo.com" in u:
                m = re.search(r"uddg=([^&]+)", u)
                if m:
                    u = unquote(m.group(1))
            out.append({"t": r.get("t") or "", "u": u,
                        "snip": r.get("snip") or ""})
        return out
    finally:
        browser.safe_close(page)


def _decode_bing(u: str) -> str:
    """Bing 跳转链接 /ck/a?...&u=a1<base64url> → 真实 URL。"""
    m = re.search(r"[?&]u=a1([^&]+)", u or "")
    if not m:
        return ""
    b = m.group(1)
    b += "=" * (-len(b) % 4)
    try:
        return base64.urlsafe_b64decode(b).decode("utf-8", "ignore")
    except Exception:
        return ""


def _bing_search(query: str) -> list:
    """Bing 搜索兜底（DDG 限流时用）：li.b_algo → 跳转链接解码为真实 URL。"""
    if not _engine_ok("bing"):
        return []
    page = browser.new_page_in(loopnet._PROFILE)
    try:
        url = "https://www.bing.com/search?q=" + quote(query)
        browser.goto_retry(page, url, timeout=45000, wait=3000, retries=1)
        body = page.evaluate("() => document.body.innerText") or ""
        if _page_is_challenge(body):
            _engine_down["bing"] = time.time() + _ENGINE_COOLDOWN
            return []
        results = page.evaluate(
            """() => [...document.querySelectorAll('li.b_algo')].map(li => {
                const a = li.querySelector('h2 a');
                const s = li.querySelector('p');
                return {t: (a ? a.innerText : ''), u: (a ? a.href : ''),
                        snip: (s ? s.innerText : '')};
            }).slice(0, 8)""") or []
        out = []
        for r in results:
            u = _decode_bing(str(r.get("u") or "")) or str(r.get("u") or "")
            out.append({"t": r.get("t") or "", "u": u,
                        "snip": r.get("snip") or ""})
        return out
    finally:
        browser.safe_close(page)


def _search_listings(query: str) -> list:
    """双引擎搜索：DDG 优先，空结果时切 Bing（两家交替被限流时互为备份）。"""
    results = []
    try:
        results = _ddg_search(query)
    except Exception:
        results = []
    if not results:
        try:
            results = _bing_search(query)
        except Exception:
            results = []
    return results


def _slug(address: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", str(address)).strip("-").lower()


def _addr_tokens(address: str) -> tuple:
    """("223A", ["9th", "street"]) —— 门牌号（可带单元字母）+ 街名词干。"""
    s = str(address or "").strip()
    # "223A 9th Street"：门牌号允许带单元字母（223A/330B），此前 \d{1,6}\s+
    # 要求号后必须是空格，遇到 "223A" 整个解析失败 → 交叉查询静默查不到
    m = re.match(r"\s*(\d{1,6}[A-Za-z]?)\s+(.+?)(?:,|$)", s)
    if not m:
        return "", []
    num = m.group(1)
    # 按字母+数字切分：此前按纯字母切分会把 "9th" 拆成 "th"（数字丢失），
    # "56th"/"3rd" 这类街名全部失配
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", m.group(2)) if w]
    # 去掉方向词/单元词，降低匹配噪声
    skip = {"n", "s", "e", "w", "north", "south", "east", "west",
            "suite", "ste", "unit", "room"}
    words = [w.lower() for w in words if w.lower() not in skip]
    return num, [w.lower().rstrip("e") for w in words[:2]]


def _card_matches(card_text: str, num: str, words: list) -> bool:
    t = (card_text or "").lower()
    if not num or not t:
        return False
    num_l = num.lower()
    if num_l not in t:
        # "223A" 的挂牌常写作 "223"（单元号在标题里省略）→ 退而匹配裸门牌号
        bare = re.sub(r"[A-Za-z]+$", "", num_l)
        if not bare or bare not in t:
            return False
    return all(w[:4] in t for w in words)   # 词干前 4 字符，容忍 Ave/Avenue


def _via_loopnet(address: str, tag: str) -> dict:
    """已停用：改架构后 LoopNet 不再开详情页（Akamai 额度只留给搜索页出候选）。

    原来这里 DDG 搜 LoopNet → loopnet.fetch_broker_info 开详情页抓电话，但详情页
    停用后拿不到电话；而 DDG 搜索命中 LoopNet 直链后也无法落地。直接返回空，
    电话/经纪人改由 _via_crexi（Crexi 登录态详情页）补。保留函数签名避免调用方改动。
    """
    return {}


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

    顺序：Crexi（登录态详情页）。LoopNet 详情页通道已停用（额度留给搜索页出候选）。
    exclude_source：跳过与当前记录相同的源（本源自己都拿不到，别再查一遍）。
    """
    addr = (address or "").strip()
    if not addr or not re.search(r"\d", addr):
        return {}
    if exclude_source != "crexi":
        r = _via_crexi(addr)
        if r.get("phone"):
            return r
    return {}


def engines_down() -> bool:
    """两个搜索引拍是否都处于验证码熔断中（pipeline 用它把'未命中'和
    '被限流'区分开——前者是正常结果，后者是暂时故障）。"""
    return not (_engine_ok("ddg") or _engine_ok("bing"))
