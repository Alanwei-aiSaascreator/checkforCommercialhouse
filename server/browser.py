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
        launch_kw = dict(headless=False, viewport=VIEWPORT,
                         args=["--disable-blink-features=AutomationControlled",
                               "--lang=en-US"])
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


def new_page():
    """新开一个标签页（调用方负责 close）。连接在检查后瞬断时重建并重试一次。"""
    try:
        return _ensure_context().new_page()
    except Exception:
        _reset()
        return _ensure_context().new_page()


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
