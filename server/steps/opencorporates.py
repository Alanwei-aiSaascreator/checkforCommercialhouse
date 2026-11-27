"""步骤4：OpenCorporates 同址公司查重（阈值 ≤5 家通过）。

走真人路径：进首页 → 在搜索框里逐字键盘输入地址 → 回车提交 → 等结果页渲染 → 截图 + 读公司数。
（不再直接拼 ?q= 深链导航：深链没有首页 Referer / 表单提交指纹，容易被反爬拦下）

message 示例（与既有档案一致）：Found 0 companies ≤ 5，查重通过

2026-11 修复（另一台设备跑超级轮次时成片报「结果页未解析到注册公司数」）：
  1. 多工人（4~6 个 Chrome）同时打 OpenCorporates 会被 Cloudflare 限流，结果页永远
     停在「正在验证」而不是给出公司数 —— 现在全局串行 + 两次查询最小间隔 OC_MIN_GAP；
  2. 校验页不再当成"解析失败"：在 OC_READ_TIMEOUT 内继续等它自动放行；
  3. 公司数解析兼容中英文/本地化文案（Found N companies / 找到 N 家公司 / N results）；
  4. 解析不到时重试一次，message 带上 url/title/正文片段，便于定位是封 IP 还是改版。
"""
import json
import random
import re
import threading
import time
from urllib.parse import quote_plus

from .. import browser, config

_STRIP_RE = re.compile(r"\b(suite|ste|room|rm|unit|#)\s*[\w-]+", re.I)

_HOME_URL = "https://opencorporates.com/"

# 首页公司搜索框（实测命中第一个：class="oc-home-search_input js-oc-search-input" name="q"）
_SEARCH_INPUTS = (
    "input.oc-home-search_input[name='q']",
    "#ocSearchBox input[name='q']",
    "form.js-oc-search-form input[name='q']",
    "input[name='q']",
)
_SUBMIT_BUTTONS = (
    "#ocSearchBox button[type='submit']",
    "form.js-oc-search-form button[type='submit']",
    "button.oc-home-search_button",
)
_CAPTCHA_WORDS = ("captcha", "are you a robot", "access denied")
_GATE_TITLES = ("just a moment", "checking your browser", "attention required")

# 结果页的公司数文案：英文原文、中文翻译、纯 "N results" 都认
_COUNT_PATTERNS = (
    re.compile(r"([\d,]+)\s+compan", re.I),                   # Found 12 companies
    re.compile(r"([\d,]+)\s*家\s*公司"),                       # 找到 12 家公司
    re.compile(r"共?\s*(?:找到|搜索到|有)\s*([\d,]+)\s*家"),     # 共找到 12 家
    re.compile(r"([\d,]+)\s+results?\b", re.I),               # 12 results
    re.compile(r"of\s+([\d,]+)\s+compan", re.I),              # 1-30 of 1234 companies
)
# 0 结果时页面不一定带数字（"No companies found" / 「未找到任何公司」）
_ZERO_PATTERNS = (
    re.compile(r"\bno\s+companies?\b", re.I),
    re.compile(r"did not match any compan", re.I),
    re.compile(r"未找到任何公司|没有找到任何公司|未匹配任何公司|没有匹配的公司|找不到任何公司"),
)
# Cloudflare 人机校验页：不是解析失败，继续等它放行
_CHALLENGE_WORDS = (
    "verifying you are human", "checking if the site connection is secure",
    "checking your browser", "just a moment", "enable javascript and cookies",
    "ddos protection", "请稍候", "正在验证", "安全检查", "验证您是人类", "请完成安全验证",
)
# OpenCorporates 自己的「Verify your browser」闸门页（不是 Cloudflare）：
#   "We couldn't confirm your browser ... Your browser hasn't been verified yet,
#    so this search wasn't run." + 一个 "Verify and continue" 链接（href 带 oc_verify=1）
# 点这个链接即种下验证 cookie 并继续本次搜索，不需要输验证码。
_VERIFY_HINTS = ("couldn't confirm your browser", "hasn't been verified yet",
                 "verify your browser", "verify and continue")

# OpenCorporates 全局串行：同一时刻只允许一个查询在跑，并保证最小间隔
_OC_RUN_LOCK = threading.Lock()
_OC_LAST = [0.0]

# 熔断（详见 config.OC_BREAKER_*）：站点/IP 被彻底封掉时，连续失败 N 次就进入
# 冷却期，期间查重直接返回 suspect，不再白跑；冷却到期自动恢复。
# 字典用 list 包一层以便无锁读快照；_breaker_lock 保护计数与状态翻转。
_breaker_lock = threading.Lock()
_breaker = {"fails": 0, "until": 0.0, "opened_at": 0.0, "cooldown_s": 0.0}


def breaker_status() -> dict:
    """给前端 /api/status 展示：是否冷却中、还剩几秒、已连续失败几次。"""
    if not int(getattr(config, "OC_BREAKER_FAILS", 8) or 0):
        return {"cooling": False, "cooldown_left": 0, "cooldown_s": 0,
                "consecutive_fails": 0, "threshold": 0}
    with _breaker_lock:
        b = dict(_breaker)
    remain = b["until"] - time.time()
    return {"cooling": remain > 0, "cooldown_left": max(0, int(remain)),
            "cooldown_s": int(b.get("cooldown_s") or 0),
            "consecutive_fails": b["fails"],
            "threshold": int(getattr(config, "OC_BREAKER_FAILS", 8) or 8)}


def _breaker_check() -> str:
    """进入 check_duplicates 时先看：冷却中返回提示文案，正常返回空串。

    注意这里**不持 _OC_RUN_LOCK**——冷却判断不能排队等前一个查询跑完，
    否则 6 个工人会把熔断空转拖成串行长队。
    """
    if not int(getattr(config, "OC_BREAKER_FAILS", 8) or 0):
        return ""
    with _breaker_lock:
        b = dict(_breaker)
    remain = b["until"] - time.time()
    if remain <= 0:
        return ""
    return (f"OpenCorporates 连续失败 {b['fails']} 次已熔断，"
            f"冷却中还剩 {int(remain)} 秒（到期自动恢复）")


def _breaker_on_success() -> None:
    """成功拿到公司数：网络/站点是好的，清零连续失败计数。"""
    if not int(getattr(config, "OC_BREAKER_FAILS", 8) or 0):
        return
    with _breaker_lock:
        _breaker["fails"] = 0


def _breaker_on_fail(msg: str = "") -> None:
    """本次查重彻底失败（重试过仍拿不到数）：累计；达到阈值触发熔断冷却。

    只告警一次（熔断刚打开的那一刻），之后冷却期内的失败不再刷屏。
    冷却到期后首个请求会真的去探路，成功则清零，失败则立即重新熔断。
    """
    threshold = int(getattr(config, "OC_BREAKER_FAILS", 8) or 0)
    if threshold <= 0:
        return
    cooldown = float(getattr(config, "OC_BREAKER_COOLDOWN", 900) or 900)
    with _breaker_lock:
        prev = dict(_breaker)
        if prev["until"] - time.time() > 0:
            _breaker["fails"] = prev["fails"] + 1   # 冷却期内只涨计数（供观测）
            return
        fails = prev["fails"] + 1
        if fails < threshold:
            _breaker["fails"] = fails
            return
        _breaker.update({"fails": fails, "opened_at": time.time(),
                         "until": time.time() + cooldown,
                         "cooldown_s": cooldown})
    print(f"[OC熔断] 连续失败 {fails} 次（{msg[:60]}），暂停查重 {int(cooldown)} 秒后自动恢复",
          flush=True)


def _throttle() -> None:
    """两次查重之间至少间隔 OC_MIN_GAP 秒（必须在持 _OC_RUN_LOCK 时调用）。"""
    gap = float(getattr(config, "OC_MIN_GAP", 3) or 0)
    if gap <= 0:
        return
    wait = gap - (time.time() - _OC_LAST[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0, 0.8))
    _OC_LAST[0] = time.time()


def _strip_unit(address: str) -> str:
    return _STRIP_RE.sub(" ", address).strip(" ,")


def _wait_human_gate(page, timeout_ms: int = 30000) -> None:
    """等 Cloudflare/reCAPTCHA 过渡页自己放行（标题恢复成正常页面即可）。"""
    waited = 0
    while waited < timeout_ms:
        try:
            title = (page.title() or "").lower()
        except Exception:
            title = ""
        if not any(g in title for g in _GATE_TITLES):
            return
        page.wait_for_timeout(1500)
        waited += 1500


def _find_search_box(page, timeout_ms: int = 15000):
    for sel in _SEARCH_INPUTS:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:
            continue
    return None


def _submit_by_typing(page, query: str) -> bool:
    """首页 → 搜索框键盘输入 → 回车。成功离开首页(进 /companies 结果页)返回 True。"""
    box = _find_search_box(page)
    if box is None:
        # 首页未渲染出搜索框：重进一次（首次导航可能撞上过渡页/半截渲染）
        browser.goto_retry(page, _HOME_URL, timeout=45000, wait=3000)
        _wait_human_gate(page)
        box = _find_search_box(page, timeout_ms=20000)
        if box is None:
            return False

    # 逐字输入：click → 随机间隔敲键，模仿真人节奏
    try:
        box.click(timeout=8000)
    except Exception:
        try:
            box.click(force=True, timeout=5000)
        except Exception:
            try:
                box.focus()
            except Exception:
                return False
    page.wait_for_timeout(random.randint(300, 700))
    box.press_sequentially(query, delay=random.randint(70, 130))
    page.wait_for_timeout(random.randint(400, 900))
    try:
        if not (box.input_value() or "").strip():
            return False
    except Exception:
        pass

    box.press("Enter")
    try:
        page.wait_for_url(lambda u: "/companies?" in u, timeout=25000)
        return True
    except Exception:
        pass

    # 回车没提交（被自动补全吞掉/表单未响应）：退回点搜索按钮再试一次
    for sel in _SUBMIT_BUTTONS:
        try:
            page.locator(sel).first.click(timeout=4000)
            break
        except Exception:
            continue
    try:
        page.wait_for_url(lambda u: "/companies?" in u, timeout=15000)
        return True
    except Exception:
        return False


def _parse_count(text: str):
    """从结果页正文解析公司数；0 家返回 0；解析不出返回 None。"""
    for pat in _COUNT_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except (ValueError, IndexError):
                continue
    for pat in _ZERO_PATTERNS:
        if pat.search(text):
            return 0
    return None


def _pass_verify_page(page) -> bool:
    """点掉 OpenCorporates 的「Verify and continue」（href 带 oc_verify=1）。

    这一步等价于点页面上的那个链接：验证通过后 cookie 写进当前 Chrome 配置目录，
    同一 profile 后续搜索不再被拦。点不到就直接带参数导航过去。
    """
    try:
        link = page.locator("a[href*='oc_verify']").first
        if link.count() > 0:
            link.click(timeout=6000)
            return True
    except Exception:
        pass
    try:
        url = page.url or ""
        if "oc_verify=" in url:
            return False
        sep = "&" if "?" in url else "?"
        page.goto(f"{url}{sep}oc_verify=1", wait_until="domcontentloaded", timeout=45000)
        return True
    except Exception:
        return False


def _read_count(page, timeout_ms: int = 0):
    """等结果页出数：返回 (公司数 or None, 正文, 诊断串)。

    轮询期间遇到的都不是"解析失败"：
      - Cloudflare 过渡页：等它自己放行；
      - OpenCorporates「Verify your browser」闸门页：点 Verify and continue 继续。
    """
    if not timeout_ms:
        timeout_ms = int(float(getattr(config, "OC_READ_TIMEOUT", 60) or 60) * 1000)
    body = ""
    diag = ""
    waited = 0
    verified = 0
    while waited < timeout_ms:
        page.wait_for_timeout(2000)
        waited += 2000
        try:
            text = page.evaluate("() => document.body.innerText") or ""
            url = page.url
            title = page.title() or ""
        except Exception:
            continue
        if text:
            body = text
        low = text.lower()
        # OpenCorporates 自己的浏览器验证闸门（不点会一直停在这页）
        if any(w in low for w in _VERIFY_HINTS) or "verify your browser" in (title or "").lower():
            if verified < 3 and _pass_verify_page(page):
                verified += 1
                diag = "已点击 Verify and continue"
            else:
                diag = f"浏览器验证页未放行（title={title[:30]}）"
            continue
        if any(w in low for w in _CHALLENGE_WORDS) or "just a moment" in (title or "").lower():
            diag = f"安全校验页未放行（title={title[:30]}）"
            continue
        n = _parse_count(text)
        if n is not None:
            return n, text, ""
        # 兜底：结果页没出汇总句时数结果条目
        try:
            cnt = page.locator("li.result, ul.results li, div.search-result").count()
        except Exception:
            cnt = 0
        if cnt:
            return cnt, text, ""
        diag = (f"url={url[:70]} | title={title[:40]} | "
                f"正文={' '.join(text.split())[:70]}")
    return None, body, diag


_SEARCH_URL = ("https://opencorporates.com/companies?q={q}&type=companies"
               "&utf8=%E2%9C%93")

# ---- 同址查重缓存 ----------------------------------------------------------
# 同一楼栋常有多个套间（同一 address 只差 suite #），实际只需去 OC 查一次。
# 缓存 7 天：公司注册状态变化很慢，重复查询只会徒增被限流的概率。
_CACHE_FILE = config.DATA_DIR / "oc_cache.json"
_CACHE_TTL = 7 * 24 * 3600
_cache_lock = threading.Lock()
_cache_mem = None


def _cache_key(address: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (address or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _cache_all() -> dict:
    global _cache_mem
    if _cache_mem is None:
        try:
            _cache_mem = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _cache_mem = {}
        if not isinstance(_cache_mem, dict):
            _cache_mem = {}
    return _cache_mem


def _cache_get(key: str):
    with _cache_lock:
        item = _cache_all().get(key) or {}
        try:
            n, ts = int(item.get("n")), float(item.get("ts") or 0)
        except (TypeError, ValueError):
            return None
    if time.time() - ts > _CACHE_TTL:
        return None
    return n


def _cache_put(key: str, n: int) -> None:
    with _cache_lock:
        d = _cache_all()
        d[key] = {"n": int(n), "ts": time.time()}
        try:
            tmp = _CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_CACHE_FILE)
        except Exception:
            pass


def _query_once(q: str, tag: str):
    """跑一次搜索并读结果。返回 (公司数 or None, 截图, 失败原因)。

    先走结果页深链（省掉首页加载 ~8 秒）；深链被拦或解析不到时，
    退回「首页 → 键盘输入 → 回车」的真人路径再试一次。
    """
    page = browser.new_page()
    shot = ""
    try:
        deep_ok = False
        try:
            page.goto(_SEARCH_URL.format(q=quote_plus(q)),
                      wait_until="domcontentloaded", timeout=45000)
            deep_ok = True
        except Exception:
            deep_ok = False

        if not deep_ok:
            # 深链连不上：走真人路径
            browser.goto_retry(page, _HOME_URL, timeout=45000, wait=2500)
            _wait_human_gate(page)
            if not _submit_by_typing(page, q):
                shot = browser.shot(page, f"{tag}_opencorp")
                return None, shot or "", "OpenCorporates 未能提交查询（页面未就绪）"

        n, body, diag = _read_count(page)

        if n is None and deep_ok:
            # 深链被反爬闸门拦下 → 退回首页真人输入，再给一次机会
            try:
                browser.goto_retry(page, _HOME_URL, timeout=45000, wait=2000)
                _wait_human_gate(page)
                if _submit_by_typing(page, q):
                    n2, body2, diag2 = _read_count(page)
                    if n2 is not None:
                        n, body, diag = n2, body2, diag2
                    else:
                        body, diag = body2 or body, diag2 or diag
            except Exception:
                pass

        shot = browser.shot(page, f"{tag}_opencorp")
        if n is None:
            low = (body or "").lower()
            if any(w in low for w in _CAPTCHA_WORDS):
                return None, shot or "", "OpenCorporates 触发反爬验证，结果无法解析"
            if "安全校验页" in diag:
                return None, shot or "", "OpenCorporates 一直停在人机校验页（换代理节点或稍后再试）"
            return None, shot or "", f"OpenCorporates 结果页未解析到注册公司数（{diag[:90]}）"
        return n, shot or "", ""
    except Exception as e:
        try:
            shot = shot or browser.shot(page, f"{tag}_opencorp")
        except Exception:
            shot = ""
        return None, shot or "", f"OpenCorporates 访问失败: {e}"
    finally:
        browser.safe_close(page)


def check_duplicates(address: str, tag: str = "oc") -> dict:
    q = _strip_unit(address)
    limit = config.OPENCORPORATES_LIMIT

    # 同址命中缓存：直接出结论（同一楼栋的不同套间只查一次）
    key = _cache_key(q)
    hit = _cache_get(key)
    if hit is not None:
        if hit <= limit:
            return {"status": "pass", "screenshot": "",
                    "message": f"Found {hit} companies ≤ {limit}，查重通过（同址缓存）"}
        return {"status": "fail", "screenshot": "",
                "message": f"Found {hit} companies > {limit}，同址注册公司过多（同址缓存）"}

    # 熔断冷却中：不再真的去打站点（也省下 Smarty 等上游步骤的无效消耗）
    cooling = _breaker_check()
    if cooling:
        return {"status": "suspect", "screenshot": "", "message": cooling}

    n, shot, msg = None, "", ""
    # 全局串行（见文件头：多工人并发会被 Cloudflare 打成"结果页不出数"）
    with _OC_RUN_LOCK:
        for attempt in (1, 2):
            _throttle()
            n, got_shot, msg = _query_once(q, tag if attempt == 1 else f"{tag}b")
            shot = got_shot or shot
            if n is not None:
                break
            if attempt == 1:
                time.sleep(random.uniform(2.0, 4.0))

    if n is None:
        _breaker_on_fail(msg)
        return {"status": "suspect", "screenshot": shot or "",
                "message": msg or "OpenCorporates 结果页未解析到注册公司数"}

    _breaker_on_success()
    _cache_put(key, n)
    if n <= limit:
        return {"status": "pass", "screenshot": shot or "",
                "message": f"Found {n} companies ≤ {limit}，查重通过"}
    return {"status": "fail", "screenshot": shot or "",
            "message": f"Found {n} companies > {limit}，同址注册公司过多"}
