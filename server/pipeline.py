"""核查流水线：查重前置 → 步骤1-4 → 结论归档。"""
import queue
import random
import re
import threading
import traceback
from datetime import datetime

from . import browser, config, deepseek_client, store
from .addrutil import clean_address
from .steps import (cbre, commercialcafe, commercialsearch, crexi, cityfeet, loopnet,
                    officespace, smarty, streetview, opencorporates)

# 步骤1 数据源注册表：source key → (模块, 展示名, 详情页域名)
# 新数据源只要实现 search_candidates / fetch_broker_info（+ candidates_from_urls）
# 就能挂进来，pipeline 其余部分与数据源无关。
FEEDS = {
    "loopnet": (loopnet, "LoopNet", "loopnet.com"),
    "crexi": (crexi, "Crexi", "crexi.com"),
    "cityfeet": (cityfeet, "CityFeet", "cityfeet.com"),
    "commercialcafe": (commercialcafe, "CommercialCafe", "commercialcafe.com"),
    "commercialsearch": (commercialsearch, "CommercialSearch", "commercialsearch.com"),
    "officespace": (officespace, "OfficeSpace", "officespace.com"),
    "cbre": (cbre, "CBRE", "cbre.com"),
}


def feed_of(source: str):
    return FEEDS.get((source or "loopnet").strip().lower()) or FEEDS["loopnet"]


def _manual_candidates(urls: list, source: str = "loopnet") -> dict:
    """手动链接：只解析与当前步骤1 数据源同站的链接。

    各数据源互不替代——选了 Crexi 就只认 crexi.com 的链接，贴了别家链接
    不会"顺手帮你跑"，而是明确报错并忽略，避免把多个数据源的数据混进同一次任务。
    """
    mod, label, expect = feed_of(source)
    ok = [u for u in urls if expect in (u or "").lower()]
    skipped = [u for u in urls if u not in ok]
    if not ok:
        return {"status": "fail", "candidates": [], "screenshot": "",
                "message": f"当前步骤1 数据源是 {label}，但手动链接里没有 {expect} 的链接；"
                           f"两条线不互相替代，请改贴 {label} 详情页链接，"
                           f"或把数据源切换为另一家后重跑"}
    r = mod.candidates_from_urls(ok)
    msg = r.get("message") or ""
    if skipped:
        msg += f"（已忽略 {len(skipped)} 条非 {label} 链接）"
    return {"status": "pass" if r.get("candidates") else "warn",
            "message": msg or "手动链接未解析出候选",
            "candidates": r.get("candidates") or [],
            "screenshot": r.get("screenshot") or ""}


class TaskRunner:
    """一个核查任务；进度通过队列推给 SSE。"""

    def __init__(self, task_id: str, params: dict):
        self.task_id = task_id
        self.params = params
        self.events: queue.Queue = queue.Queue()
        self.done = False
        self.stopped = False

    def stop(self) -> None:
        """协作式终止：当前操作完成后立即停止，不再处理后续候选。"""
        self.stopped = True

    def emit(self, msg: str, level: str = "info") -> None:
        self.events.put(
            {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": msg}
        )

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:
            tb = traceback.format_exc()
            self.emit(f"任务异常终止: {e}", "error")
            self.emit(tb[-1500:], "error")
        finally:
            self.done = True
            if self.stopped:
                self.events.put({"time": datetime.now().strftime("%H:%M:%S"),
                                 "level": "warn", "message": "—— 任务已手动终止 ——"})
            self.events.put({"__done__": True})

    def _run(self) -> None:
        p = self.params
        city, state = p["city"], p["state"]
        price_min, price_max = int(p.get("price_min") or 0), int(p.get("price_max") or 2000)
        target = int(p.get("target") or 3)
        # 步骤1 数据源：LoopNet（默认）/ Crexi / CityFeet …（见 FEEDS 注册表）
        source = (p.get("source") or "loopnet").strip().lower()
        feed, src_label, _domain = feed_of(source)
        known_count = store.count_known()
        self.emit(f"开始核查：{city}, {state}，价格 ${price_min}-${price_max}/月，目标 {target} 个地址；"
                  f"步骤1 数据源：{src_label}；收录表共 {known_count} 条")

        manual_urls = [u for u in (p.get("listing_urls") or "").replace("\r", "").split("\n") if u.strip()]
        approved = 0
        seen_addrs: set = set()

        def _auto_candidate_stream():
            """自动搜索：先探出真实总页数，然后把「所有未检查过的页」打乱顺序全部遍历，
            每页先把"已收录/失败档案"的地址剔除，只产出全新候选。

            - 不再抽固定数量页：一次排队全部页，顺序随机（用户要求"全查但顺序随机"）
            - 跨任务记住查过的页，下次从没查过的页里随机排在前面
            - 一直跑到「达到目标通过数」或「所有页都跑完」为止
            """
            # 各源每页条数不同：Crexi 60 / CityFeet 30 / Commercial* 30 / LoopNet 40
            per_page = {"crexi": 60, "cityfeet": 30, "commercialcafe": 30,
                        "commercialsearch": 30}.get(source, 40)
            skey = store.page_key(city, state, price_min, price_max, source=source)
            seen_pages = store.page_seen_set(skey)        # 历史任务翻过的页（跨任务记忆）
            used: set = set()
            visits = 0
            total_pages = 0
            reset_done = False
            plan: list = []
            while not self.stopped and approved < target:
                if not plan:
                    if not total_pages:
                        # 用第 1 页探总页数（只为读分页条，不代表优先查第 1 页）
                        plan = [1]
                        self.emit("步骤1：先用第 1 页探测该搜索条件共有多少页...")
                    else:
                        remaining = [p for p in range(1, total_pages + 1)
                                     if p not in used and p not in seen_pages]
                        if not remaining:
                            if reset_done:
                                self.emit(f"{src_label} 共 {total_pages} 页已全部遍历，"
                                          f"未再产出全新候选", "warn")
                                return
                            # 所有页都检查过：清空翻页记忆，再随机重跑一轮（只重置一次）
                            reset = store.page_reset(skey)
                            seen_pages.clear()
                            used.clear()
                            reset_done = True
                            self.emit(f"该搜索条件共 {total_pages} 页都检查过了，重置翻页记录"
                                      f"（清掉 {reset} 条），重新随机遍历", "warn")
                            continue
                        plan = remaining
                        random.shuffle(plan)
                        self.emit(f"步骤1：{src_label} 全部 {total_pages} 页按随机顺序遍历"
                                  f"（本轮 {len(plan)} 页，已跳过检查过的 {len(seen_pages)} 页）："
                                  + "、".join(f"第{p}页" for p in plan))
                produced = 0
                for page_no in list(plan):
                    if self.stopped or approved >= target:
                        return
                    if total_pages and page_no > total_pages:
                        continue
                    used.add(page_no)
                    visits += 1
                    self.emit(f"步骤1：{src_label} 采集候选（第 {page_no}/{total_pages or '?'} 页，"
                              f"已访问 {visits} 页）...")
                    r1 = feed.search_candidates(city, state, price_min, price_max,
                                                limit=per_page, tag=f"task{self.task_id}",
                                                page=page_no)
                    self.emit(f"步骤1结果（第{page_no}页）：{r1['message']}",
                              "pass" if r1["status"] == "pass" else "warn")
                    if visits == 1 and r1["screenshot"]:
                        self.events.put({"__screenshot__": {"step": "step1", "path": r1["screenshot"]}})
                    # 反爬拦截：后面的页同样过不去，继续抽只是空转
                    if r1["status"] == "fail" and "拦截" in r1["message"]:
                        self.emit(f"{src_label} 被反爬拦截，停止自动搜索", "error")
                        return
                    # 第 1 页就 404 = 搜索地区本身无效（如把州名当城市填），
                    # 再抽 20 页只是重复 404，直接终止并给出可操作提示
                    if r1.get("not_found") and page_no == 1:
                        self.emit(r1["message"], "error")
                        return
                    # 404 页：说明抽样超出真实总页数 → 收窄页池，别再碰更高的页
                    if r1.get("not_found"):
                        real = int(r1.get("total_pages") or 0) or max(page_no - 1, 0)
                        if real and (not total_pages or real < total_pages):
                            total_pages = real
                            self.emit(f"  实测第 {page_no} 页为 404，真实总页数收窄为 "
                                      f"{total_pages} 页，后续只抽 1-{total_pages}", "warn")
                        continue
                    # 正常页（非 404）落库为「已检查」，后续任务随机跳页时直接跳过
                    store.page_mark(skey, page_no)
                    seen_pages.add(page_no)
                    if not total_pages and r1.get("total_pages"):
                        total_pages = int(r1["total_pages"])
                        self.emit(f"  {src_label} 该搜索条件下共 {total_pages} 页（约 {per_page} 条/页）")
                    fresh, skip_known, skip_failed = [], 0, 0
                    for c in r1["candidates"]:
                        if store.normalize(c["address"]) in seen_addrs:
                            continue
                        if store.is_known_address(c["address"]):
                            skip_known += 1
                            continue
                        hit, _reason = store.is_failed_address(c["address"])
                        if hit:
                            skip_failed += 1
                            continue
                        fresh.append(c)
                    if skip_known or skip_failed:
                        self.emit(f"  第{page_no}页剔除：已收录 {skip_known} 个 / 失败档案 {skip_failed} 个", "warn")
                    if not fresh:
                        self.emit(f"第 {page_no} 页无全新候选，继续随机换页...", "warn")
                        continue
                    for c in fresh:
                        seen_addrs.add(store.normalize(c["address"]))
                    produced += len(fresh)
                    yield from fresh
                plan = []
                if produced == 0 and not self.stopped and approved < target:
                    self.emit("本轮随机页均无全新候选，重新抽下一批页...", "warn")

        _JUNK_PRICE = re.compile(r"\b(bill|billion|million|hour|considera|redevelop)\b", re.I)

        def _price_state(cand: dict):
            """价格硬约束三态：('pass', 月租) / ('over', 月租) / ('unknown', None)。

            为什么必须自己算：LoopNet 出租房源的搜索价格过滤是 $/SF/YR 口径
            （pr=0-2000 表示每平方英尺每年 0-2000 美元），不是月租总价。
            一栋 14,000 SF、$6.54/SF/YR 的仓库能通过 LoopNet 过滤，
            实际月租 ≈ $7,631，远超 $2000/月（实测踩坑：4437 Supply Ct, Austin）。
            'unknown' = 价格没抓到 / 抓到的是描述里的金额（"...billion"/"per hour"）/
            $/SF/YR 但面积缺失 → 无法判断是否在预算内。"""
            price_txt = (cand.get("price") or "").strip()
            if not price_txt or _JUNK_PRICE.search(price_txt):
                return "unknown", None
            monthly = loopnet.monthly_price(price_txt, cand.get("area", ""))
            if monthly is None:
                return "unknown", None
            if price_min <= monthly <= price_max:
                return "pass", monthly
            return "over", monthly

        def _suite_no(name: str) -> str:
            """从 "3rd Floor, Ste 302A" 里取套间号 302A。"""
            m = re.search(r"(?:ste|suite|unit)\s*\.?\s*#?\s*([A-Za-z0-9][A-Za-z0-9-]*)",
                          name or "", re.I)
            return m.group(1) if m else ""

        def _suite_hint_addr(address: str, suite_no: str) -> str:
            """把套间号拼进楼栋地址："1150 N State St, Chicago, IL" + 302A →
            "1150 N State St Ste 302A, Chicago, IL"（Smarty 可直接验证）。"""
            if not suite_no:
                return ""
            parts = [p.strip() for p in str(address).split(",")]
            head = f"{parts[0]} Ste {suite_no}"
            return head + (", " + ", ".join(parts[1:]) if len(parts) > 1 else "")

        def _verify_suites(addrs, record_id: int, shots: list):
            """逐一验证套间地址，命中双绿即返回该地址；额度耗尽则终止任务。"""
            for s_addr in addrs or []:
                if self.stopped:
                    return None
                self.emit(f"  验证 {s_addr}")
                r = smarty.verify_address(s_addr, tag=f"r{record_id}")
                shots.append(r.get("screenshot", ""))
                if r["status"] == "quota":
                    self.emit(r["message"], "error")
                    self.stopped = True
                    return None
                if r["status"] == "pass":
                    self.emit(f"  ✓ Suite 地址双绿通过：{s_addr}", "pass")
                    return s_addr
                self.emit(f"  ✗ {r['message'][:60]}")
            return None

        if manual_urls:
            self.emit(f"检测到 {len(manual_urls)} 条手动房源链接，跳过自动搜索（反爬兜底模式）...")
            r1 = _manual_candidates(manual_urls, source)
            self.emit(f"步骤1结果：{r1['message']}", "pass" if r1["status"] == "pass" else "warn")
            if r1["screenshot"]:
                self.events.put({"__screenshot__": {"step": "step1", "path": r1["screenshot"]}})
            if not r1["candidates"]:
                self.emit("手动链接未解析出候选，任务结束", "warn")
                return
            candidate_stream = iter(r1["candidates"])
        else:
            candidate_stream = _auto_candidate_stream()

        for cand in candidate_stream:
            if self.stopped:
                break
            # 清洗重复片段（"537 Long Point Road Road" / "413 & 415 415 E Remington st"）：
            # 脏地址会把 Google 地理编码带偏到别的城市（实测被编到洛杉矶）
            raw_addr = cand["address"]
            addr = clean_address(raw_addr)
            if addr != raw_addr:
                self.emit(f"—— 候选地址：{addr}（原文 {raw_addr}，已去重复）")
                cand["address"] = addr
            else:
                self.emit(f"—— 候选地址：{addr}")

            # 查重前置：与收录表比对
            if store.is_known_address(addr):
                self.emit(f"命中收录表（已收录过），跳过 → {addr}", "warn")
                continue

            # 失败档案前置：此前失败过的地址快速跳过（不耗 Smarty 额度/流程时间）。
            # 精确级（含 Suite）命中任何候选都跳过；楼栋级只跳过"无房号"候选，
            # 带 Ste/Suite 的新候选不受楼栋失败牵连（区分楼和房，不杀错）
            failed_hit, f_reason = store.is_failed_address(addr)
            if failed_hit:
                self.emit(f"命中失败档案（{f_reason[:60]}），快速跳过 → {addr}", "warn")
                continue

            # 价格粗判：卡片上的面积常是「整栋/整层可租面积」，直接按它折算会把
            # 预算内的小套间误杀（实测 1150 N State：整层 1,440 SF × $30 = $3,600，
            # 但 Ste 302A 336 SF × $28 ≈ $784 完全在预算内）。
            # 因此只要粗判不通过，就必须进详情页看「套间表」再定。
            pstate, pmonthly = _price_state(cand)
            # 是否值得开详情页（每条 3-8s，是前半程最大耗时）：
            #   * 卡片写 Undisclosed/价格面议 → 也必须开页：详情页常有套间表
            #     （面积+单价）、经纪人电话，卡片那句话不代表详情页没数字
            #   * 卡片完全没显示价格 → 仍开页（有一部分能在详情页补到真实价格）
            #   * 联系方式/面积缺失仍要开页：SOP 要求中介+电话是硬字段，
            #     而且 email 从未在卡片上出现过，不能拿它当开页理由
            undisclosed = bool(cand.get("undisclosed"))
            # 该数据源以详情页为唯一价格口径（Crexi）：卡片价不参与淘汰，一律进详情页看套间表
            detail_prices = bool(getattr(feed, "DETAIL_IS_PRICE_SOURCE", False))
            # 搜索接口已直接给出套间级月租（crexi 的 leaseDetails.totalMonthly）→
            # 价格口径就是详情页那套，不必再开页，省 3-8s/条
            has_api_price = any(s.get("monthly") is not None
                                for s in (cand.get("spaces") or []))
            if has_api_price:
                need_detail = False
            elif undisclosed:
                # 价格未披露不等于详情页没价格：一律开页核实（套间表/经纪人/面积），
                # 只有详情页确实拿不到价格时才转人工
                need_detail = True
            elif detail_prices:
                need_detail = True
            else:
                need_detail = (not cand.get("price") or not cand.get("area")
                               or pstate != "pass"
                               or not cand.get("agent") or not cand.get("phone"))

            record_id = store.insert_record({
                "address": addr, "price": cand.get("price", ""),
                "agent": cand.get("agent", ""), "phone": cand.get("phone", ""),
                "email": cand.get("email", ""), "area": cand.get("area", ""),
                "listing_url": cand.get("listing_url", ""),
                "step1": f"pass：{src_label} 采集（价格 {cand.get('price', '未知')}）",
                "source": source,   # 记录归属哪条线（LoopNet / Crexi），两条线分开查看/导出
                "screenshots": [],  # SOP：搜索页截图不入档
            })
            shots = []
            spaces: list = list(cand.get("spaces") or [])
            info: dict = {}

            # SOP：价格/中介姓名/联系方式/面积都是硬性字段——搜索卡片缺哪个就去详情页补抓
            if cand.get("listing_url") and need_detail:
                self.emit("补抓详情页（价格/经纪人/面积/套间表）...")
                info = feed.fetch_broker_info(cand.get("listing_url", ""))
                spaces = info.get("spaces") or []
                for k in ("agent", "phone", "email", "price", "area"):
                    if info.get(k) and not cand.get(k):
                        cand[k] = info[k]
                if detail_prices and info.get("price"):
                    # 该数据源以详情页价格为准：卡片价直接换成详情页价，
                    # 避免同一房源出现"卡片说超预算、套间表说在预算内"两套数字
                    cand["price"] = info["price"]
                    if info.get("area"):
                        cand["area"] = info["area"]
                if any(info.get(k) for k in ("agent", "phone", "email", "price", "area")):
                    store.update_record(record_id, agent=cand.get("agent", ""),
                                        phone=cand.get("phone", ""),
                                        email=cand.get("email", ""),
                                        price=cand.get("price", ""),
                                        area=cand.get("area", ""))
                    self.emit(f"  价格：{cand.get('price') or '未知'} | 中介：{cand.get('agent') or '未知'}"
                              f" | 电话：{cand.get('phone') or '未知'} | 邮箱：{cand.get('email') or '未知'}"
                              f" | 面积：{cand.get('area') or '未知'} SF")
                else:
                    self.emit("  详情页未解析到价格/经纪人信息，字段留空（自动流程继续）", "warn")
                if spaces:
                    self.emit("  解析到可租套间：" + "、".join(
                        f"{s['name']} {s['size']} SF" for s in spaces[:6])
                        + f"（共 {len(spaces)} 个）")

            # 价格判定：有套间 → 按「套间月租」判断（选最小且落在预算内的那间）；
            # 没有套间 → 才按整栋/整层面积判断
            fitted = [(s["monthly"], s) for s in spaces
                      if s.get("monthly") is not None
                      and price_min <= s["monthly"] <= price_max]
            if spaces:
                if not fitted:
                    priced = [s for s in spaces if s.get("monthly") is not None]
                    if not priced:
                        # 单价栏被误填成「每月总价」的房源（$5,200/SF/MO × 1,171 SF 这种）：
                        # 若那个数字本身已超预算，无论按单价还是按总价解释都超 → 可判死
                        suspect = [s for s in spaces if s.get("suspected_total")]
                        if suspect and min(s["suspected_total"] for s in suspect) > price_max:
                            bad = min(suspect, key=lambda s: s["suspected_total"])
                            note = (f"单价栏数据异常（{bad['rate']} × {bad['size']} SF 不合理，"
                                    f"疑似把每月总价填进单价栏）：按总价 ${bad['suspected_total']:,.0f}/月"
                                    f"也已超 ${price_max}/月，淘汰")
                            store.update_record(record_id, conclusion="不通过", notes=note)
                            self.emit(f"  {note}", "warn")
                            continue
                        # 区分「数据缺失」和「换算失败」：全部套间都没单价 = 数据源不报价，
                        # 文案要直接说"需询价"，别写成"无法换算"让人以为解析出问题
                        sizes = sorted(int(s["size"]) for s in spaces
                                       if str(s.get("size") or "").isdigit())
                        if suspect:
                            note = (f"套间单价异常（{suspect[0]['rate']}，疑似总价 "
                                    f"${suspect[0]['suspected_total']:,.0f}/月，落在预算内需复核）")
                        elif not any(s.get("rate") for s in spaces) and sizes:
                            note = (f"详情页全部 {len(spaces)} 个套间均未披露单价"
                                    f"（{sizes[0]:,}–{sizes[-1]:,} SF），需向经纪人询价")
                        else:
                            note = "套间价格无法换算成月租，需人工核对"
                        store.update_record(record_id, conclusion="待人工", notes=note)
                        self.emit(f"  {note} → 转人工，继续下一个", "warn")
                        continue
                    cheapest = min(priced, key=lambda s: s["monthly"])
                    note = (f"所有套间均超预算：最小 {cheapest['name']} {cheapest['size']} SF "
                            f"≈ ${cheapest['monthly']:,.0f}/月（要求 ≤${price_max}/月）")
                    store.update_record(record_id, conclusion="不通过", notes=note)
                    self.emit(f"  {note} → 淘汰该房源", "warn")
                    continue
                # 预算内取「面积最大」的那间（同样合规，空间更实用）
                monthly, sp = max(fitted, key=lambda x: int(x[1]["size"]))
                # 单价可能未披露（套间只有 Total Monthly Rent）→ 价格字段写总价，避免留空
                rate_txt = sp["rate"] or f"${monthly:,.0f}/月（总价）"
                calc = (f"{sp['size']} SF × {sp['rate']} ≈ ${monthly:,.0f}/月"
                        if sp["rate"] else f"{sp['size']} SF，总月租 ${monthly:,.0f}")
                cand["price"], cand["area"] = rate_txt, sp["size"]
                cand["suite_name"] = sp["name"]
                cand["suite_no"] = _suite_no(sp["name"])
                store.update_record(
                    record_id, price=f"{rate_txt}（{sp['name']} {sp['size']} SF）",
                    area=sp["size"],
                    notes=f"采用套间 {sp['name']}：{calc}")
                self.emit(f"  套间 {sp['name']}：{calc}"
                          f" → 在 ${price_min}-${price_max}/月 内", "pass")
            else:
                pstate, pmonthly = _price_state(cand)
                if pstate == "over":
                    note = (f"价格超区间：{cand.get('price') or '未知'} ≈ ${pmonthly:,.0f}/月，"
                            f"要求 ${price_min}-${price_max}/月（该房源无套间明细，按整栋算）")
                    store.update_record(record_id, conclusion="不通过", notes=note)
                    self.emit(f"  {note} → 淘汰该房源", "warn")
                    continue
                if pstate == "unknown":
                    # 预算无法判定时不放行（否则 $/SF/YR 缺面积会算出错误月租）
                    if undisclosed:
                        # 已进过详情页仍无价格：写明"详情页也没有数字"，别让人以为是漏开页
                        seen = "详情页也只有 Undisclosed 且无套间总价" if need_detail else "未开详情页"
                        note = (f"价格未披露（Undisclosed/面议），{seen}，面积 "
                                f"{cand.get('area') or '未知'} SF，需人工核对是否 ≤${price_max}/月")
                    else:
                        note = (f"价格无法判定预算：{cand.get('price') or '未抓到价格'}"
                                f"，面积 {cand.get('area') or '未知'} SF，"
                                f"需人工核对是否 ≤${price_max}/月")
                    store.update_record(record_id, conclusion="待人工", notes=note)
                    self.emit(f"  {note} → 转人工，继续下一个", "warn")
                    continue

            # 步骤1.5 办公房源预筛（SOP 补充：明显非办公房源直接不用，360 全景图优先）
            if self.stopped:
                break
            photos = []
            if cand.get("listing_url"):
                # 详情页已经拿到图片 URL → 直接下，不再开一次同样的详情页（省 ~10s/条）
                photo_urls = info.get("photo_urls") or []
                if photo_urls:
                    self.emit(f"下载房源照片（复用详情页图片 {len(photo_urls)} 张，免二次开页）...")
                    photos = streetview.download_photo_urls(
                        photo_urls, tag=f"r{record_id}", referer=cand["listing_url"])
                if not photos:
                    self.emit("下载房源照片并做办公适用性 AI 预筛...")
                    photos = streetview.grab_listing_photos(cand["listing_url"], tag=f"r{record_id}")
                if photos:
                    self.emit(f"  已下载 {len(photos)} 张房源照片（360/全景图优先）")
                    shots.extend(photos[:3])  # 房源图入档（记录页展示用）
                    try:
                        office = deepseek_client.check_office_photos(photos)
                        oc = office.get("conclusion", "uncertain")
                        if oc == "not_office":
                            note = f"AI 预筛：非办公房源（{office.get('reason', '')}），直接淘汰"
                            store.update_record(record_id, conclusion="不通过", notes=note)
                            store.add_failed_address(addr, "预筛", note, building=True)
                            self.emit(f"  {note}", "warn")
                            continue
                        self.emit(f"  AI 预筛：{oc}（置信度 {office.get('confidence', '?')}）"
                                  f" {office.get('reason', '')}",
                                  "pass" if oc == "office" else "warn")
                    except Exception as e:
                        self.emit(f"  AI 预筛调用失败（不阻断流程，稍后街景对比兜底）：{e}", "warn")
                else:
                    self.emit("  未抓到房源照片，跳过预筛（街景步骤将跳过外观比对）", "warn")

            # 步骤2 Smarty
            if self.stopped:
                break
            self.emit("步骤2：Smarty 验证地址有效性...")
            r2 = smarty.verify_address(addr, tag=f"r{record_id}")
            shots.append(r2.get("screenshot", ""))
            self.emit(f"步骤2结果：{r2['message']}", "pass" if r2["status"] == "pass" else ("error" if r2["status"] == "fail" else "warn"))
            store.update_record(record_id, step2=f"{r2['status']}：{r2['message']}", screenshots=[s for s in shots if s])

            # 额度耗尽：停止任务而不是逐个淘汰
            if r2["status"] == "quota":
                store.update_record(record_id, conclusion="不通过", notes="Smarty 额度耗尽，未完成验证")
                self.emit(r2["message"], "error")
                self.stopped = True
                break

            # SOP 常见问题：缺房间号 → 挖号（房源页线索 → Smarty API → Smarty 官网部件）
            msg_low = r2["message"].lower()
            needs_suite = ("missing" in msg_low or "缺房间号" in r2["message"] or "n1" in msg_low)
            if r2["status"] == "fail" and needs_suite:
                # 优先用详情页已经给出的套间号直接验证（省掉 25-40s 挖号）
                hint = _suite_hint_addr(addr, cand.get("suite_no", ""))
                merged: list = []
                if hint:
                    self.emit(f"  {src_label} 详情页已给出套间（{cand.get('suite_name')}），"
                              f"先直接验证 {hint}", "pass")
                    merged = [hint]
                else:
                    self.emit("Smarty 提示缺房间号，在 Smarty 中挖掘该楼房间号...", "warn")
                    # 房间号只从 Smarty 挖（用户要求）：官方 API（订阅后自动生效）
                    # → 官网免费通道（楼层锚点猜 + DPV 判定，不耗我们账号的 lookups）
                    suites = smarty.api_mine_suites(addr, max_suites=4)
                    if suites is None:
                        self.emit("  API 未订阅房间号枚举，改用官网免费通道探测楼层房号...", "warn")
                        suites = smarty.web_mine_suites(addr, max_suites=4)
                    elif suites:
                        self.emit(f"  Smarty API 返回 {len(suites)} 个房间号")
                    merged = list(suites or [])[:4]
                    if merged:
                        self.emit(f"  Smarty 挖到 {len(merged)} 个真实房号（DPV=Y），逐一验证："
                                  + "、".join(s.split(" Ste ")[-1].split(",")[0] for s in merged))
                    else:
                        reason = getattr(smarty, "_web_mine_last_error", "") or "无可用通道"
                        self.emit(f"  Smarty 未能挖到房号（{reason}）→ 该地址淘汰", "warn")
                suite_pass = _verify_suites(merged, record_id, shots)
                # 详情页给的套间没过 → 回退挖号再试（不直接淘汰）
                if not suite_pass and hint and not self.stopped:
                    self.emit("  该套间验证未通过，回退 Smarty 挖号再试...", "warn")
                    suites = smarty.api_mine_suites(addr, max_suites=4)
                    if suites is None:
                        suites = smarty.web_mine_suites(addr, max_suites=4)
                    extra = [s for s in (suites or []) if s != hint][:4]
                    if extra:
                        self.emit("  Smarty 挖到 " + "、".join(
                            s.split(" Ste ")[-1].split(",")[0] for s in extra))
                    else:
                        self.emit(f"  回退挖号也未命中（{getattr(smarty, '_web_mine_last_error', '') or '无可用通道'}）",
                                  "warn")
                    suite_pass = _verify_suites(extra, record_id, shots)
                shots = [s for s in shots if s]
                if suite_pass:
                    addr = suite_pass
                    # 只保留最终通过的绿卡：楼栋/失败 Suite 的红卡不入档（过程在日志可查），
                    # 避免"结论通过但截图显示未通过"的困惑；房源照片保留
                    smarty_card = [s for s in shots if "smarty" in s.lower()][-1:]
                    shots = smarty_card + [p for p in photos[:3] if p]
                    store.update_record(record_id, address=addr,
                                        step2="pass：Suite 级地址双绿通过", screenshots=shots)
                    r2 = {"status": "pass", "message": "Suite 地址双绿", "standard_address": ""}
                else:
                    if self.stopped:
                        store.update_record(record_id, conclusion="不通过", notes="Smarty 额度耗尽，未完成验证")
                        break
                    # 挖号通道被限流（429/401）时不能算"这地址不合格"——它只是没查到，
                    # 转待人工而不登记失败档案，避免把好地址误杀（下次重跑还能挖）
                    mine_reason = getattr(smarty, "_web_mine_last_error", "") or ""
                    if smarty.channel_down(mine_reason):
                        note = f"缺房间号且挖号通道限流（{mine_reason[:60]}），未验证完，建议稍后重跑"
                        store.update_record(record_id, conclusion="待人工", notes=note,
                                            screenshots=shots)
                        self.emit(f"  {note} → 转待人工，继续下一个", "warn")
                        continue
                    # 地址本身有效、只是缺房间号且挖不到 → 不能判死：
                    # 房源和价格都是真实的（实测 10524 Watterson Trail 790 SF $1,200/月
                    # 就在预算内），只是 Smarty 要求 secondary。转待人工，不写失败档案。
                    note = (f"缺房间号且 Smarty 未挖到可验证套间"
                            f"（{r2['message'][:70]}），需人工确认具体套间")
                    store.update_record(record_id, conclusion="待人工",
                                        notes=note, screenshots=shots)
                    self.emit(f"  {note} → 转待人工，继续下一个", "warn")
                    continue
            elif r2["status"] == "fail":
                note = f"Smarty 未通过：{r2['message'][:80]}"
                store.update_record(record_id, conclusion="不通过", notes=note)
                # 楼栋级登记：空置/后缀变化/无效等属楼栋属性，下次无房号候选直接跳过
                store.add_failed_address(addr, "Smarty", note, building=True)
                self.emit("该地址淘汰，处理下一个候选", "warn")
                continue
            if r2["status"] == "suspect":
                # 限流/网络类失败是"这次没查到"，不是"这地址不合格"：
                # 转待人工、不写失败档案（否则好地址被拉黑，重跑也救不回来）
                _transient = ("429", "限流", "请求失败", "请求异常", "timeout", "超时")
                if any(k in r2["message"] for k in _transient):
                    note = f"Smarty 限流/网络未验证完（{r2['message'][:60]}），建议稍后重跑"
                    store.update_record(record_id, conclusion="待人工", notes=note,
                                        screenshots=shots)
                    self.emit(f"  {note} → 转待人工，继续下一个", "warn")
                    continue
                note = f"Smarty 结果不明确，全自动模式保守淘汰：{r2['message'][:80]}"
                store.update_record(record_id, conclusion="不通过", notes=note)
                store.add_failed_address(addr, "Smarty", note, building=True)
                self.emit("Smarty 结果不明确 → 全自动模式按保守原则淘汰", "warn")
                continue

            # SOP：步骤3/4 使用 Smarty 双绿后的标准化地址
            check_addr = r2.get("standard_address") or addr
            if check_addr != addr:
                self.emit(f"采用 Smarty 标准化地址：{check_addr}")

            # 步骤3 街景 + AI 对比（复用预筛阶段已下载的房源照片）
            # 与步骤4 查重并行：两者都用浏览器，但浏览器层已按线程隔离
            # （各自一个 Chrome 实例 + 独立配置目录），互不阻塞。
            # 实测单条：街景 ~26s、查重 ~18s，串行 45s → 并行 ~27s。
            if self.stopped:
                break
            self.emit("步骤3/4 并行：Google Maps 街景+AI 与 OpenCorporates 查重同时进行...")
            # 街景丢给常驻辅助线程（独立 Chrome/配置目录），本线程同时跑查重
            job = browser.submit(lambda: streetview.check_street_view(
                check_addr, cand.get("listing_url", ""),
                tag=f"r{record_id}", photos=photos))
            try:
                r4 = opencorporates.check_duplicates(check_addr, tag=f"r{record_id}")
            except Exception as e:
                r4 = {"status": "suspect", "screenshot": "",
                      "message": f"查重步骤异常: {type(e).__name__}: {e}"}
            try:
                r3 = job.result(timeout=420)
            except Exception as e:
                r3 = {"status": "suspect", "screenshot": "", "extra_shots": [],
                      "message": f"街景步骤异常: {type(e).__name__}: {e}"}
            if not isinstance(r3, dict) or "status" not in r3:
                r3 = {"status": "suspect", "screenshot": "", "extra_shots": [],
                      "message": "街景步骤无有效返回"}

            shots.append(r3.get("screenshot", ""))
            shots.extend(r3.get("extra_shots") or [])
            self.emit(f"步骤3结果：{r3['message']}", "pass" if r3["status"] == "pass" else ("error" if r3["status"] == "fail" else "warn"))
            store.update_record(record_id, step3=f"{r3['status']}：{r3['message']}", screenshots=[s for s in shots if s])
            if r3["status"] != "pass":
                reason = "AI 判定外观不符" if r3["status"] == "fail" else "街景对比无法完成"
                store.update_record(record_id, conclusion="不通过", notes=reason)
                # 精确级登记：视觉问题只拉黑该具体地址，同楼其他 Suite 仍可尝试
                store.add_failed_address(addr, "街景", reason, building=False)
                self.emit("该地址淘汰，处理下一个候选", "warn")
                continue

            # 步骤4 OpenCorporates（结果已在上面与步骤3 并行取得，这里只报状态）
            self.emit("步骤4：OpenCorporates 查重完成（与步骤3 并行）")
            shots.append(r4.get("screenshot", ""))
            self.emit(f"步骤4结果：{r4['message']}", "pass" if r4["status"] == "pass" else ("error" if r4["status"] == "fail" else "warn"))
            store.update_record(record_id, step4=f"{r4['status']}：{r4['message']}", screenshots=[s for s in shots if s])

            if r4["status"] != "pass":
                note = "查重公司数超标" if r4["status"] == "fail" else "查重结果无法解析"
                store.update_record(record_id, conclusion="不通过", notes=note)
                # 公司数超标按楼栋级（同楼注册公司一样多）；解析失败仅精确级
                store.add_failed_address(addr, "查重", note,
                                         building=(r4["status"] == "fail"))
                self.emit("该地址淘汰，处理下一个候选", "warn")
                continue

            # 四步全过 → 通过并写入收录表（供后续查重）
            store.update_record(record_id, conclusion="通过")
            store.import_known_addresses([addr], source="auto")
            approved += 1
            self.emit(f"✓ 地址核查通过并归档：{addr}", "pass")

            if approved >= target:
                self.emit(f"已达成目标：{approved}/{target} 个通过地址", "pass")
                break

        if approved >= target:
            self.emit(f"任务完成：通过 {approved}/{target} 个地址", "pass")
        elif self.stopped:
            self.emit(f"任务已终止：通过 {approved}/{target} 个地址", "warn")
        else:
            self.emit(f"任务完成：候选已用尽，本轮通过 {approved}/{target} 个地址。"
                      f"可换城市/放宽价格区间再跑，或直接粘房源链接补搜", "warn")


# 全局任务注册表（单机单用户足够）
RUNNERS: dict[str, TaskRunner] = {}


def start_task(task_id: str, params: dict) -> TaskRunner:
    runner = TaskRunner(task_id, params)
    RUNNERS[task_id] = runner

    def _run_and_cleanup():
        try:
            runner.run()
        finally:
            # 任务结束必须关掉本线程的 Chrome，否则进程残留会占住配置目录，
            # 下一个任务启动时报 "Opening in existing browser session"
            browser.close_current()

    # 用独立线程跑流水线（而非事件循环的 executor）：executor 线程池被 uvicorn
    # 复用后可能带事件循环上下文，Playwright Sync API 会报
    # "using Sync API inside the asyncio loop"。全新线程则干净无环。
    threading.Thread(target=_run_and_cleanup, daemon=True, name=f"task-{task_id}").start()
    return runner
