"""全州模式的城市发现：从 CommercialCafe 的州 sitemap 页抓该州所有城市。

实测（2026-11）：https://www.commercialcafe.com/sitemaps/commercial-real-estate/us/ca/
是一页 HTML，按字母列出该州所有有房源的城市（加州 278 个），链接形如
    /commercial-real-estate/us/ca/los-angeles/
链接文字就是真实城市名（"Los Angeles"），可直接喂给各数据源。

不用首页的「热门城市」区块：那里只有 20~30 个，且混着地区聚合页
（East Bay / Kern County / Santa Clara County），拿地区名去搜会被判 bad_city。
"""
import json
import re
import time

from . import browser, config

_CACHE_TTL = 7 * 24 * 3600        # 城市清单变化很慢，缓存一周
_REGION_SLUG = re.compile(r"(-county|-bay|-area|-metro|-region)$", re.I)
_REGION_NAME = re.compile(r"\b(county|metro area|bay area|greater)\b", re.I)

# 大城市优先：全州模式的目标是"全州凑够 N 个通过"，先跑房源多的大城市最省时间
_HOT = [
    "Los Angeles", "San Diego", "San Francisco", "San Jose", "Sacramento", "Fresno",
    "Oakland", "Long Beach", "Bakersfield", "Anaheim", "Santa Ana", "Riverside",
    "Stockton", "Irvine", "Chula Vista", "Fremont", "San Bernardino", "Modesto",
    "Oxnard", "Fontana", "Moreno Valley", "Huntington Beach", "Glendale", "Santa Clarita",
    "Garden Grove", "Oceanside", "Rancho Cucamonga", "Santa Rosa", "Ontario", "Elk Grove",
    "Corona", "Lancaster", "Palmdale", "Salinas", "Pomona", "Hayward", "Escondido",
    "Sunnyvale", "Torrance", "Pasadena", "Orange", "Fullerton", "Thousand Oaks",
    "Visalia", "Simi Valley", "Concord", "Roseville", "Santa Clara", "Vallejo",
    "Victorville", "El Monte", "Berkeley", "Downey", "Costa Mesa", "Inglewood",
    "Carlsbad", "Ventura", "Fairfield", "Westminster", "Santa Maria", "Richmond",
    "Murrieta", "Burbank", "Antioch", "Temecula", "Daly City", "Rialto",
    "Santa Monica", "Compton", "Clovis", "Norwalk", "Chino", "Mountain View",
    "Redwood City", "San Mateo", "Redlands", "Pleasanton", "Folsom", "Emeryville",
]
_HOT_IDX = {n.lower(): i for i, n in enumerate(_HOT)}

CHALLENGE = ("just a moment", "请稍候", "安全验证", "access denied", "checking your browser")


def _cache_path(state: str):
    return config.DATA_DIR / f"state_cities_{state.lower()}.json"


def _load_cache(state: str):
    try:
        d = json.loads(_cache_path(state).read_text(encoding="utf-8"))
        return list(d.get("cities") or []), float(d.get("ts") or 0)
    except Exception:
        return [], 0.0


def _save_cache(state: str, cities: list) -> None:
    try:
        _cache_path(state).write_text(
            json.dumps({"ts": time.time(), "cities": cities}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def _sort(cities: list) -> list:
    def key(name: str):
        hit = _HOT_IDX.get(name.lower())
        return (0, hit, name) if hit is not None else (1, 0, name)
    return sorted(cities, key=key)


def _scrape(state: str) -> list:
    url = (f"https://www.commercialcafe.com/sitemaps/commercial-real-estate/"
           f"us/{state.lower()}/")
    page = browser.new_page_in("chrome_profile_feeds")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # Cloudflare 挑战页会自动放行，最长等 40s
        for _ in range(10):
            try:
                body = page.evaluate("document.body.innerText || ''") or ""
            except Exception:
                body = ""
            low = (body or "").lower()
            if body and not any(k in low for k in CHALLENGE):
                break
            time.sleep(4)
        pairs = page.evaluate("""(() => [...document.querySelectorAll('a[href]')]
            .map(a => [a.getAttribute('href') || '', (a.innerText || '').trim()]))()""")
    finally:
        try:
            page.close()
        except Exception:
            pass

    pat = re.compile(rf"/commercial-real-estate/us/{state.lower()}/([a-z0-9\-]+)/?$", re.I)
    out, seen = [], set()
    for href, text in pairs:
        m = pat.search(href or "")
        if not m:
            continue
        slug = m.group(1).lower()
        name = re.sub(r"\s+", " ", (text or "").strip())
        name = re.sub(r"\s+Commercial Real Estate$", "", name, flags=re.I).strip()
        if not name or slug in seen:
            continue
        if _REGION_SLUG.search(slug) or _REGION_NAME.search(name):
            continue        # 地区聚合页（East Bay / Kern County）不是城市，跳过
        seen.add(slug)
        out.append(name)
    return _sort(out)


def discover(state: str, use_cache: bool = True) -> list:
    """返回该州城市名列表（热门大城市在前）。抓取失败时回退到过期缓存。"""
    state = (state or "").strip().upper()
    if not state:
        return []
    cached, ts = _load_cache(state)
    if use_cache and cached and time.time() - ts < _CACHE_TTL:
        return cached
    # 首次访问常遇到 Cloudflare 人机验证，失败就再试一次（浏览器已暖起来，通常就过了）
    for attempt in range(2):
        try:
            cities = _scrape(state)
        except Exception:
            cities = []
        if cities:
            _save_cache(state, cities)
            return cities
        if attempt == 0:
            time.sleep(3)
    return cached or []
