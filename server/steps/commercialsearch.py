"""步骤1（数据源）：CommercialSearch 出租房源采集。

CommercialSearch 与 CommercialCafe 同属 Yardi 同一套模板（列表卡片、套间块、
Contacts 经纪人区块完全一致），只有域名不同 → 直接复用 commercialcafe 的解析：
  /commercial-real-estate/us/<st>/<city>/?listingType=lease
"""
from . import commercialcafe as _cc

_BASE = "https://www.commercialsearch.com"
DETAIL_IS_PRICE_SOURCE = True
PAGE_OK_SIGNAL = True              # 复用 commercialcafe 的 page_ok 页码标记
PAGES_ESTIMATE_UNRELIABLE = True   # 同上：条数估值不可信，需要二分探真实末页


def search_candidates(city: str, state: str, price_min: int, price_max: int,
                      limit: int = 30, tag: str = "search", page: int = 1) -> dict:
    return _cc.search_candidates(city, state, price_min, price_max,
                                 limit=limit, tag=tag, page=page, base=_BASE)


def fetch_broker_info(listing_url: str) -> dict:
    return _cc.fetch_broker_info(listing_url)


def candidates_from_urls(urls: list) -> dict:
    return _cc.candidates_from_urls(urls)
