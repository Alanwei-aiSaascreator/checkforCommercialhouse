"""步骤3：Google Maps 街景 + DeepSeek 视觉模型对比建筑外观（全自动）。"""
import hashlib
import json
import re
import threading
import time
from pathlib import Path

import httpx

from .. import config, deepseek_client
from ..browser import goto_retry as _goto_retry, new_page, safe_close, shot

# 站点通用占位图（默认城市天际线插图、logo 等）在几十个房源里字节完全相同。
# 按内容哈希登记：同一张图出现在 ≥3 个不同房源 → 判为占位图，后续直接丢弃。
_HASH_DB = Path(config.DATA_DIR) / "photo_hashes.json"
_HASH_LOCK = threading.Lock()
_SHARED_AT = 3      # 跨几个房源重复才算占位图（1~2 次可能是同楼重复挂牌，放行）
_HASH_MAX = 8000    # 注册表上限，超出按最后出现时间淘汰


def _load_hashes() -> dict:
    try:
        db = json.loads(_HASH_DB.read_text(encoding="utf-8"))
        if isinstance(db, dict):
            return db
    except Exception:
        pass
    return {}


def _save_hashes(db: dict) -> None:
    tmp = _HASH_DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_HASH_DB)


def _seed_hashes(db: dict) -> None:
    """首次运行把已落盘的历史房源图登记一遍——库里已经在流传的占位图
    立刻生效，不必等它再被重复下载 3 次才认出来。"""
    for p in Path(config.SHOTS_DIR).glob("*_photo*"):
        m = re.match(r"^(.*?)_photo\d+\.(?:jpg|jpeg|png|webp|gif)$", p.name, re.I)
        if not m:
            continue
        try:
            h = hashlib.blake2b(p.read_bytes(), digest_size=16).hexdigest()
        except Exception:
            continue
        rec = db.get(h)
        if not isinstance(rec, dict):
            rec = {"n": 0, "tags": [], "t": 0}
            db[h] = rec
        tag = m.group(1)
        if tag not in rec["tags"]:
            rec["tags"] = (rec["tags"] + [tag])[-10:]
            rec["n"] = int(rec.get("n") or 0) + 1
        rec["t"] = int(p.stat().st_mtime)


def _is_shared_photo(body: bytes, tag: str) -> bool:
    """登记这张图并判断它是否为站点占位图（跨房源重复 ≥_SHARED_AT 次）。"""
    h = hashlib.blake2b(body, digest_size=16).hexdigest()
    with _HASH_LOCK:
        db = _load_hashes()
        if not db.get("_seeded"):
            db.pop("_seeded", None)
            _seed_hashes(db)
            db["_seeded"] = 1
        rec = db.get(h)
        if not isinstance(rec, dict):
            rec = {"n": 0, "tags": [], "t": 0}
            db[h] = rec
        if tag not in rec["tags"]:
            rec["tags"] = (rec["tags"] + [tag])[-10:]
            rec["n"] = int(rec.get("n") or 0) + 1
        rec["t"] = int(time.time())
        shared = int(rec.get("n") or 0) >= _SHARED_AT
        if len(db) > _HASH_MAX:      # 淘汰老条目，避免文件无限膨胀
            keys = sorted((k for k in db if k != "_seeded"),
                          key=lambda k: (db[k].get("t") or 0))
            for k in keys[:len(keys) - _HASH_MAX]:
                db.pop(k, None)
        _save_hashes(db)
    return shared


def _strip_unit(address: str) -> str:
    """SOP：街景搜索前去掉房间号 Ste/Suite/Room。"""
    return re.sub(r"\b(suite|ste|room|rm|unit|#)\s*\w+", " ", address, flags=re.I).strip(" ,")


def _save_photo(body: bytes, tag: str, idx: int) -> str | None:
    """按魔数落盘：扩展名跟着真实格式走，SVG/HTML/错误页直接丢弃。
    以前一律写 .jpg，于是把站点占位 SVG 存成 jpg，DeepSeek 收到
    data:image/jpeg,<svg> 就返回 400 unsupported image（实测）。
    另外按内容哈希挡掉站点通用占位图，别让它混进 AI 预筛/外观对比。"""
    mime = deepseek_client.image_mime(body)
    ext = {"image/jpeg": "jpg", "image/png": "png",
           "image/webp": "webp", "image/gif": "gif"}.get(mime)
    if not ext:
        return None
    if _is_shared_photo(body, tag):
        return None
    p = Path(config.SHOTS_DIR) / f"{tag}_photo{idx}.{ext}"
    p.write_bytes(body)
    return str(p)


def _is_loopnet(url: str) -> bool:
    return "loopnet.com" in (url or "").lower()


def _listing_origin(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url or "", re.I)
    return (m.group(1) + "/") if m else "https://www.loopnet.com/"


def _grab_listing_photos(listing_url: str, tag: str, limit: int = 3) -> list[str]:
    """从房源详情页抓外观照片（下载原图，非截图）。支持 LoopNet / Crexi。

    主路径：用浏览器上下文的 request API 下载原图字节——与页面共享 Cookie、
    自动带 UA，可显式补 Referer，且不受页面 CORS 限制（之前浏览器内 fetch
    和 httpx 直下都会 403/被 CORS 挡）。拿到的是 CDN 原图，比元素截图清晰。
    360 全景/虚拟看房图优先（URL 含 360/tour/panorama 的排前面）。
    兜底：元素截图（截渲染像素，一定能成）。
    """
    if not listing_url:
        return []
    loopnet = _is_loopnet(listing_url)
    # 图片筛选：LoopNet 只认 CDN 路径 /i1//i2//i3/；其他站（Crexi）按「大图 + 排除图标」筛
    if loopnet:
        pick_js = ("(s.includes('loopnet.com') && (s.includes('/i2/') || s.includes('/i3/')"
                   " || s.includes('/i1/')))")
    else:
        pick_js = (r"(!/logo|icon|sprite|avatar|placeholder|favicon/i.test(s) && s.startsWith('http'))")
    paths: list[str] = []
    page = new_page()
    try:
        _goto_retry(page, listing_url, timeout=45000, wait=5000)
        # 滚动触发懒加载
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(2500)
        srcs = page.evaluate(
            f"""() => {{
                const urls = [];
                const og = document.querySelector("meta[property='og:image']");
                if (og && og.content) urls.push(og.content);
                for (const img of document.querySelectorAll('img')) {{
                    const s = img.currentSrc || img.src || '';
                    if ({pick_js} && img.naturalWidth > 400) urls.push(s);
                    if (urls.length >= 12) break;
                }}
                return [...new Set(urls)];
            }}"""
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
                    src, headers={"Referer": _listing_origin(listing_url)}, timeout=20000)
                if resp.status != 200:
                    continue
                body = resp.body()
                if len(body) < 8000:  # 太小的是占位图
                    continue
                p = _save_photo(body, tag, len(paths))
                if not p:
                    continue
                paths.append(p)
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
                if loopnet else "img[src^='http']"
            )
            n = min(imgs.count(), limit + 6)
            for i in range(n):
                try:
                    img = imgs.nth(i)
                    box = img.bounding_box()
                    if not box or box["width"] < 300:
                        continue
                    if not img.is_visible():
                        img.scroll_into_view_if_needed()
                        page.wait_for_timeout(800)
                    p = Path(config.SHOTS_DIR) / f"{tag}_photo{i}.png"
                    img.screenshot(path=str(p), timeout=15000)
                    if p.exists() and p.stat().st_size > 8000:
                        if _is_shared_photo(p.read_bytes(), tag):   # 占位图，删掉不留档
                            p.unlink(missing_ok=True)
                            continue
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


def download_photo_urls(urls: list, tag: str, referer: str = "", limit: int = 3) -> list[str]:
    """下载已知图片 URL（详情页已解析出来），不再导航整页。

    用浏览器上下文的 request API：共享 Cookie/UA、可补 Referer，
    与 _grab_listing_photos 同一套下载方式，只是省掉第二次打开详情页。
    360 全景/虚拟看房图优先（与 _grab_listing_photos 一致）。"""
    if not urls:
        return []
    prio = [u for u in urls if any(k in u.lower() for k in ("360", "tour", "panorama", "virtual"))]
    ordered = prio + [u for u in urls if u not in prio]
    paths: list[str] = []
    page = new_page()
    try:
        for src in ordered:
            if len(paths) >= limit:
                break
            try:
                resp = page.context.request.get(
                    src, headers={"Referer": referer or _listing_origin(src)}, timeout=20000)
                if resp.status != 200:
                    continue
                body = resp.body()
                if len(body) < 8000:      # 太小的是占位图
                    continue
                p = _save_photo(body, tag, len(paths))
                if not p:
                    continue
                paths.append(p)
            except Exception:
                continue
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
