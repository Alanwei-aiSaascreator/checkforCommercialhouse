"""步骤2：Smarty US Street Address API 验证地址有效性（含缓存与每日额度）。

返回结构（pipeline 约定）：
  status: pass | fail | suspect | quota
  message: 中文结果描述（fail 缺房间号时需含 "缺房间号"/"N1" 以触发 Suite 挖掘）
  screenshot: 结果卡截图路径
  standard_address: USPS 标准化地址
"""
import html
import re
import threading
from pathlib import Path

import httpx

from .. import browser, config, store


def _smarty_query(address: str) -> dict:
    # 注意（401 根因，勿改回）：
    # 1. auth-token 是 website secret key，只能以 query 参数传递，不支持 HTTP Basic 认证；
    # 2. 请求不能携带 Referer 头，否则 Smarty 判定为浏览器请求并拒绝 secret key
    #    （报错 "Secret key in browser request" → HTTP 401）。
    params = {
        "street": address,
        "candidates": "1",
        "auth-id": config.SMARTY_AUTH_ID,
        "auth-token": config.SMARTY_AUTH_TOKEN,
    }
    if config.PROXY_URL:
        proxies = config.PROXY_URL
    else:
        proxies = None
    with httpx.Client(timeout=25, proxy=proxies) as client:
        r = client.get(
            "https://us-street.api.smartystreets.com/street-address",
            params=params,
        )
        r.raise_for_status()
        data = r.json()
    return (data or [{}])[0]


def _result_card(tag: str, address: str, ok: bool, lines: list) -> str:
    """把 API 结果渲染成结果卡并截图（绿勾=通过项，红叉=未过项，即“双绿”凭证）。"""
    rows = ""
    for label, good, detail in lines:
        icon = "✅" if good else "❌"
        rows += (f"<tr><td style='padding:10px 18px;font-size:20px'>{icon}</td>"
                 f"<td style='padding:10px 6px;font-weight:600'>{html.escape(label)}</td>"
                 f"<td style='padding:10px;color:#555'>{html.escape(detail)}</td></tr>")
    color = "#0a7a4d" if ok else "#b3261e"
    title = "Smarty 地址验证 · 通过" if ok else "Smarty 地址验证 · 未通过"
    page = browser.new_page()
    try:
        page.set_content(f"""
        <div style="font-family:Segoe UI,Arial,sans-serif;padding:28px">
          <div style="border:2px solid {color};border-radius:12px;max-width:680px;overflow:hidden">
            <div style="background:{color};color:#fff;padding:14px 20px;font-size:20px;font-weight:700">
              {title}
            </div>
            <div style="padding:12px 20px;color:#333;font-size:15px">
              查询地址：<b>{html.escape(address)}</b>
            </div>
            <table style="border-collapse:collapse;width:100%">{rows}</table>
            <div style="padding:10px 20px;color:#888;font-size:12px">
              Smarty US Street Address API · {tag}
            </div>
          </div>
        </div>""", wait_until="networkidle")
        return browser.shot(page, f"{tag}_smarty")
    except Exception:
        return ""
    finally:
        browser.safe_close(page)


def _quota_message() -> str:
    n = store.quota_today()
    return f"Smarty 免费额度已用完（今日已查 {n}/{config.SMARTY_DAILY_LIMIT} 次），任务停止，请明天再跑或升级套餐"


_SUFFIX_ALIAS = {
    "st": "street", "str": "street", "ave": "avenue", "av": "avenue",
    "rd": "road", "dr": "drive", "blvd": "boulevard", "ln": "lane",
    "ct": "court", "way": "way", "pl": "place", "cir": "circle",
    "hwy": "highway", "pkwy": "parkway", "ter": "terrace", "trl": "trail",
    "n": "north", "s": "south", "e": "east", "w": "west",
}


def _suffix_changed(address: str, components: dict) -> str:
    """SOP 常见问题③：街道后缀发生变化（输入 St，Smarty 返回 Ave 等）。
    返回变化描述（如 "St → Ave"），未变化返回空串。"""
    try:
        street_part = address.split(",")[0].strip()
        words = street_part.split()
        if len(words) < 2:
            return ""
        in_suffix = words[-1].lower().rstrip(".")
        out_suffix = (components.get("street_suffix") or "").lower().rstrip(".")
        if not in_suffix or not out_suffix:
            return ""
        a = _SUFFIX_ALIAS.get(in_suffix, in_suffix)
        b = _SUFFIX_ALIAS.get(out_suffix, out_suffix)
        if a != b:
            return f"{words[-1]} → {components.get('street_suffix')}"
    except Exception:
        pass
    return ""


def verify_address(address: str, tag: str = "smarty") -> dict:
    norm = store.normalize(address)

    # 1) 缓存命中：不消耗额度（凭证卡仍然渲染归档——SOP 要求双绿截图留档）
    cached = store.smarty_cache_get(norm)
    if cached:
        status = cached["status"]
        if status == "pass":
            shot = _result_card(tag, address, ok=True, lines=[
                ("DPV 送验匹配", True, "dpv_match_code = Y（缓存命中，未耗额度）"),
                ("USPS 标准化地址", True, cached["standard"] or address),
            ])
            return {"status": "pass", "message": "Smarty API 验证有效（双绿，缓存命中）",
                    "screenshot": shot, "standard_address": cached["standard"] or ""}
        if status == "missing":
            # 缺房间号（N1）类失败：消息含"缺房间号"以触发上层 Suite 挖掘
            shot = _result_card(tag, address, ok=False, lines=[
                ("主地址匹配", True, "dpv_match_code = D（缓存命中）"),
                ("房间号（二级地址）", False, "缺失房间号（N1，缓存命中）"),
            ])
            return {"status": "fail",
                    "message": "缓存结果：地址有效但缺房间号（N1），missing secondary",
                    "screenshot": shot, "standard_address": ""}
        if status == "fail":
            shot = _result_card(tag, address, ok=False, lines=[
                ("查询结果", False, "此前验证未通过（缓存命中，未耗额度）"),
            ])
            return {"status": "fail", "message": "缓存结果：Smarty 未通过（此地址此前验证未通过）",
                    "screenshot": shot, "standard_address": ""}

    # 2) 密钥未配置：明确提示（不当作淘汰依据太可惜，给 suspect 由上层保守处理）
    if not (config.SMARTY_AUTH_ID and config.SMARTY_AUTH_TOKEN):
        return {"status": "suspect", "screenshot": "", "standard_address": "",
                "message": "未配置 Smarty 密钥（SMARTY_AUTH_ID / SMARTY_AUTH_TOKEN），无法验证"}

    # 3) 额度检查：耗尽则整任务停止（pipeline 对 quota 的处理）
    if store.quota_today() >= config.SMARTY_DAILY_LIMIT:
        return {"status": "quota", "message": _quota_message(),
                "screenshot": "", "standard_address": ""}

    # 4) 调用 API
    try:
        r = _smarty_query(address)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return {"status": "suspect", "screenshot": "", "standard_address": "",
                    "message": "Smarty 认证失败（401），请检查密钥"}
        if e.response.status_code == 402:
            return {"status": "quota", "message": _quota_message(),
                    "screenshot": "", "standard_address": ""}
        return {"status": "suspect", "screenshot": "", "standard_address": "",
                "message": f"Smarty API 请求失败: {e}"}
    except Exception as e:
        return {"status": "suspect", "screenshot": "", "standard_address": "",
                "message": f"Smarty API 请求异常: {e}"}

    store.quota_incr()

    if not r:
        store.smarty_cache_put(norm, "fail")
        shot = _result_card(tag, address, ok=False,
                            lines=[("查询结果", False, "API 无返回候选，查询未知")])
        return {"status": "fail",
                "message": "查询未知：Smarty 未匹配到该地址（无效地址），按 SOP 直接淘汰",
                "screenshot": shot, "standard_address": ""}

    analysis = r.get("analysis") or {}
    # 实测字段名是 dpv_match_code（旧代码读 dpv_match 恒为空，导致双绿判定失效）
    dpv = analysis.get("dpv_match_code") or analysis.get("dpv_match", "")
    # dpv_footnotes 是字符串（如 "AAN1"，N1=高层缺房间号）；兼容列表返回
    fn = analysis.get("dpv_footnotes") or ""
    footnotes = ",".join(fn) if isinstance(fn, list) else str(fn)
    components = r.get("components") or {}
    # Smarty 官方键名是 city_name / state_abbreviation（此前用错导致城市州显示为空）
    city = (components.get("city_name") or components.get("default_city_name")
            or components.get("city") or "")
    state = (components.get("state_abbreviation") or components.get("state") or "")
    zipcode = components.get("zipcode", "") + (f"-{components['plus4_code']}" if components.get("plus4_code") else "")
    standard = f"{r.get('delivery_line_1', '')}, {city} {state} {zipcode}".strip(" ,")

    # SOP 常见问题：地址为数字区间（如 202-208 W 4th St）→ 采用 Smarty 给出的具体门牌号
    range_note = ""
    if re.search(r"\b\d+\s*[-–]\s*\d+\s+\w", address):
        range_note = f"（输入为门牌号区间，已采用 Smarty 门牌号：{components.get('primary_number', '')}）"

    if dpv == "Y":
        vacant = analysis.get("dpv_vacant") == "Y"
        if vacant:
            # SOP 常见问题：房产空置 → 直接淘汰
            store.smarty_cache_put(norm, "fail")
            shot = _result_card(tag, address, ok=False, lines=[
                ("DPV 送验匹配", True, f"dpv_match = Y"),
                ("房产占用状态", False, "dpv_vacant = Y（房产空置）"),
            ])
            return {"status": "fail",
                    "message": "房产空置（dpv_vacant=Y），按 SOP 直接淘汰",
                    "screenshot": shot, "standard_address": ""}
        store.smarty_cache_put(norm, "pass", standard)
        shot = _result_card(tag, address, ok=True, lines=[
            ("DPV 送验匹配", True, f"dpv_match = Y（footnotes: {footnotes or '无'}）"),
            ("ZIP / 城市-州", True, f"{city}, {state} {zipcode}"),
            ("USPS 标准化地址", True, standard),
        ])
        msg = "Smarty API 验证有效（双绿）"
        if range_note:
            msg += range_note
        return {"status": "pass", "message": msg,
                "screenshot": shot, "standard_address": standard}

    if dpv == "D" or "N1" in footnotes.upper():
        # 主地址有效但缺二级（房间号）→ 触发 Suite 挖掘重验
        # 缓存状态用 missing 区分于普通 fail（命中时才能继续触发 Suite 挖掘）
        store.smarty_cache_put(norm, "missing")
        shot = _result_card(tag, address, ok=False, lines=[
            ("主地址匹配", True, f"dpv_match = {dpv}"),
            ("房间号（二级地址）", False, f"缺失房间号（footnotes: {footnotes or 'N1'}）"),
        ])
        return {"status": "fail",
                "message": f"地址有效但缺房间号（dpv_match={dpv}, footnotes={footnotes or 'N1'}），missing secondary",
                "screenshot": shot, "standard_address": ""}

    # SOP 常见问题：街道后缀发生变化 → 直接淘汰
    suffix_note = _suffix_changed(address, components)
    store.smarty_cache_put(norm, "fail")
    shot = _result_card(tag, address, ok=False, lines=[
        ("DPV 送验匹配", False, f"dpv_match = {dpv or 'N'}（footnotes: {footnotes or '无'}）"),
        ("ZIP / 城市-州", bool(r.get("delivery_line_1")), f"{city}, {state} {zipcode}"),
    ])
    if suffix_note:
        return {"status": "fail",
                "message": f"街道后缀发生变化（{suffix_note}），非双绿，按 SOP 直接淘汰",
                "screenshot": shot, "standard_address": ""}
    return {"status": "fail", "message": f"Smarty 未通过（dpv_match={dpv or 'N'}, footnotes={footnotes}）",
            "screenshot": shot, "standard_address": ""}


# ---------- 大楼房间号挖掘（Smarty 优先，官网部件兜底） ----------

_AUTOCOMPLETE_URL = "https://us-autocomplete.api.smartystreets.com/suggest"
_SMARTY_WIDGET_URL = "https://www.smartystreets.com/products/single-address"


def _auth_params() -> dict:
    return {"auth-id": config.SMARTY_AUTH_ID, "auth-token": config.SMARTY_AUTH_TOKEN}


def _split_street_rest(address: str) -> tuple:
    """把 "5757 W Century Blvd, Los Angeles, CA 90045" 拆成 (街道, ", Los Angeles, CA 90045")。"""
    parts = address.split(",", 1)
    if len(parts) == 2:
        return parts[0].strip(), "," + parts[1]
    return address.strip(), ""


def api_mine_suites(address: str, max_suites: int = 6):
    """官方 US Autocomplete API 挖房间号（需订阅；返回 None 表示未订阅/不可用，
    调用方应降级官网部件方案；开通订阅后本函数自动生效）。"""
    street, rest = _split_street_rest(address)
    params = {"search": street, "max_results": 20, **_auth_params()}
    try:
        with httpx.Client(timeout=25, proxy=config.PROXY_URL or None) as client:
            r = client.get(_AUTOCOMPLETE_URL, params=params)
        if r.status_code != 200:
            return None  # 402=未订阅；其他错误也交给兜底
        entries = []
        for s in r.json().get("suggestions", []):
            line = s.get("street_line", "")
            m = re.search(r"\b(?:Ste|Suite|#)\s*#?\s*([A-Za-z0-9-]+)\b", line, re.I)
            if m:
                city = s.get("city", "")
                state = s.get("state_abbreviation") or s.get("state", "")
                zc = s.get("zipcode", "")
                tail = f", {city}, {state} {zc}".strip(", ")
                entries.append(f"{street} {m.group(1).upper()}{(' ' + tail) if rest == '' else rest}")
        # 去重保序
        seen, out = set(), []
        for e in entries:
            if store.normalize_precise(e) not in seen:
                seen.add(store.normalize_precise(e))
                out.append(e)
        return out[:max_suites]
    except Exception:
        return None


_web_mine_last_error = ""  # 调试用：最近一次官网挖号失败原因


def _web_mine_worker(address: str, max_suites: int) -> list:
    """（内部）在独立线程中运行的官网挖号实现：独立临时 Chrome，英文环境。"""
    global _web_mine_last_error
    street, rest = _split_street_rest(address)
    import tempfile
    from playwright.sync_api import sync_playwright

    tmp = tempfile.mkdtemp(prefix="smarty_mine_")
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                tmp, headless=False,
                args=["--disable-blink-features=AutomationControlled", "--lang=en-US"],
                proxy={"server": config.PROXY_URL} if config.PROXY_URL else None,
                viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            try:
                page.goto(_SMARTY_WIDGET_URL, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
                box = page.locator("#us-street-address-entry").first
                box.scroll_into_view_if_needed(timeout=15000)
                box.click(timeout=5000)
                box.type(" ".join(address.replace(",", " ").split()), delay=50)
                # 显式等待下拉选项出现（异步加载，固定等待会竞态）
                page.wait_for_selector("li[id^='us-street-address-entry-option']",
                                       timeout=12000)
                page.wait_for_timeout(1500)
                ste_opt = page.locator("li[id^='us-street-address-entry-option']").filter(
                    has_text=re.compile(r"Ste|Suite", re.I))
                if not ste_opt.count():
                    _web_mine_last_error = "下拉无 Ste 折叠项（该楼可能无房间号）"
                    return []
                ste_opt.first.click()
                page.wait_for_timeout(4500)
                texts = page.evaluate(
                    """() => [...document.querySelectorAll(
                            "li[id^='us-street-address-entry-option'], [role=option]")]
                         .map(e => e.innerText.trim()).filter(Boolean)""")
                seen, suites = set(), []
                for t in texts:
                    m = re.search(r"\bSte\s*#?\s*([A-Za-z0-9-]+)\b", t, re.I)
                    if m and m.group(1).upper() not in seen:
                        seen.add(m.group(1).upper())
                        suites.append(m.group(1).upper())
                if not suites:
                    _web_mine_last_error = "点击后未解析到房间号"
                return [f"{street} Ste {s}{rest}" for s in suites[:max_suites]]
            finally:
                ctx.close()
    except Exception as e:
        _web_mine_last_error = f"{type(e).__name__}: {e}"
        return []


def web_mine_suites(address: str, max_suites: int = 6, timeout: int = 90) -> list:
    """在 Smarty 官网免费验证部件里挖房间号（用户指定方案：Smarty 里挖、Smarty 里验）。

    必须在【独立线程】中执行：Playwright 同步 API 不允许同一线程内同时存在
    两个实例——流水线线程已占用一个（服务常驻浏览器），直接调用会立即失败
    （表现为秒回空结果、退化为 LoopNet 兜底）。独立线程 + 独立临时 Chrome
    解决该问题，也不影响服务浏览器。

    网页部件走 smarty.com 站点自身额度，不消耗 API 免费额度。
    返回房间号级完整地址列表，失败返回 []。"""
    result = {"suites": []}

    def _run():
        result["suites"] = _web_mine_worker(address, max_suites)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return result["suites"]
