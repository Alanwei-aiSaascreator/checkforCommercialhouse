"""地址清洗：修卡片文本里常见的重复片段。

实测脏数据（LoopNet / Crexi 卡片与外部 API 都会出现）：
  - "537 Long Point Road Road"        → 街名重复
  - "1640 Meeting Street Street # 103" → 同上
  - "413 & 415 415 E Remington st"    → 门牌号重复（会让 Google 地理编码跑偏）
  - "975 Savannah Highway # 8"        → 正常，不能动
清洗只做「紧邻重复」的折叠，不做缩写/重写，避免误改合法地址。
"""
import re

# 连续重复的单词/号码："Road Road"、"415 415"、"Ste Ste"
_DUP_WORD = re.compile(r"\b([A-Za-z0-9#]+)\s+\1\b", re.I)
# 连续重复的两词短语："Long Point Long Point"
_DUP_PAIR = re.compile(r"\b([A-Za-z]+(?:\s+[A-Za-z]+)?)\s+\1\b", re.I)


def clean_address(addr: str) -> str:
    """折叠紧邻重复片段并整理空白；无变化的地址原样返回。"""
    s = (addr or "").strip()
    if not s:
        return s
    prev = None
    while prev != s:
        prev = s
        s = _DUP_WORD.sub(r"\1", s)
        s = _DUP_PAIR.sub(r"\1", s)
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*,", ",", s)
    return s.strip(" ,")
