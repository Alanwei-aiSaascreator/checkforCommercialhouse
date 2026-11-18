"""Playwright 浏览器共享层：持久化 Chrome 配置 + 截图 + 重试导航。

使用系统 Chrome（channel="chrome"）+ data/chrome_profile 持久化配置，
保留登录态/ Cookie，降低 LoopNet、OpenCorporates 的反爬拦截概率。
"""
import ctypes
import queue
import subprocess
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
_aux = {"lanes": {}}    # 工人池：每个工人命名空间一条独立辅助线程（lane → (thread, queue)）

VIEWPORT = {"width": 1440, "height": 900}


def _minimize_chrome_window(pids):
    """强制将 Playwright 启动的 Chrome 窗口移出屏幕并最小化（不抢焦点）。

    pids：属于本程序启动的 Chrome 进程号集合（见 _profile_pids）。

    关键点：
    * 先 SetWindowPos 移到屏幕外（SWP_NOACTIVATE），窗口再出现也不会盖住
      用户正在操作的内容；
    * 用 SW_SHOWMINNOACTIVE(7) 而不是 SW_MINIMIZE(6)：后者会激活窗口、
      把键盘焦点抢走，正是"任务跑起来就抢我鼠标键盘"的元凶。
    """
    if not hasattr(ctypes, "windll") or not pids:
        return
    user32 = ctypes.windll.user32
    hwnds = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _):
        found_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
        # 只处理本程序启动的 Chrome 进程；禁止"找不到 PID 就最小化所有
        # Chrome 窗口"的兜底——那会把用户自己开着的浏览器一并压下去。
        if found_pid.value in pids and user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    SWP_NOACTIVATE, SWP_NOZORDER, SWP_NOSIZE = 0x0010, 0x0004, 0x0001
    HWND_BOTTOM = 1
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080    # 工具窗口：不出现在任务栏
    WS_EX_NOACTIVATE = 0x08000000    # 不接收键盘焦点（点它也不抢）
    for hwnd in hwnds:
        # 设为工具窗口 + 不激活窗口：从任务栏图标里消失，且系统层面
        # 永远不把键盘焦点交给它（用户要求：全程不碰我的鼠标键盘）
        try:
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        except Exception:
            pass
        # 无激活移出屏幕（不改变 Z 序、不抢焦点）
        try:
            user32.SetWindowPos(hwnd, HWND_BOTTOM, -32000, -32000, 0, 0,
                                SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOSIZE)
        except Exception:
            pass
        # 无激活最小化（SW_SHOWMINNOACTIVE）
        user32.ShowWindow(hwnd, 7)


def _kill_stale_profile_chrome(profile_dir: str) -> None:
    """杀掉仍占用该配置目录的残留 Chrome/Chromium 进程。

    场景：上次任务异常终止后，Chrome 进程还活着并锁着 user-data-dir，
    下次启动报 "Opening in existing browser session"。只按命令行里的
    配置目录路径精确匹配，不影响用户自己开着的浏览器。"""
    if not hasattr(subprocess, "run"):
        return
    d = str(profile_dir).replace("'", "''")
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{d}*' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=25)
        time.sleep(1.5)          # 等文件锁释放
    except Exception:
        pass


def _is_profile_locked_error(e: Exception) -> bool:
    s = str(e)
    return "Opening in existing browser session" in s or "user data directory is already in use" in s


def _profile_pids(profile_dir: str) -> set:
    """按命令行里的 --user-data-dir 找出本配置目录的 Chrome 进程号。

    Playwright 的 Browser 对象不暴露 pid（实测 context.browser.pid 直接
    AttributeError，异常被吞后 pid 恒为 None，看护线程一直在空转）——
    所以只能从进程命令行反查：Chrome 主进程必带 --user-data-dir 参数。
    每个 context 只查一次（结果缓存在 context._our_pids 上）。
    """
    d = str(profile_dir)
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{d}*' }} | "
          "Select-Object -ExpandProperty ProcessId")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=30).stdout
        return {int(x) for x in out.split() if x.strip().isdigit()}
    except Exception:
        return set()


# ---------- 全局常驻看护 ----------
# 单次 context 只看护 ~20s：Aux 道浏览器可能在任务开始几分钟后才首次启动、
# Chrome 崩溃后自愈重启、页内弹窗晚出现——这些"晚到的窗口"没人压，实测
# 仍会抢焦点。改为：登记所有我们用过的配置目录，常驻线程 2s 巡检一次，
# 窗口出现最多 2 秒内必被压住（工具窗口 + 不激活 + 屏幕外最小化）。
_profile_registry: set = set()
_watchdog_lock = threading.Lock()
_watchdog_started = False


def _note_profile(profile_dir) -> None:
    global _watchdog_started
    with _watchdog_lock:
        _profile_registry.add(str(profile_dir))
        if not _watchdog_started:
            _watchdog_started = True
            threading.Thread(target=_watchdog_loop, daemon=True,
                             name="chrome-focus-watchdog").start()


def _watchdog_loop():
    cache: dict = {}          # dir → (取样时间, pids)
    while True:
        now = time.time()
        dirs = list(_profile_registry)
        # 进程号每 20s 刷新一次（PowerShell 查询较贵），窗口压制每轮都做
        stale = [d for d in dirs
                 if d not in cache or now - cache[d][0] > 20]
        for d in stale:
            cache[d] = (now, _profile_pids(d))
        for d in dirs:
            pids = cache.get(d, (0, set()))[1]
            if pids:
                try:
                    _minimize_chrome_window(pids)
                except Exception:
                    pass
        time.sleep(2)


def _minimize_context(context, profile_dir=None):
    """在启动、开页和导航后持续压制 Chrome 抢前台。

    Chrome 新开窗口/弹窗时会把自己提到最前并抢走键盘焦点；启动后前 20 秒
    最容易反复发生（步骤切换、辅助浏览器弹出）。这里用看护线程在窗口出现
    的第一时间无激活移出屏幕并最小化 + 设为工具窗口，全程不碰用户的鼠标、
    焦点和任务栏。同时把配置目录登记进全局常驻看护（_watchdog_loop），
    20 秒之外的任何新窗口（Aux 道晚启动、Chrome 崩溃重启、歇了的弹窗）
    也会被 2 秒一次的巡检压住——实测只靠单次 20s 看护仍会漏抢焦点。
    """
    pids = getattr(context, "_our_pids", None)
    if not pids and profile_dir:
        pids = _profile_pids(str(profile_dir))
        try:
            context._our_pids = pids
        except Exception:
            pass
    if profile_dir:
        _note_profile(profile_dir)

    def minimize_later():
        # 指数退避看护 ~20s：前 1 秒内 0.05/0.15/0.4 三连压（Chrome 首帧和
        # 各类气泡都在这窗口里冒头），再 0.1/0.3/0.6/1/1.5/2/3/4/6/8/10
        for delay in (0.05, 0.15, 0.4, 0.1, 0.3, 0.6, 1.0, 1.5, 2.0, 3.0,
                      4.0, 6.0, 8.0, 10.0):
            time.sleep(delay)
            try:
                _minimize_chrome_window(pids)
            except Exception:
                pass

    threading.Thread(target=minimize_later, daemon=True).start()
    # 弹出的新标签/新窗口也立即压住（popup 会开新 OS 窗口并抢前台）。
    # 每个 context 只注册一次，避免 new_page 反复叠加监听。
    if not getattr(context, "_popup_hook", False):
        try:
            context._popup_hook = True
            context.on("page", lambda _p: _minimize_chrome_window(pids))
        except Exception:
            pass


def _state():
    if not hasattr(_tls, "pw"):
        _tls.pw = None
        _tls.context = None
    return _tls


def _profile_dir():
    """按线程命名空间取配置目录：主配置保留 LoopNet/Crexi 登录态，
    辅助线程用 _aux，工人池每个工人再用 _wN 后缀隔离。

    同一个 user-data-dir 不允许两个 Chrome 实例同时打开
    （实测报 "Opening in existing browser session"），所以必须分开目录。
    """
    name = config.CHROME_PROFILE_DIR.name
    ns = getattr(_tls, "ns", "") or ""
    if getattr(_tls, "role", "main") == "aux":
        return config.CHROME_PROFILE_DIR.parent / f"{name}{ns}_aux"
    return config.CHROME_PROFILE_DIR.parent / f"{name}{ns}"


def set_worker_ns(ns: str):
    """为当前线程设置工人命名空间（工人池配置目录隔离用）。

    "" = 工人1（沿用主配置 chrome_profile，带 Crexi 登录态）；
    "_w2"/"_w3"… = 后续工人，各自用 chrome_profile_wN / chrome_profile_wN_aux
    等独立目录，绝不允许两个 Chrome 同时打开同一个 user-data-dir。
    """
    _tls.ns = ns or ""


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
    try:
        return _state().pw.chromium.launch_persistent_context(str(profile_dir), **extra)
    except Exception as e:
        if _is_profile_locked_error(e):
            # 上次任务残留的 Chrome 还锁着配置目录 → 精确清理后重试一次
            _kill_stale_profile_chrome(str(profile_dir))
            return _state().pw.chromium.launch_persistent_context(str(profile_dir), **extra)
        raise


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
            # ChromeWhatsNew：关"Chrome 新功能"弹页；Translate* 关自动翻译
            # （Crexi/CityFeet 被翻译后英文标签/价格正则全部失效）
            "--disable-features=Translate,TranslateUI,ChromeWhatsNew",
            "--disable-translate",
            "--enable-gpu",
            "--ignore-gpu-blocklist",
            "--use-angle=d3d11",
            # 防焦点窃取：工人池为每个工人/生产者开全新配置目录，Chrome
            # 首次启动的欢迎页、崩溃恢复气泡、默认浏览器询问都会把自己弹到
            # 前台抢走键盘焦点——全部关掉（实测抢焦点主因）
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            "--disable-crash-reporter"]
    if background:
        # 保留有头渲染以支持 Google Maps/WebGL；窗口移到屏幕外并最小化，
        # 不创建无头窗口，避免街景画布在 headless 下出现黑屏。
        args.extend(["--start-minimized", "--window-position=-32000,-32000"])
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
    # 工人命名空间：feeds 等配置目录也按工人隔离（_w2 → chrome_profile_feeds_w2）
    d = config.CHROME_PROFILE_DIR.parent / (profile_name + (getattr(_tls, "ns", "") or ""))
    last = None
    for attempt in range(2):
        if st.pw is None:
            st.pw = sync_playwright().start()
        try:
            try:
                ctx = st.pw.chromium.launch_persistent_context(
                    str(d), channel="chrome", **_launch_kw(background=True))
            except Exception as e:
                if _is_profile_locked_error(e):
                    _kill_stale_profile_chrome(str(d))
                    ctx = st.pw.chromium.launch_persistent_context(
                        str(d), channel="chrome", **_launch_kw(background=True))
                else:
                    ctx = st.pw.chromium.launch_persistent_context(str(d), **_launch_kw(background=True))
            page = ctx.new_page()
            extra[profile_name] = ctx
            _minimize_context(ctx, d)
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
        _minimize_context(st.context, profile_dir)
    except Exception:
        # 彻底失败：清理驱动，下次任务从干净状态重试
        _stop_pw()
        raise
    return st.context


def force_kill_all():
    """强制杀掉本程序启动的所有 Chrome（按配置目录反查进程）。

    用途：任务终止时若有 Playwright 调用僵死（页面挂起、同步调用无超时），
    协作式终止会永远等不到收尾——杀掉浏览器后，卡死的调用立即抛
    TargetClosedError，工人线程几秒内退出，任务得以收尾。"""
    for d in list(_profile_registry):
        try:
            _kill_stale_profile_chrome(d)
        except Exception:
            pass


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


def _aux_loop(q, lane: str):
    """常驻辅助线程：独占一个 Chrome（<主配置><lane>_aux 目录），专供并行步骤使用。

    lane 是工人命名空间（"" = 旧全局道，"_w2"… = 工人2…）：同一工人的街景/
    查重任务永远走同一条道、同一套配置目录，绝不和别的工人撞 user-data-dir。
    """
    _tls.role = "aux"
    _tls.ns = lane
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


def _aux_queue(lane: str = ""):
    with _lock:
        lanes = _aux["lanes"]
        t = lanes.get(lane)
        if t is None or not t[0].is_alive():
            q = queue.Queue()
            t = threading.Thread(target=_aux_loop, args=(q, lane), daemon=True,
                                 name=f"browser-aux{lane or '-global'}")
            t.start()
            lanes[lane] = (t, q)
        return lanes[lane][1]


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
    """把 fn 丢给**当前工人命名空间**的常驻辅助线程执行，立即返回 Job。

    为什么不用普通 Thread：Playwright Sync API 的对象绑定创建它的线程，
    每开一个新线程就要重启一次 Chrome（数秒），且并发时配置目录容易冲突；
    常驻线程只启动一次，后续复用。

    工人池语义：调用线程带 _tls.ns（"_w2" 等）→ 任务进对应工人的专属道，
    用 <配置>_w2_aux 目录；主任务线程（无 ns）走旧的全局道。
    """
    q = _aux_queue(getattr(_tls, "ns", "") or "")
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
        context = _ensure_context()
        page = context.new_page()
    except Exception:
        _reset()
        context = _ensure_context()
        page = context.new_page()
    _minimize_context(context)
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
