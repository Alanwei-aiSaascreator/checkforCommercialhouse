"""邮箱旁路反查：不碰浏览器、不碰 Akamai、不进主线流水线。

思路：从 records.agent（形如 "Michael Cassidy（Medori Commercial Realty LLC）"）
解析出「经纪人姓名 + 公司名」，再调 Hunter.io 反查真实邮箱，写回 records.email。

与主线完全解耦：独立脚本 run_email_enrich.py 或 /api/email-enrich 手动触发，
缺 key / 反查不到都不影响核查任务本身。

缓存语义：只有「Hunter 返回 200 且确实查不到」才会缓存为 miss；
401/402/429/5xx/网络错误返回 api_error，不写缓存，下次重试（避免额度用尽时把记录全污染成 miss）。
"""
import json
import re
import threading
import time

import httpx

from . import config

# 这些是抓取源的兜底文案，不是真实邮箱，一律视为「缺失」
EMAIL_PLACEHOLDERS = {
    "请联系该官网工作人员", "请联系官网工作人员", "请联系官网",
    "contact", "n/a", "na", "-", "null", "none",
}
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

_CACHE_PATH = config.DATA_DIR / "email_cache.json"
_CACHE_LOCK = threading.Lock()
_cache = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        with _CACHE_LOCK:
            if _cache is None:
                try:
                    _cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
                except Exception:
                    _cache = {}
    return _cache


def _save_cache() -> None:
    global _cache
    with _CACHE_LOCK:
        try:
            _CACHE_PATH.write_text(
                json.dumps(_cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass


def _cache_get(key: str):
    return _load_cache().get(key)


def _cache_has(key: str) -> bool:
    return key in _load_cache()


def _cache_set(key: str, val) -> None:
    _load_cache()[key] = val
    _save_cache()


def _http_get(url: str, params: dict):
    """统一 HTTP 出口，复用代理；网络异常返回 None。"""
    try:
        with httpx.Client(timeout=30, proxy=config.PROXY_URL or None) as client:
            return client.get(url, params=params)
    except httpx.HTTPError:
        return None


def _http_post(url: str, json_body: dict, headers: dict):
    """POST 出口（Apollo 用：X-Api-Key 头 + JSON body）。"""
    try:
        with httpx.Client(timeout=30, proxy=config.PROXY_URL or None) as client:
            return client.post(url, json=json_body, headers=headers)
    except httpx.HTTPError:
        return None


def is_missing(email: str) -> bool:
    """占位文案 / 空 / 非法格式都算缺失。"""
    e = (email or "").strip().lower()
    if not e:
        return True
    if e in EMAIL_PLACEHOLDERS:
        return True
    # 中文占位、英文"联系官网"类兜底
    if any(p in e for p in ("请联系", "contact the", "visit the website", "contact us")):
        return True
    return not _EMAIL_RE.match(e)


def parse_agent(agent: str):
    """从 "Michael Cassidy（Medori Commercial Realty LLC）" 解析出 {first,last,company}。

    兼容：姓名后带英文逗号头衔（"Sher Tolan, SIOR（...）"）。
    返回 None 表示无法解析成可反查的人名（空、通用账户、单名等）。
    """
    a = (agent or "").strip()
    if not a:
        return None
    company = ""
    name = a
    m = re.match(r"^(.*?)[（(](.*?)[）)]?\s*$", a)
    if m:
        name = m.group(1).strip()
        company = m.group(2).strip().rstrip(" ,，")
    # 去掉 "Tolan, SIOR" 这类逗号后头衔
    name = re.split(r"[，,]", name)[0].strip()
    parts = [p for p in name.split() if p]
    if not parts:
        return None
    # 过滤掉明显不是人名的占位（共享办公前台、通用账户名）
    joined = " ".join(parts).lower()
    if any(k in joined for k in ("admin", "us admin", "contact", "leasing", "info@")):
        return None
    first = parts[0]
    last = parts[-1] if len(parts) > 1 else ""
    if not last:
        return None  # 单名无法猜个人邮箱
    return {"first": first, "last": last, "company": company}


def _hunter_domain(company: str):
    """公司名 → 官网域名。返回 (domain, err)，err=="ok" 表示结果可信（含 None=已知无域名）。"""
    key = f"dom:{company.strip().lower()}"
    if _cache_has(key):
        return _cache_get(key), "ok"
    r = _http_get("https://api.hunter.io/v2/domain-search",
                  {"company": company, "api_key": config.HUNTER_API_KEY})
    if r is None or r.status_code != 200:
        return None, "api_error"
    domains = (r.json().get("data") or {}).get("domains") or []
    dom = domains[0].get("domain") if domains else None
    _cache_set(key, dom)  # None 也缓存：该公司在 Hunter 里查不到域名
    return dom, "ok"


def _hunter_email(domain: str, first: str, last: str):
    """域名 + 姓名 → 邮箱。返回 (email, score, err)。"""
    r = _http_get("https://api.hunter.io/v2/email-finder",
                  {"domain": domain, "first_name": first, "last_name": last,
                   "api_key": config.HUNTER_API_KEY})
    if r is None or r.status_code != 200:
        return None, 0, "api_error"
    d = (r.json().get("data") or {})
    return d.get("email"), d.get("score") or 0, "ok"


# Apollo 的 email_status → 置信度换算（people/match 不直接给分数）
_APOLLO_CONF = {"verified": 90, "likely": 70, "guessed": 40}


def _apollo_email(first: str, last: str, company: str):
    """姓名 + 公司名 → 邮箱（Apollo people/match，一步到位）。返回 (email, score, err)。"""
    r = _http_post("https://api.apollo.io/v1/people/match",
                   {"first_name": first, "last_name": last, "organization_name": company},
                   {"X-Api-Key": config.APOLLO_API_KEY, "Content-Type": "application/json"})
    if r is None or r.status_code != 200:
        return None, 0, "api_error"
    person = (r.json().get("person") or {})
    email = person.get("email")
    status = (person.get("email_status") or "").lower()
    score = _APOLLO_CONF.get(status, 50) if email else 0
    return email, score, "ok"


def find_email(agent: str):
    """核心入口：给一条 agent 文本，返回 (email, score, status)。

    status ∈ {"hit", "miss", "no_key", "no_parse", "no_company", "no_domain", "api_error"}
    数据源：配了 Apollo 优先（一步到位），否则回退 Hunter（两步）。
    """
    if not config.EMAIL_ENRICH_ENABLED:
        return None, 0, "no_key"
    if not config.APOLLO_API_KEY and not config.HUNTER_API_KEY:
        return None, 0, "no_key"
    p = parse_agent(agent)
    if not p:
        return None, 0, "no_parse"
    first, last, company = p["first"], p["last"], p["company"]
    if not company:
        return None, 0, "no_company"

    provider = "apollo" if config.APOLLO_API_KEY else "hunter"
    ck = f"{provider}:e:{company.strip().lower()}|{first.lower()}|{last.lower()}"
    if _cache_has(ck):
        hit = _cache_get(ck)
        if isinstance(hit, dict) and hit.get("email"):
            return hit["email"], hit.get("score", 0), "hit"
        return None, 0, "miss"

    if provider == "apollo":
        email, score, err = _apollo_email(first, last, company)
        if err:
            return None, 0, err          # api_error：不缓存，下次可重试
        if email and not _EMAIL_RE.match(email):
            email = None
        _cache_set(ck, {"email": email, "score": score if email else 0, "ts": time.time()})
        return email, (score if email else 0), ("hit" if email else "miss")

    # Hunter 两步：公司名 → 域名 → 域名+姓名 → 邮箱
    domain, derr = _hunter_domain(company)
    if derr:
        return None, 0, derr
    if not domain:
        _cache_set(ck, {"email": None, "score": 0, "ts": time.time()})
        return None, 0, "no_domain"

    email, score, eerr = _hunter_email(domain, first, last)
    if eerr:
        return None, 0, eerr
    if email and not _EMAIL_RE.match(email):
        email = None
    _cache_set(ck, {"email": email, "score": score if email else 0, "ts": time.time()})
    return email, (score if email else 0), ("hit" if email else "miss")
