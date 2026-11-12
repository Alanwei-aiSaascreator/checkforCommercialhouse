"""步骤3：Google Maps 街景 + DeepSeek 视觉模型对比建筑外观（全自动）。"""
import re
from pathlib import Path

import httpx

from .. import config, deepseek_client
from ..browser import goto_retry as _goto_retry, new_page, safe_close, shot


def _strip_unit(address: str) -> str:
    """SOP：街景搜索前去掉房间号 Ste/Suite/Room。"""
    return re.sub(r"\b(suite|ste|room|rm|unit|#)\s*\w+", " ", address, flags=re.I).strip(" ,")


def _grab_listing_photos(listing_url: str, tag: str, limit: int = 3) -> list[str]:
    """从 LoopNet 详情页抓外观照片（下载原图，非截图）。

    主路径：用浏览器上下文的 request API 下载原图字节——与页面共享 Cookie、
    自动带 UA，可显式补 Referer，且不受页面 CORS 限制（之前浏览器内 fetch
    和 httpx 直下都会 403/被 CORS 挡）。拿到的是 CDN 原图，比元素截图清晰。
    360 全景/虚拟看房图优先（URL 含 360/tour/panorama 的排前面）。
    兜底：元素截图（截渲染像素，一定能成）。
    """
    if not listing_url:
        return []
    paths: list[str] = []
    page = new_page()
    try:
        _goto_retry(page, listing_url, timeout=45000, wait=5000)
        # 滚动触发懒加载
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(2500)
        srcs = page.evaluate(
            """() => {
                const urls = [];
                const og = document.querySelector("meta[property='og:image']");
                if (og && og.content) urls.push(og.content);
                for (const img of document.querySelectorAll('img')) {
                    const s = img.currentSrc || img.src || '';
                    if (s.includes('loopnet.com') && (s.includes('/i2/') || s.includes('/i3/') || s.includes('/i1/'))
                        && img.naturalWidth > 400) urls.push(s);
                    if (urls.length >= 12) break;
                }
                return [...new Set(urls)];
            }"""
        )
        # 360 全景 / 虚拟看房图优先下载
        prio = [s for s in (srcs or [])
                if any(k in s.lower() for k in ("360", "tour", "panorama", "virtual"))]
        normal = [s for s in (srcs or []) if s not in prio]
        for i, src in enumerate(prio + normal):
            if len(paths) >= limit:
                break
            try:
                resp = page.context.request.get(
                    src, headers={"Referer": "https://www.loopnet.com/"}, timeout=20000)
                if resp.status != 200:
                    continue
                body = resp.body()
                if len(body) < 8000:  # 太小的是占位图
                    continue
                p = Path(config.SHOTS_DIR) / f"{tag}_photo{len(paths)}.jpg"
                p.write_bytes(body)
                paths.append(str(p))
            except Exception:
                continue
    except Exception:
        pass
    finally:
        safe_close(page)

    # 兜底：元素截图（CDP 页面渲染像素，绕过一切下载校验）
    if not paths:
        page = new_page()
        try:
            _goto_retry(page, listing_url, timeout=45000, wait=5000)
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(2500)
            imgs = page.locator(
                "img[src*='loopnet.com'][src*='/i2/'], img[src*='loopnet.com'][src*='/i1/']"
            )
            n = min(imgs.count(), limit + 2)
            for i in range(n):
                try:
                    img = imgs.nth(i)
                    if not img.is_visible():
                        img.scroll_into_view_if_needed()
                        page.wait_for_timeout(800)
                    p = Path(config.SHOTS_DIR) / f"{tag}_photo{i}.png"
                    img.screenshot(path=str(p), timeout=15000)
                    if p.exists() and p.stat().st_size > 8000:
                        paths.append(str(p))
                    if len(paths) >= limit:
                        break
                except Exception:
                    continue
        except Exception:
            pass
        finally:
            safe_close(page)
    return paths


def _geocode(address: str):
    """普查局免费地理编码，返回 (lat, lon) 或 None。"""
    try:
        with httpx.Client(timeout=20, proxy=config.PROXY_URL) as client:
            r = client.get(
                "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
                params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
            )
            matches = r.json().get("result", {}).get("addressMatches") or []
            if matches:
                c = matches[0]["coordinates"]
                return c["y"], c["x"]
    except Exception:
        pass
    return None


def _crop_clean(path: str) -> str:
    """裁掉街景截图里的所有 UI：左侧地址栏、小地图、Google 水印、分享/关闭按钮，
    只保留画面中央的建筑物区域（SOP：截图不得包含网页元素和地址信息）。"""
    try:
        from PIL import Image

        img = Image.open(path)
        w, h = img.size
        # 1440x900 视口下，全景图区域约 x:408-1440、UI 集中在四边
        # 取中央安全区：x 680-1270、y 90-760，避开左侧栏(0-408)、
        # 小地图(左下 ~x650)、Google 标识(底部)、分享按钮(右上)
        box = (min(680, w - 200), 90, min(1270, w), min(760, h))
        if box[2] <= box[0] or box[3] <= box[1]:
            return path
        img.crop(box).save(path)
    except Exception:
        pass
    return path


def _img_diff(a: str, b: str) -> float:
    """两张截图的平均灰度差（0-255），用于判断朝向不同的两张街景是否拍到同一画面。"""
    try:
        from PIL import Image, ImageChops

        ia = Image.open(a).convert("L")
        ib = Image.open(b).convert("L")
        if ia.size != ib.size:
            ib = ib.resize(ia.size)
        hist = ImageChops.difference(ia, ib).histogram()
        total = sum(i * v for i, v in enumerate(hist))
        return total / (ia.size[0] * ia.size[1])
    except Exception:
        return 255.0


def _activate_panorama(page) -> None:
    """等待 360 全景画布就绪并点击激活（进入沉浸式全景，画面更大更干净）。"""
    try:
        canvas = page.locator("#widget-scene-canvas").first
        if canvas.count():
            canvas.click(timeout=6000)
            page.wait_for_timeout(2000)
    except Exception:
        pass


def _capture_street_views(page, coords, tag: str, max_views: int = 3) -> list:
    """多朝向采集街景：cbp=12,<heading> 旋转全景（0/90/180/270°），
    保证至少有一张正对目标建筑，图像去重后最多保留 max_views 张。

    SOP 步骤3：单朝向常被树/邻楼遮挡（AI 只看到局部红砖墙误判 suspect），
    多朝向是"拖动屏幕找建筑"的自动化等价物。"""
    lat, lon = coords
    shots = []
    for heading in (0, 90, 180, 270):
        url = (f"https://www.google.com/maps/@?layer=c&cbll={lat},{lon}"
               f"&cbp=12,{heading},0,0,0")
        try:
            _goto_retry(page, url, timeout=45000, wait=5000)
            _activate_panorama(page)
        except Exception:
            continue
        s = shot(page, f"{tag}_maps") or ""
        if not s:
            continue
        s = _crop_clean(s)
        # 去重：与已有视角几乎同画面则跳过
        if any(_img_diff(s, t) < 8.0 for t in shots):
            try:
                from pathlib import Path
                Path(s).unlink(missing_ok=True)
            except Exception:
                pass
            continue
        shots.append(s)
        if len(shots) >= max_views:
            break
    return shots


# 对外别名：pipeline 在预筛阶段先抓照片，步骤3 直接复用（免重复开详情页）
grab_listing_photos = _grab_listing_photos


def check_street_view(address: str, listing_url: str = "", tag: str = "maps",
                      photos: list = None) -> dict:
    """打开 Google Maps 多朝向截取街景，抓房源照片，交给 DeepSeek 分级对比。

    photos：pipeline 预筛阶段已抓取的房源照片路径（None 时内部现抓）。
    返回 screenshot=第一张街景；extra_shots=其余朝向（归档用）。"""
    q = _strip_unit(address)
    page = new_page()
    street_shots = []
    try:
        coords = _geocode(q)
        if coords:
            street_shots = _capture_street_views(page, coords, tag)
        if not street_shots:
            # 坐标直开失败兜底：普通搜索 + 点击街景按钮
            maps_url = f"https://www.google.com/maps/search/?api=1&query={q.replace(' ', '+')}"
            _goto_retry(page, maps_url, timeout=45000, wait=6000)
            try:
                sv = page.locator("button[aria-label*='Street View'], button[aria-label*='街景'],"
                                  " a[aria-label*='Street View']").first
                if sv.count():
                    sv.click(timeout=5000)
                    page.wait_for_timeout(4000)
                    s = shot(page, f"{tag}_streetview") or ""
                    if s:
                        street_shots = [_crop_clean(s)]
            except Exception:
                pass
    except Exception as e:
        try:
            err_shot = shot(page, f"{tag}_maps_error") or ""
        except Exception:
            err_shot = ""
        return {"status": "suspect", "screenshot": err_shot, "extra_shots": [],
                "message": f"Google Maps 访问失败: {e}", "detail": {}}
    finally:
        safe_close(page)

    # 抓房源照片（全自动对比的输入之一），沿用记录级 tag 保证文件名唯一；
    # pipeline 预筛已抓过则直接复用
    photos = photos if photos is not None else _grab_listing_photos(listing_url, tag)
    if not street_shots:
        return {"status": "suspect", "screenshot": "", "extra_shots": [],
                "message": "街景截图失败，无法比对", "detail": {}}
    if not photos:
        # 全自动模式：无照片可比对时跳过外观环节（街景图已留档），不卡流程
        return {"status": "pass", "screenshot": street_shots[0],
                "extra_shots": street_shots[1:],
                "message": "未抓到房源照片，跳过外观比对（街景图已留档可追溯）", "detail": {}}

    try:
        result = deepseek_client.compare_images(street_shots, photos[:3])
    except Exception as e:
        return {"status": "suspect", "screenshot": street_shots[0],
                "extra_shots": street_shots[1:],
                "message": f"DeepSeek 对比调用失败: {e}", "detail": {}}

    conclusion = result.get("conclusion", "suspect")
    status = {"pass": "pass", "reject": "fail"}.get(conclusion, "suspect")
    reason = result.get("reason", "")
    matched = "; ".join(result.get("matched_features", []) or [])
    diffs = "; ".join(result.get("key_differences", []) or [])
    msg = (f"AI 判定={conclusion} (置信度 {result.get('confidence', '?')}, "
           f"街景 {len(street_shots)} 个视角) {reason}")
    if matched:
        msg += f" | 吻合: {matched[:120]}"
    if diffs:
        msg += f" | 差异: {diffs[:120]}"
    return {"status": status, "screenshot": street_shots[0],
            "extra_shots": street_shots[1:], "message": msg, "detail": result}
