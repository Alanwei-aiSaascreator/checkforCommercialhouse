"""全局配置：路径、密钥（环境变量或 data/config.json）。"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # address-check/
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
SHOTS_DIR = DATA_DIR / "screenshots"
CHROME_PROFILE_DIR = DATA_DIR / "chrome_profile"
SHOTS_DIR.mkdir(parents=True, exist_ok=True)

# 可选代理（httpx / Playwright 共用）；留空不用代理
PROXY_URL = os.environ.get("PROXY_URL", "")

# Smarty（US Street Address API）免费额度：每天 100 次查询
SMARTY_AUTH_ID = os.environ.get("SMARTY_AUTH_ID", "")
SMARTY_AUTH_TOKEN = os.environ.get("SMARTY_AUTH_TOKEN", "")
SMARTY_DAILY_LIMIT = int(os.environ.get("SMARTY_DAILY_LIMIT", "100"))

# DeepSeek 视觉模型（OpenAI 兼容接口）
# 注意：实测 deepseek-v4-pro 不支持图像（返回 [Unsupported Image]），
# deepseek-flash 支持图像输入；"deepseek-vision" 模型名不存在（会 400）。
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_VISION_MODEL = os.environ.get("DEEPSEEK_VISION_MODEL", "deepseek-flash")

# OpenCorporates 查重阈值：同址注册公司数 ≤ 5 视为通过
OPENCORPORATES_LIMIT = int(os.environ.get("OPENCORPORATES_LIMIT", "5"))

# LoopNet 翻页策略：顺序翻页总撞在历史核查过的前几页（整页已收录地址），
# 改为「假定总页数池里随机抽页」——默认 100 页里每轮随机抽 10 页。
LOOPNET_PAGE_POOL = int(os.environ.get("LOOPNET_PAGE_POOL", "100"))
LOOPNET_MAX_PAGES = int(os.environ.get("LOOPNET_MAX_PAGES", "10"))


def _load_config_json() -> None:
    """data/config.json 支持 {"smarty_auth_id": ..., "smarty_auth_token": ...,
    "deepseek_api_key": ..., "proxy_url": ...}，优先级低于环境变量。"""
    p = DATA_DIR / "config.json"
    if not p.exists():
        return
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    global SMARTY_AUTH_ID, SMARTY_AUTH_TOKEN, DEEPSEEK_API_KEY, PROXY_URL
    SMARTY_AUTH_ID = SMARTY_AUTH_ID or str(cfg.get("smarty_auth_id") or "")
    SMARTY_AUTH_TOKEN = SMARTY_AUTH_TOKEN or str(cfg.get("smarty_auth_token") or "")
    DEEPSEEK_API_KEY = DEEPSEEK_API_KEY or str(cfg.get("deepseek_api_key") or "")
    PROXY_URL = PROXY_URL or str(cfg.get("proxy_url") or "")


_load_config_json()
