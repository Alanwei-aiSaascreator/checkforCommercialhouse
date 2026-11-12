"""Playwright 浏览器共享层：持久化 Chrome 配置 + 截图 + 重试导航。

使用系统 Chrome（channel="chrome"）+ data/chrome_profile 持久化配置，
保留登录态/ Cookie，降低 LoopNet、OpenCorporates 的反爬拦截概率。
"""
import threading
import time

from playwright.sync_api import sync_playwright

from . import config

_pw = None
_context = None
_lock = threading.Lock()

VIEWPORT = {"width": 1440, "height": 900}


def _stop_pw():
    """停掉 Playwright 驱动（node 子进程），失败也不抛。"""
    global _pw
    if _pw is None:
        return
    try:
        _pw.stop()
    except Exception:
        pass
    _pw = None


def _launch(**extra):
    return _pw.chromium.launch_persistent_context(
        str(config.CHROME_PROFILE_DIR), **extra)


def _alive() -> bool:
    """探测连接是否存活。优先用 Playwright 内部的判死条件（_sync 同款）：
    事件循环已关闭或 dispatcher fiber 已死 → 连接必断。"""
    if _context is None:
        return False
    try:
        if _context._loop.is_closed() or _context._dispatcher_fiber.dead:
            return False
        return True
    except Exception:
        return False


def _reset():
    """丢弃失效的 context 与驱动，回到干净状态。"""
    global _context
    if _context is not None:
        try:
            _context.close()
        except Exception:
            pass
        _context = None
    _stop_pw()


def _ensure_context():
    global _pw, _context
    with _lock:
        if _context is not None and _alive():
            return _context
        # 连接失效或首次使用：清理残留（驱动/浏览器可能已死），重建
        _reset()
        _pw = sync_playwright().start()
        # 有头模式：LoopNet / 谷歌地图 / OpenCorporates 页面肉眼可见，方便人工盯流程
        # Translate* 必须关掉：Crexi 等站点会被 Chrome 自动翻译成中文，
        # 页面文案变成「每平方英尺/月 x 美元」，所有英文标签/价格正则都会失效。
        launch_kw = dict(headless=False, viewport=VIEWPORT, locale="en-US",
                         args=["--disable-blink-features=AutomationControlled",
                               "--lang=en-US",
                               "--disable-features=Translate,TranslateUI",
                               "--disable-translate"])
        proxy = {"server": config.PROXY_URL} if config.PROXY_URL else None
        if proxy:
            launch_kw["proxy"] = proxy
        try:
            try:
                # 优先系统 Chrome（保留真实指纹特征）
                _context = _launch(channel="chrome", **launch_kw)
            except Exception:
                # 未装 Chrome 时退回 Playwright 自带 Chromium
                _stop_pw()
                _pw = sync_playwright().start()
                _context = _launch(**launch_kw)
        except Exception:
            # 彻底失败：清理驱动，下次任务从干净状态重试
            _stop_pw()
            raise
        return _context


_TRANSLATE_HOSTS = ("translate.goog", "translate-pa.googleapis")


def block_translate(page) -> None:
    """拦截 Google 翻译请求，保证页面保持英文原文。

    为什么必须全局做：Crexi / OpenCorporates 都会按浏览器语言自动把页面翻成中文，
    翻完文案变成「每平方英尺每月 4 美元」「未找到任何公司」，
    所有英文正则（价格、Found N companies）全部失效（实测：查重步骤因此误判）。
    """
    def _handler(route):
        low = route.request.url.lower()
        if any(h in low for h in _TRANSLATE_HOSTS):
            return route.abort()
        return route.continue_()

    try:
        page.route("**/*", _handler)
    except Exception:
        pass


_exit_ip_cache = {"t": 0.0, "v": ""}


def exit_ip() -> str:
    """当前代理出口 IP（10 分钟缓存）。

    反爬排查用：LoopNet 的 Akamai 认的是出口 IP，机房/脏 IP 会整段被封，
    把 IP+运营商打进日志，用户才知道该换哪个节点（而不是反复重试同一节点）。
    """
    now = time.time()
    if _exit_ip_cache["v"] and now - _exit_ip_cache["t"] < 600:
        return _exit_ip_cache["v"]
    info = ""
    try:
        resp = _ensure_context().request.get("https://ipinfo.io/json", timeout=15000)
        if resp.status == 200:
            d = resp.json()
            seg = [str(d.get("ip") or "?")]
            org = d.get("org") or d.get("isp") or ""
            city = d.get("city") or ""
            if org:
                seg.append(org)
            if city:
                seg.append(city)
            info = " · ".join(seg)
    except Exception:
        info = ""
    if info:
        _exit_ip_cache.update(t=now, v=info)
    return info


def new_page():
    """新开一个标签页（调用方负责 close）。连接在检查后瞬断时重建并重试一次。"""
    try:
        page = _ensure_context().new_page()
    except Exception:
        _reset()
        page = _ensure_context().new_page()
    block_translate(page)
    return page


def safe_close(page) -> None:
    """关闭页面，失败不抛。连接已断时 finally 里裸 close() 会抛
    TargetClosedError，顶掉步骤原本的降级返回值、把整个任务带崩。"""
    try:
        page.close()
    except Exception:
        pass


def shot(page, tag: str) -> str:
    """整页截图到 data/screenshots/{tag}_{ms}.png，失败返回空串。
    毫秒时间戳：多朝向连拍时避免同秒同名互相覆盖。"""
    try:
        from pathlib import Path
        p = Path(config.SHOTS_DIR) / f"{tag}_{int(time.time() * 1000)}.png"
        page.screenshot(path=str(p), full_page=False, timeout=20000)
        return str(p)
    except Exception:
        return ""


def goto_retry(page, url: str, timeout: int = 45000, wait: int = 4000, retries: int = 2):
    """带重试的导航：反爬/网络抖动时多试几次，成功后静置 wait 毫秒等渲染。"""
    last_exc = None
    for _ in range(retries + 1):
        try:
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            page.wait_for_timeout(wait)
            return page
        except Exception as e:
            last_exc = e
            time.sleep(2)
    raise last_exc
