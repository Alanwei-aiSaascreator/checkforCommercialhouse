"""DeepSeek 视觉模型客户端：街景 vs 房源照片 外观对比。

OpenAI 兼容 chat 接口，多图输入，要求返回 JSON：
{conclusion: pass|reject|suspect, confidence: 0-1, reason: str, key_differences: [str]}
"""
import base64
import json
import re
import time
from pathlib import Path

import httpx

from . import config

_PROMPT = """你是商业地产核查专家。前面几张图是【同一地址】的 Google 街景（多个不同朝向视角，
可能有树木遮挡、只拍到局部墙面、或拍摄年代不同），之后几张是房源详情页（LoopNet/Crexi）的建筑照片。
请判断街景中是否出现房源照片里的同一栋建筑。

严格按以下步骤执行：

第一步·分别描述：简要描述每张街景可见内容（建筑结构/材质/遮挡），以及房源照片主建筑特征。

第二步·特征矩阵：逐项对比"可见"特征——楼体结构与轮廓、层数、外墙材质与颜色、
屋顶形状、入口/台阶/栏杆、门窗布局、周边环境（树木/道路/邻楼）。
只对比双方都可见的特征，被遮挡的特征标注"不可见"。

第三步·分级裁决（严格执行）：
- 强一致：≥2 个核心特征（结构/层数/材质/屋顶）明确吻合 → pass
- 中度一致：可见局部特征吻合（如被树遮挡但砖墙材质、台阶、栏杆、门窗细节吻合）
  且【无任何矛盾特征】→ pass，并在 reason 说明遮挡情况
- 明确矛盾：出现无法解释的矛盾（结构不同/层数不同/材质明显不同/完全是另一栋建筑）→ reject
- 街景完全无有效内容（全遮挡/室内/空地，没有任何可比对建筑）→ suspect

关键原则：
1. 宁可放行中度一致，不可因局部遮挡错杀正确地址——只有"明确矛盾"才允许 reject。
2. 拍摄时间差（树木生长、招牌更换、外墙翻新、车辆变化）属正常差异，不构成矛盾。
3. 街景与照片取景角度不同很正常，重点看建筑本体特征而非构图。

只输出 JSON（不要多余文本）：
{"conclusion": "pass|reject|suspect",
 "confidence": 0.0到1.0,
 "reason": "一句话中文结论",
 "matched_features": ["吻合的特征1", "吻合的特征2"],
 "key_differences": ["差异或存疑点"]}"""


def _data_url(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".") or "png"
    if suffix == "jpg":
        suffix = "jpeg"
    b64 = base64.b64encode(Path(path).read_bytes()).decode()
    return f"data:image/{suffix};base64,{b64}"


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"模型未返回 JSON: {text[:200]}")
    return json.loads(m.group(0))


_OFFICE_PROMPT = """你是美国商业地产审核专家。以下是一组 LoopNet 待租房源照片。
请判断该房源是否属于【可注册办公地址的商业/产业物业】。

判断标准（逐条核对）：
【接受，判 office】—— 以下全部算合格：
- 写字楼/办公楼：玻璃幕墙、大堂、办公室内景（工位/会议室/隔间）
- **工业厂房/仓库/配送中心：卷帘门、装卸码头、高棚顶、钢结构——同样接受**
- 灵活空间（Flex）：办公+仓储混合、研发/车间+办公
- 零售/临街商铺/商务园区/医疗诊所等商业物业
- 带商业招牌、Suite 指示牌、楼层导视的建筑

【不接受，判 not_office】—— 仅限以下：
- 住宅：独栋民宅、公寓、卧室/厨房/客厅等家居内景
- 空地/停车场主体/无明显建筑物
- 在建工地（只有施工没有建筑）

照片模糊、仅局部特写、无法确认 → uncertain。
只输出 JSON（不要多余文本）：
{"conclusion": "office|not_office|uncertain",
 "confidence": 0.0到1.0,
 "reason": "一句话中文结论",
 "which_photos_office": [1, 2]}"""


def check_office_photos(image_paths: list) -> dict:
    """LoopNet 房源照片办公适用性预筛（SOP：明显非办公房源直接不用）。"""
    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法进行办公房源预筛")
    content = [{"type": "text", "text": _OFFICE_PROMPT}]
    for i, p in enumerate(image_paths, 1):
        label = {"type": "text", "text": f"（第 {i} 张照片）"}
        content.append(label)
        content.append({"type": "image_url", "image_url": {"url": _data_url(p)}})
    text = _chat_content(content, 1500)
    if not text.strip():
        raise ValueError("模型返回空内容（推理 token 耗尽限额，已重试仍失败）")
    result = _extract_json(text)
    result.setdefault("conclusion", "uncertain")
    result.setdefault("confidence", 0)
    result.setdefault("reason", "")
    result.setdefault("which_photos_office", [])
    return result


def _post_chat(payload: dict, headers: dict, proxies):
    """POST chat/completions。429（每分钟请求数/token 限额）按 Retry-After 退避重试，
    最多 3 次——不加这个，限流会被上层当成"AI 判定失败"，好地址被误淘汰。"""
    for attempt in range(3):
        with httpx.Client(timeout=150, proxy=proxies) as client:
            r = client.post(f"{config.DEEPSEEK_BASE_URL}/chat/completions",
                            json=payload, headers=headers)
        if r.status_code == 429 and attempt < 2:
            try:
                wait = float(r.headers.get("Retry-After") or 0)
            except Exception:
                wait = 0
            time.sleep(min(max(wait, 3.0 * (attempt + 1)), 20.0))
            continue
        r.raise_for_status()
        return r.json()


def _chat_content(content: list, max_tokens: int) -> str:
    """调 chat/completions 返回 content 文本。deepseek-flash 的 reasoning token
    会占用 max_tokens，不足时 content 为空（此前 1500 偶发不够）——空回复自动加倍重试一次。"""
    headers = {"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"}
    proxies = config.PROXY_URL or None
    text = ""
    for mt in (max_tokens, max_tokens * 2):
        payload = {
            "model": config.DEEPSEEK_VISION_MODEL,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": mt,
        }
        data = _post_chat(payload, headers, proxies)
        text = data["choices"][0]["message"].get("content") or ""
        if text.strip():
            return text
    return text


def compare_images(street_views: list, listing_photos: list) -> dict:
    """分级对比：street_views=多朝向街景截图，listing_photos=LoopNet 房源照片。
    裁决规则（分级框架）见 _PROMPT：局部遮挡但特征吻合→pass；明确矛盾才 reject。"""
    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError(
            "未配置 DEEPSEEK_API_KEY（请在环境变量或 data/config.json 中配置后重启服务）")
    content = [{"type": "text", "text": _PROMPT}]
    for i, p in enumerate(street_views, 1):
        content.append({"type": "text", "text": f"（街景视角 {i}，朝向可能不同）"})
        content.append({"type": "image_url", "image_url": {"url": _data_url(p)}})
    for i, p in enumerate(listing_photos, 1):
        content.append({"type": "text", "text": f"（房源照片 {i}）"})
        content.append({"type": "image_url", "image_url": {"url": _data_url(p)}})
    text = _chat_content(content, 3000)
    if not text.strip():
        raise ValueError("模型返回空内容（推理 token 耗尽限额，已重试仍失败）")
    result = _extract_json(text)
    result.setdefault("conclusion", "suspect")
    result.setdefault("confidence", 0)
    result.setdefault("reason", "")
    result.setdefault("matched_features", [])
    result.setdefault("key_differences", [])
    return result
