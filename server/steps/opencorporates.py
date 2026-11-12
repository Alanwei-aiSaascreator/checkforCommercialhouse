"""步骤4：OpenCorporates 同址公司查重（阈值 ≤5 家通过）。

走真人路径：进首页 → 在搜索框里逐字键盘输入地址 → 回车提交 → 等结果页渲染 → 截图 + 读公司数。
（不再直接拼 ?q= 深链导航：深链没有首页 Referer / 表单提交指纹，容易被反爬拦下）

message 示例（与既有档案一致）：Found 0 companies ≤ 5，查重通过
"""
import random
import re

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


def _read_count(page, timeout_ms: int = 45000):
    """等结果页出数：返回 (公司数 or None, 正文)。轮询期间容忍反爬过渡页。"""
    body = ""
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(2000)
        waited += 2000
        try:
            text = page.evaluate("() => document.body.innerText") or ""
            url = page.url
        except Exception:
            continue
        if text:
            body = text
        if "/companies?" not in url:
            continue
        m = re.search(r"([\d,]+)\s+compan", text.lower())
        if m:
            return int(m.group(1).replace(",", "")), text
        # 0 结果时页面不一定带数字（英文 "No companies found" / 被翻译成中文
        # 「未找到任何公司」），直接判定 0 家，别退化成"无法解析"
        if re.search(r"\bno\s+companies?\b|未找到任何公司|没有找到|未找到", text, re.I):
            return 0, text
        # 兜底：结果页没出汇总句时数结果条目
        try:
            n = page.locator("li.result, ul.results li, div.search-result").count()
        except Exception:
            n = 0
        if n:
            return n, text
    return None, body


def check_duplicates(address: str, tag: str = "oc") -> dict:
    q = _strip_unit(address)
    page = browser.new_page()
    shot = ""
    try:
        # 真人路径：进首页 → 键盘输入 → 回车
        browser.goto_retry(page, _HOME_URL, timeout=45000, wait=2500)
        _wait_human_gate(page)
        if not _submit_by_typing(page, q):
            shot = browser.shot(page, f"{tag}_opencorp")
            return {"status": "suspect", "screenshot": shot or "",
                    "message": "OpenCorporates 首页搜索框未能提交查询（页面未就绪）"}

        n, body = _read_count(page)
        shot = browser.shot(page, f"{tag}_opencorp")
        low = body.lower()
        if n is None:
            if any(w in low for w in _CAPTCHA_WORDS):
                return {"status": "suspect", "screenshot": shot or "",
                        "message": "OpenCorporates 触发反爬验证，结果无法解析"}
            return {"status": "suspect", "screenshot": shot or "",
                    "message": "OpenCorporates 结果页未解析到注册公司数"}
    except Exception as e:
        try:
            shot = shot or browser.shot(page, f"{tag}_opencorp")
        except Exception:
            shot = ""
        return {"status": "suspect", "screenshot": shot or "",
                "message": f"OpenCorporates 访问失败: {e}"}
    finally:
        browser.safe_close(page)

    limit = config.OPENCORPORATES_LIMIT
    if n <= limit:
        return {"status": "pass", "screenshot": shot or "",
                "message": f"Found {n} companies ≤ {limit}，查重通过"}
    return {"status": "fail", "screenshot": shot or "",
            "message": f"Found {n} companies > {limit}，同址注册公司过多"}
