"""临时工具：用**采集同一个**配置目录（chrome_profile_feeds）开一个有头 Chrome，
停在 LoopNet，供人工登录。登录态会写进该配置目录，后续 LoopNet 采集直接复用。

为什么不能登录主配置 chrome_profile：
    主配置带着 Crexi 登录态，已被 Akamai 记住（实测同一出口 IP 下主配置 Access
    Denied、干净配置正常 200）。LoopNet 的采集身份必须与 Crexi 隔离。

用法（务必先停掉后端服务，两者抢同一个配置目录）：
    .venv\\Scripts\\python.exe _loopnet_login.py
登录完成后直接关闭 Chrome 窗口即可，进程会自己退出并释放配置目录。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright

from server import browser, config

LOG = Path(__file__).with_name("_loopnet_login.log")
PROFILE = "chrome_profile_feeds"        # 与 server/steps/loopnet.py 的 _PROFILE 一致
ENTRIES = ("https://www.loopnet.com/", "https://www.loopnet.com/login/")


def w(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def _logged_in(page) -> bool:
    try:
        body = page.evaluate("() => document.body.innerText").lower()
    except Exception:
        return False
    return any(k in body for k in ("sign out", "log out", "my loopnet", "my account"))


def main() -> None:
    profile_dir = config.CHROME_PROFILE_DIR.parent / PROFILE
    with sync_playwright() as pw:
        kw = dict(browser._launch_kw())
        kw["args"] = list(kw.get("args") or []) + ["--remote-debugging-port=9334"]
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(profile_dir), channel="chrome", **kw)
        except Exception as e:
            w(f"启动 Chrome 失败（回退默认内核）: {str(e)[:120]}")
            kw.pop("args", None)
            ctx = pw.chromium.launch_persistent_context(str(profile_dir), **kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        browser.block_translate(page)
        w(f"PROFILE={profile_dir}")
        for url in ENTRIES:
            try:
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
                title = (page.title() or "").lower()
                w(f"TRY {url} -> TITLE={page.title()}")
                if "not found" not in title and "404" not in title:
                    break
            except Exception as e:
                w(f"打开 {url} 失败: {str(e)[:120]}")
        w(f"URL={page.url}")
        w(f"TITLE={page.title()}")
        try:
            shot = Path(config.SHOTS_DIR) / "_loopnet_login.png"
            page.screenshot(path=str(shot))
            w(f"SHOT={shot}")
        except Exception as e:
            w(f"截图失败: {str(e)[:80]}")
        w("READY 请人工登录 LoopNet；登录完成后直接关闭 Chrome 窗口")
        done = False
        last = ""
        try:
            while True:
                time.sleep(5)
                if not ctx.pages:
                    break
                try:
                    pg = ctx.pages[0]
                    t = (pg.title() or "").strip()
                    if t != last:
                        w(f"TITLE={t} | URL={pg.url}")
                        last = t
                    if not done and _logged_in(pg):
                        shot = Path(config.SHOTS_DIR) / "_loopnet_login_ok.png"
                        pg.screenshot(path=str(shot))
                        w(f"LOGGED_IN 已检测到登录态，截图 {shot}")
                        done = True
                except Exception as e:
                    w(f"轮询异常: {str(e)[:80]}")
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
