"""步骤1（数据源）：CommercialSearch 出租房源采集。

CommercialSearch 与 CommercialCafe 同属 Yardi 同一套模板（列表卡片、套间块、
Contacts 经纪人区块完全一致），只有域名不同 → 直接复用 commercialcafe 的解析：
  /commercial-real-estate/us/<st>/<city>/?listingType=lease
"""
from . import commercialcafe as _cc

_BASE = "https://www.commercialsearch.com"
DETAIL_IS_PRICE_SOURCE = True
# 实测（2026-11-18）：commercialsearch.com 完全忽略翻页参数（?Page=5 和
# &page=5 返回的都是第 1 页的同样房源，大小写都无效）——管线按 SINGLE_PAGE_ONLY
# 只采第 1 页，否则会拿虚高的条数估值（4868 条 → "163 页"）把同一批房源翻 163 遍
SINGLE_PAGE_ONLY = True


def search_candidates(city: str, state: str, price_min: int, price_max: int,
                      limit: int = 30, tag: str = "search", page: int = 1) -> dict:
    return _cc.search_candidates(city, state, price_min, price_max,
                                 limit=limit, tag=tag, page=page, base=_BASE)


def fetch_broker_info(listing_url: str) -> dict:
    return _cc.fetch_broker_info(listing_url)


def candidates_from_urls(urls: list) -> dict:
    return _cc.candidates_from_urls(urls)
