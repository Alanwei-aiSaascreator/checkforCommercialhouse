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

# DeepSeek 视觉模型（OpenAI 兼容接口）。优先级：环境变量 > data/config.json > 内置默认。
# 注意：实测 deepseek-v4-pro 不支持图像（返回 [Unsupported Image]），
# deepseek-flash 支持图像输入；"deepseek-vision" 模型名不存在（会 400）。
_DEEPSEEK_BASE_DEFAULT = "https://api.deepseek.com/v1"
_DEEPSEEK_MODEL_DEFAULT = "deepseek-flash"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "")
DEEPSEEK_VISION_MODEL = os.environ.get("DEEPSEEK_VISION_MODEL", "")

# OpenCorporates 查重阈值：同址注册公司数 ≤ 5 视为通过
OPENCORPORATES_LIMIT = int(os.environ.get("OPENCORPORATES_LIMIT", "5"))

# 步骤4 节流：多个工人（4~6 个）同时打 OpenCorporates 会被 Cloudflare 拦成
# 「结果页永远不出现公司数」（实测超级轮次跑全州时成片失败）。
# 这里全局串行化，并保证两次查询之间至少间隔这么多秒；设 0 关闭串行。
OC_MIN_GAP = float(os.environ.get("OC_MIN_GAP", "3"))

# 单次查重等待结果页出数的最长秒数（遇到 Cloudflare 校验页会继续等它放行）
OC_READ_TIMEOUT = float(os.environ.get("OC_READ_TIMEOUT", "60"))

# 步骤4 熔断：OpenCorporates 彻底不可用（IP 被封/站点宕机）时，与其让超级轮次
# 一条接一条地白跑失败（每条还烧 Smarty 额度），不如自动冷却再恢复。
# 连续失败 OC_BREAKER_FAILS 次 → 暂停 OC_BREAKER_COOLDOWN 秒；期间查重直接返回
# suspect（不上人工队列、不写失败档案），冷却到期自动恢复，成功 1 次即清零计数。
OC_BREAKER_FAILS = int(os.environ.get("OC_BREAKER_FAILS", "8"))
OC_BREAKER_COOLDOWN = float(os.environ.get("OC_BREAKER_COOLDOWN", "900"))

# 邮箱旁路反查：不碰浏览器，用 姓名+公司 → 反查邮箱，写回 records.email。
# 两个数据源二选一（都配了则优先 Apollo，一步到位不需要先查域名）：
#   - Apollo.io：people/match，按 姓名+公司名 直接匹配，认证用 X-Api-Key 头。
#   - Hunter.io：两步（公司名→域名 → 域名+姓名→邮箱），认证用 query 参数。
# 留空=禁用；EMAIL_ENRICH_MIN_CONF 为置信度阈值，低于该分的邮箱不采纳。
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
EMAIL_ENRICH_ENABLED = os.environ.get("EMAIL_ENRICH_ENABLED", "1") not in ("0", "false", "False")
EMAIL_ENRICH_MIN_CONF = int(os.environ.get("EMAIL_ENRICH_MIN_CONF", "50"))

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
    global SMARTY_AUTH_ID, SMARTY_AUTH_TOKEN, DEEPSEEK_API_KEY, PROXY_URL, HUNTER_API_KEY, APOLLO_API_KEY
    global DEEPSEEK_BASE_URL, DEEPSEEK_VISION_MODEL
    SMARTY_AUTH_ID = SMARTY_AUTH_ID or str(cfg.get("smarty_auth_id") or "")
    SMARTY_AUTH_TOKEN = SMARTY_AUTH_TOKEN or str(cfg.get("smarty_auth_token") or "")
    DEEPSEEK_API_KEY = DEEPSEEK_API_KEY or str(cfg.get("deepseek_api_key") or "")
    # base_url / model：环境变量 > config.json > 内置默认（api.deepseek.com / deepseek-flash）
    DEEPSEEK_BASE_URL = (DEEPSEEK_BASE_URL or str(cfg.get("deepseek_base_url") or "")
                         or _DEEPSEEK_BASE_DEFAULT)
    DEEPSEEK_VISION_MODEL = (DEEPSEEK_VISION_MODEL or str(cfg.get("deepseek_model") or "")
                             or _DEEPSEEK_MODEL_DEFAULT)
    PROXY_URL = PROXY_URL or str(cfg.get("proxy_url") or "")
    HUNTER_API_KEY = HUNTER_API_KEY or str(cfg.get("hunter_api_key") or "")
    APOLLO_API_KEY = APOLLO_API_KEY or str(cfg.get("apollo_api_key") or "")


_load_config_json()
