"""临时工具：用爬虫同一个主配置目录开一个有头 Chrome，停在 Crexi 登录页，
让人工完成手机号验证；登录态会留在 data/chrome_profile 里，后续采集直接复用。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright

from server import browser, config

LOG = Path(__file__).with_name("_crexi_login.log")


def w(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def main() -> None:
    with sync_playwright() as pw:
        kw = dict(browser._launch_kw())
        # 开调试端口：方便外面用 connect_over_cdp 查看进度/截图确认登录是否成功
        kw["args"] = list(kw.get("args") or []) + ["--remote-debugging-port=9333"]
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(config.CHROME_PROFILE_DIR), channel="chrome", **kw)
        except Exception as e:
            w(f"启动 Chrome 失败: {e}")
            ctx = pw.chromium.launch_persistent_context(
                str(config.CHROME_PROFILE_DIR), **kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        browser.block_translate(page)
        for url in ("https://www.crexi.com/login", "https://www.crexi.com/"):
            try:
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_timeout(4500)
                title = (page.title() or "").lower()
                if "not found" not in title and "404" not in title:
                    break
                w(f"{url} 像 404（title={page.title()}），换下一个入口")
            except Exception as e:
                w(f"打开 {url} 失败: {str(e)[:120]}")
        w(f"URL={page.url}")
        w(f"TITLE={page.title()}")
        try:
            shot = Path(config.SHOTS_DIR) / "_crexi_login.png"
            page.screenshot(path=str(shot))
            w(f"SHOT={shot}")
        except Exception as e:
            w(f"截图失败: {str(e)[:80]}")
        w("READY 浏览器已打开，请人工登录并完成手机号验证；用完直接关闭窗口")
        # 保持进程与浏览器存活，用户关掉窗口后自然退出（配置目录随即释放）
        passed = False
        last = ""
        try:
            while True:
                time.sleep(6)
                if not ctx.pages:
                    break
                try:
                    pg = ctx.pages[0]
                    t = (pg.title() or "").strip()
                    if t != last:
                        w(f"TITLE={t} | URL={pg.url}")
                        last = t
                    if not passed and "just a moment" not in t.lower() and t:
                        page_png = Path(config.SHOTS_DIR) / "_crexi_login_ok.png"
                        pg.screenshot(path=str(page_png))
                        w(f"PASSED 已过 Cloudflare 校验，截图 {page_png}")
                        passed = True
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
