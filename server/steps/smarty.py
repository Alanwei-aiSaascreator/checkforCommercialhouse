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
import time
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
    proxies = config.PROXY_URL or None
    # 429（频率限制）：按 Retry-After 退避重试，最多 3 次；仍失败抛给上层按"限流"处理
    for attempt in range(3):
        with httpx.Client(timeout=25, proxy=proxies) as client:
            r = client.get(
                "https://us-street.api.smartystreets.com/street-address",
                params=params,
            )
        if r.status_code == 429 and attempt < 2:
            try:
                wait = float(r.headers.get("Retry-After") or 0)
            except Exception:
                wait = 0
            time.sleep(min(max(wait, 2.0 * (attempt + 1)), 15.0))
            continue
        r.raise_for_status()
        data = r.json()
        break
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


# ---------- 大楼房间号挖掘（真正订阅优先，官网免费通道兜底） ----------
#
# 现状（2026-11 实测，别改回老写法）：
#   * 官方 US Autocomplete API 能给出房号建议 → api_mine_suites；未订阅时返回 402；
#   * 官网旧「single-address」部件早已改版成纯输入框，下拉选项消失，
#     老的"等下拉弹 Ste 选项"方案 100% 超时（这是通过率骤降、每个地址白等 30-40s 的根因）；
#   * smarty.com 演示页自身用一把公开 demo key 调 us-street.api.smarty.com，
#     该接口返回 DPV 匹配码：Y=房号真实存在 / S=街址有效但房号未被 USPS 确认 / D=缺房号。
#
# 新方案：楼层锚点猜 + DPV 判定（Ste 100/200/300… 逐层探，命中 Y 即收工）
#   - **默认用自有 auth-id/auth-token 后台直查**（稳定、无公共 key 限流、可长期跑）
#   - 自有密钥不可用（未配置/401/402 额度用完）时才退回官网 demo key 兜底
#   - demo key 是 smarty.com 演示页的公共凭证，全世界共用额度，连打十几次必 429，
#     所以只有兜底时才用，且限速 1.1s/次
#   - 两条通道都不可用 → 上层按"淘汰"处理，但不写失败档案（下次重跑还能挖）

# 新版接口（旧的 us-autocomplete.api.smartystreets.com/suggest 已退役，别再指回去）
_AUTOCOMPLETE_URL = "https://us-autocomplete.api.smarty.com/v2/lookup"
_DEMO_STREET_API = "https://us-street.api.smarty.com/street-address"
_DEMO_HEADERS = {"Referer": "https://www.smarty.com/", "Origin": "https://www.smarty.com"}
# 楼层锚点：写字楼套间多是 100/200/300（三位数楼层号），末尾几个是甲级/单元式备用模式
_MINE_ANCHORS = (100, 200, 300, 400, 500, 600, 700, 800, 900,
                 101, 102, 103, 110, 1, 2, 10, 20)
_MINE_MAX_CALLS = 16
_DEMO_MIN_GAP = 1.1          # demo key 的最小间隔（共享额度，别打太猛）
_OWN_MIN_GAP = 0.15          # 自有密钥：套餐一般 10 req/s，0.15s 间隔安全
_probe_lock = threading.Lock()
_probe_last = {"own": 0.0, "demo": 0.0}


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
            # v2 把房号单独放 secondary 字段；老版是拼在 street_line 里
            sec = (s.get("secondary") or "").strip()
            m = re.search(r"\b(?:Ste|Suite|#)\s*#?\s*([A-Za-z0-9-]+)\b",
                          sec or line, re.I)
            if m:
                city = s.get("city", "")
                state = s.get("state_abbreviation") or s.get("state", "")
                zc = s.get("zipcode", "")
                tail = f", {city}, {state} {zc}".strip(", ")
                entries.append(f"{street} Ste {m.group(1).upper()}"
                               f"{(' ' + tail) if rest == '' else rest}")
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


def _own_key_ready() -> bool:
    return bool(getattr(config, "SMARTY_AUTH_ID", "") and getattr(config, "SMARTY_AUTH_TOKEN", ""))


def _probe_params(street: str, demo: bool) -> dict:
    if demo:
        return {"key": config.SMARTY_DEMO_KEY, "match": "enhanced",
                "candidates": 1, "street": street}
    return {"auth-id": config.SMARTY_AUTH_ID, "auth-token": config.SMARTY_AUTH_TOKEN,
            "candidates": 1, "street": street}


def _probe_client(demo: bool) -> httpx.Client:
    # 自有密钥（secret key）不能带 Referer，否则 Smarty 按浏览器请求拒绝 → 401
    headers = dict(_DEMO_HEADERS) if demo else {}
    return httpx.Client(timeout=15, proxy=config.PROXY_URL or None, headers=headers)


def _probe_dpv(client: httpx.Client, street: str, demo: bool) -> tuple:
    """探一次 DPV 匹配码，返回 (dpv, err)。

    dpv 非空 = 拿到结论（Y=房号真实存在 / S / D / N）；
    err = rate(429) / auth(401) / quota(402) / net，供上层决定要不要换通道。
    """
    mode = "demo" if demo else "own"
    gap = _DEMO_MIN_GAP if demo else _OWN_MIN_GAP
    for attempt in range(2):
        with _probe_lock:
            wait = gap - (time.time() - _probe_last[mode])
            if wait > 0:
                time.sleep(wait)
            _probe_last[mode] = time.time()
        try:
            r = client.get(_DEMO_STREET_API, params=_probe_params(street, demo))
        except Exception:
            return "", "net"
        if r.status_code == 200:
            try:
                arr = r.json()
            except Exception:
                return "", "net"
            if not arr:
                return "N", ""
            return str(arr[0].get("analysis", {}).get("dpv_match_code") or ""), ""
        if r.status_code == 429:
            if attempt == 0:
                time.sleep(6 if demo else 3)
                continue
            return "", "rate"
        if r.status_code == 402:
            return "", "quota"
        if r.status_code in (401, 403):
            return "", "auth"
        return "", f"http{r.status_code}"
    return "", "rate"


def channel_down(note: str) -> bool:
    """挖号通道故障（限流/鉴权/额度）≠ 这个地址没有房号：
    上层据此按"淘汰"处理，不写失败档案（下次重跑还能挖）。"""
    return any(k in (note or "") for k in ("通道不可用", "限流"))


def _down_text(err: str, calls: int) -> str:
    if err == "rate":
        return f"挖号通道被限流（429），已试 {calls} 个锚点"
    if err == "quota":
        return f"挖号通道不可用（402/账号额度用完），已试 {calls} 个锚点"
    if err == "auth":
        return f"挖号通道不可用（401/密钥无效），已试 {calls} 个锚点"
    return f"挖号通道不可用（{err}），已试 {calls} 个锚点"


def web_mine_suites(address: str, max_suites: int = 6, timeout: int = 90) -> list:
    """挖房间号：楼层锚点猜 + DPV 判定，**默认自有密钥后台直查**，demo key 仅兜底。

    思路：写字楼套间基本都是三位楼层号（Ste 100/200/300…），拿锚点逐层试，
    返回 DPV=Y 的真房号即收工（管线只要一个可用房号就能继续验证）。
    实测 343 E Main St(Stockton) → Ste 100 为 Y；195 S Beverly Dr → Ste 400 为 Y；
    11751 Mississippi Ave → Ste 100 为 Y。自有密钥下通常 1-2 次即命中。

    timeout 形参保留兼容旧调用（HTTP 路径远快于该上限）。失败/无命中返回 []，
    具体原因写进 _web_mine_last_error，供日志展示（不再静默超时）。
    """
    global _web_mine_last_error
    _web_mine_last_error = ""
    # 跨任务缓存：同一栋楼挖过就不再探测
    cached = store.mined_get(address)
    if cached is not None:
        suites, note = cached
        _web_mine_last_error = note or ""
        if suites:
            _web_mine_last_error = ""
        return suites[:max_suites]

    modes = []
    if _own_key_ready():
        modes.append(False)              # 自有密钥（默认）
    if getattr(config, "SMARTY_DEMO_MINE", True) and getattr(config, "SMARTY_DEMO_KEY", ""):
        modes.append(True)               # 官网 demo key（兜底）
    if not modes:
        _web_mine_last_error = "挖号通道已关闭（未配置 Smarty 密钥且 SMARTY_DEMO_MINE=0）"
        return []

    street, rest = _split_street_rest(address)
    out: list = []
    calls = 0
    err = ""
    try:
        for demo in modes:
            client = _probe_client(demo)
            try:
                for n in _MINE_ANCHORS:
                    if calls >= _MINE_MAX_CALLS or len(out) >= max_suites:
                        break
                    candidate = f"{street} Ste {n}{rest}"
                    calls += 1
                    dpv, err = _probe_dpv(client, candidate, demo)
                    if dpv == "Y":
                        out.append(candidate)
                    elif dpv:
                        time.sleep(0.12)
                    else:
                        break            # 本通道故障：别继续打了
            finally:
                try:
                    client.close()
                except Exception:
                    pass
            if out or err not in ("auth", "quota"):
                break                    # 有结果，或不是密钥问题 → 不用换通道
            if demo:
                break
            # 自有密钥 401/402 → 换 demo key 重来一遍
            calls = 0
            err = ""
    except Exception as e:
        _web_mine_last_error = f"{type(e).__name__}: {e}"
        return out[:max_suites]

    if out:
        _web_mine_last_error = ""
    elif err:
        _web_mine_last_error = _down_text(err, calls)
    else:
        _web_mine_last_error = (f"探测 {calls} 个楼层锚点均非 DPV=Y"
                                "（该楼 USPS 侧没有房号记录，或房号不是三位楼层号）")
    # 落缓存：命中的结果长期复用；"确认没有房号"也缓存（通道故障不缓存，下次还能重试）
    if out:
        store.mined_put(address, out, "")
    elif not channel_down(_web_mine_last_error) and "已关闭" not in _web_mine_last_error:
        store.mined_put(address, [], _web_mine_last_error)
    return out[:max_suites]
