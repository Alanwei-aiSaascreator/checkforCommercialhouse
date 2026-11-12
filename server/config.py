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

# Smarty（US Street Address API）
# 本地日限额默认放开（不再卡 100/天，按用户要求"不设额度限制、一直跑到目标数"）；
# 真实余量由 Smarty 账户决定，套餐用尽时 API 返回 402，程序会识别并提示。
SMARTY_AUTH_ID = os.environ.get("SMARTY_AUTH_ID", "")
SMARTY_AUTH_TOKEN = os.environ.get("SMARTY_AUTH_TOKEN", "")
SMARTY_DAILY_LIMIT = int(os.environ.get("SMARTY_DAILY_LIMIT", "1000000"))
# 挖房间号的免费通道：smarty.com 官网演示页自己用的公开 demo key（走站点额度，不耗我们账号）。
# 置 SMARTY_DEMO_MINE=0 可关闭；该 key 随时可能被官方收紧，届时会退化为"挖不到房号"。
SMARTY_DEMO_KEY = os.environ.get("SMARTY_DEMO_KEY", "21102174564513388")
SMARTY_DEMO_MINE = os.environ.get("SMARTY_DEMO_MINE", "1") not in ("0", "false", "False")

# DeepSeek 视觉模型（OpenAI 兼容接口）
# 注意：实测 deepseek-v4-pro 不支持图像（返回 [Unsupported Image]），
# deepseek-flash 支持图像输入；"deepseek-vision" 模型名不存在（会 400）。
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_VISION_MODEL = os.environ.get("DEEPSEEK_VISION_MODEL", "deepseek-flash")

# OpenCorporates 查重阈值：同址注册公司数 ≤ 5 视为通过
OPENCORPORATES_LIMIT = int(os.environ.get("OPENCORPORATES_LIMIT", "5"))

# LoopNet 翻页策略：不再限制页数——先探出该搜索条件的真实总页数，
# 再把「所有未检查过的页」打乱顺序全部遍历（跨任务记住已查页码）。
# （旧的 LOOPNET_PAGE_POOL / LOOPNET_MAX_PAGES 已废弃）


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
