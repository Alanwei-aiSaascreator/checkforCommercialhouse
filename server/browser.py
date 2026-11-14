"""Playwright 浏览器共享层：持久化 Chrome 配置 + 截图 + 重试导航。

使用系统 Chrome（channel="chrome"）+ data/chrome_profile 持久化配置，
保留登录态/ Cookie，降低 LoopNet、OpenCorporates 的反爬拦截概率。
"""
import queue
import threading
import time

from playwright.sync_api import sync_playwright

from . import config

# 每个线程各自持有 Playwright 驱动与 context：
#   Playwright Sync API 的对象绑定创建它的线程，跨线程用会直接报错；
#   而 Chrome 同一个 user-data-dir 不能同时被两个实例打开（实测报
#   "Opening in existing browser session"）。所以：
#   - 首个建上下文的线程用主配置目录（保留 LoopNet/Crexi 登录态）；
#   - 其余线程（如并行的街景）用独立的 _aux 目录。
_tls = threading.local()
_lock = threading.Lock()
_aux = {"thread": None, "queue": None}

VIEWPORT = {"width": 1440, "height": 900}


def _state():
    if not hasattr(_tls, "pw"):
        _tls.pw = None
        _tls.context = None
    return _tls


def _profile_dir():
    """任务线程用主配置（保留 LoopNet/Crexi 登录态），辅助线程用 _aux。

    同一个 user-data-dir 不允许两个 Chrome 实例同时打开
    （实测报 "Opening in existing browser session"），所以必须分开目录。
    """
    if getattr(_tls, "role", "main") == "aux":
        return config.CHROME_PROFILE_DIR.parent / (config.CHROME_PROFILE_DIR.name + "_aux")
    return config.CHROME_PROFILE_DIR


def _stop_pw():
    """停掉本线程的 Playwright 驱动（node 子进程），失败也不抛。"""
    st = _state()
    if st.pw is None:
        return
    try:
        st.pw.stop()
    except Exception:
        pass
    st.pw = None


def _launch(profile_dir, **extra):
    return _state().pw.chromium.launch_persistent_context(str(profile_dir), **extra)


def _alive() -> bool:
    """探测本线程连接是否存活：事件循环已关闭或 dispatcher fiber 已死 → 必断。"""
    ctx = _state().context
    if ctx is None:
        return False
    try:
        if ctx._loop.is_closed() or ctx._dispatcher_fiber.dead:
            return False
        return True
    except Exception:
        return False


def _reset():
    """丢弃本线程失效的 context 与驱动，回到干净状态。

    必须连 st.extra（其它配置目录的 Chrome）一起清掉：pw.stop() 会连带杀掉
    这些浏览器，Python 侧的 ctx 对象就变成僵尸——再用它 new_page() 会抛
    "Event loop is closed! Is Playwright already stopped?"（实测任务中途崩过）。
    """
    st = _state()
    for ctx in list((getattr(st, "extra", {}) or {}).values()):
        try:
            ctx.close()
        except Exception:
            pass
    st.extra = {}
    if st.context is not None:
        try:
            st.context.close()
        except Exception:
            pass
        st.context = None
    _stop_pw()


def _launch_kw(background=False, **extra):
    """统一的启动参数（有头 + 关自动翻译 + 关自动化标记）。

    采集任务默认把 Chrome 以最小化窗口启动：保留真实浏览器、Cookie、截图和
    登录能力，但不会反复弹窗遮挡用户。人工登录工具不传 background，仍会正常显示窗口。

    Translate* 必须关掉：Crexi / CityFeet 等站会被 Chrome 自动翻译成中文，
    页面文案变成「每平方英尺/月 x 美元」，所有英文标签/价格正则都会失效。
    """
    args = ["--disable-blink-features=AutomationControlled",
            "--lang=en-US",
            "--disable-features=Translate,TranslateUI",
            "--disable-translate"]
    if background:
        args.append("--start-minimized")
    kw = dict(headless=False, viewport=VIEWPORT, locale="en-US", args=args)
    if config.PROXY_URL:
        kw["proxy"] = {"server": config.PROXY_URL}
    kw.update(extra)
    return kw


def new_page_in(profile_name: str):
    """在**指定配置目录**里开一个标签页（同线程可并存多个 Chrome 进程）。

    为什么需要：主配置 data/chrome_profile 带着 Crexi 登录态，但也因此被
    Akamai 记住（实测 CityFeet 对主配置直接返回 Access Denied，而全新配置
    正常 200）。所以免登录的数据源用独立配置（chrome_profile_feeds）。
    同一 user-data-dir 不能被两个 Chrome 打开，因此每个配置目录单独一套。
    """
    st = _state()
    if st.pw is None:
        st.pw = sync_playwright().start()
    extra = getattr(st, "extra", None)
    if extra is None:
        extra = st.extra = {}
    ctx = extra.pop(profile_name, None)
    if ctx is not None:
        try:
            ctx.pages                  # 连接已断会抛
            page = ctx.new_page()      # 驱动被 stop 会抛 Event loop is closed
            block_translate(page)
            extra[profile_name] = ctx
            return page
        except Exception:
            try:
                ctx.close()
            except Exception:
                pass
    # 走到这里：没有缓存 / 缓存已死 → 重建（驱动也可能已被 _reset 停掉）
    d = config.CHROME_PROFILE_DIR.parent / profile_name
    last = None
    for attempt in range(2):
        if st.pw is None:
            st.pw = sync_playwright().start()
        try:
            try:
                ctx = st.pw.chromium.launch_persistent_context(
                    str(d), channel="chrome", **_launch_kw(background=True))
            except Exception:
                ctx = st.pw.chromium.launch_persistent_context(str(d), **_launch_kw(background=True))
            page = ctx.new_page()
            extra[profile_name] = ctx
            block_translate(page)
            return page
        except Exception as e:
            last = e
            _stop_pw()                 # 驱动大概率已死，重启后再试一次
    raise last


def _ensure_context():
    """本线程独享 st（threading.local），所以这里不加锁——
    启动 Chrome 要数秒，持锁会与 _profile_dir 互相等待（曾因此死锁卡死任务）。"""
    st = _state()
    if st.context is not None and _alive():
        return st.context
    # 只有「旧连接确实失效」才整体重置。首次创建主 context 时不能调 _reset()：
    # 那时 pw 往往已被 new_page_in() 启动（feeds 的 Chrome 正跑着），
    # pw.stop() 会把它一起杀掉，后续再开页就抛 "Event loop is closed"（实测任务中途崩）
    if st.context is not None:
        _reset()
    profile_dir = _profile_dir()
    if st.pw is None:
        st.pw = sync_playwright().start()
    launch_kw = _launch_kw(background=True)
    try:
        try:
            # 优先系统 Chrome（保留真实指纹特征）
            st.context = _launch(profile_dir, channel="chrome", **launch_kw)
        except Exception:
            # 未装 Chrome 时退回 Playwright 自带 Chromium
            _stop_pw()
            st.pw = sync_playwright().start()
            st.context = _launch(profile_dir, **launch_kw)
    except Exception:
        # 彻底失败：清理驱动，下次任务从干净状态重试
        _stop_pw()
        raise
    return st.context


def close_current():
    """关闭当前线程的 context 与驱动（任务结束时调用，避免残留 Chrome 占住配置目录）。"""
    st = _state()
    for ctx in list((getattr(st, "extra", {}) or {}).values()):
        try:
            ctx.close()
        except Exception:
            pass
    if getattr(st, "extra", None):
        st.extra = {}
    _reset()


def _aux_loop(q):
    """常驻辅助线程：独占一个 Chrome（_aux 配置目录），专供并行步骤使用。"""
    _tls.role = "aux"
    while True:
        job = q.get()
        if job is None:
            break
        fn, box, ev = job
        try:
            box["ok"] = fn()
        except Exception as e:
            box["err"] = e
        finally:
            ev.set()


def _aux_queue():
    with _lock:
        t = _aux["thread"]
        if t is None or not t.is_alive():
            q = queue.Queue()
            t = threading.Thread(target=_aux_loop, args=(q,), daemon=True,
                                 name="browser-aux")
            t.start()
            _aux["thread"], _aux["queue"] = t, q
        return _aux["queue"]


class _Job:
    def __init__(self, box, ev):
        self._box, self._ev = box, ev

    def result(self, timeout=None):
        if not self._ev.wait(timeout):
            raise TimeoutError("辅助浏览器线程执行超时")
        if "err" in self._box:
            raise self._box["err"]
        return self._box.get("ok")


def submit(fn) -> _Job:
    """把 fn 丢给常驻辅助线程执行（独立 Chrome/配置目录），立即返回 Job。

    为什么不用普通 Thread：Playwright Sync API 的对象绑定创建它的线程，
    每开一个新线程就要重启一次 Chrome（数秒），且并发时配置目录容易冲突；
    常驻线程只启动一次，后续复用。
    """
    q = _aux_queue()
    box, ev = {}, threading.Event()
    q.put((fn, box, ev))
    return _Job(box, ev)


def run_async(fn, timeout=None):
    """同步版：在辅助线程执行并直接返回结果。"""
    return submit(fn).result(timeout)


_TRANSLATE_HOSTS = ("translate.goog", "translate-pa.googleapis")


def block_translate(page) -> None:
    """拦截 Google 翻译请求，保证页面保持英文原文。

    为什么必须全局做：Crexi / OpenCorporates 都会按浏览器语言自动把页面翻成中文，
    翻完文案变成「每平方英尺每月 4 美元」「未找到任何公司」，
    所有英文正则（价格、Found N companies）全部失效（实测：查重步骤因此误判）。

    注意：**只能用域名窄匹配，不能用 page.route("**/*")**。全局 route 会让所有请求
    都走 Playwright 的 Fetch 拦截层，跨源资源（CBRE 的 mediaassets.cbre.com 上的
    CSS/主 JS）会被浏览器按 CORP 重新校验并拒收，报
    "ERR_BLOCKED_BY_RESPONSE.NotSameOrigin" —— 页面随即退化成只有文字的裸 HTML，
    SPA 不渲染、卡片和详情链接全都没有（实测 CBRE 列表页首次加载即如此）。
    """
    def _handler(route):
        try:
            route.abort()
        except Exception:
            pass

    for pat in ("**/*translate.goog/**", "**/translate-pa.googleapis.com/**",
                "**/translate.googleapis.com/**", "**/translate.google.com/**"):
        try:
            page.route(pat, _handler)
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
