"""核查流水线：查重前置 → 步骤1-4 → 结论归档。"""
import queue
import random
import re
import threading
import time
import traceback
from datetime import datetime

from . import browser, config, deepseek_client, state_cities, store
from .addrutil import clean_address
from .steps import (cbre, commercialcafe, commercialsearch, crexi, cityfeet, loopnet,
                    officespace, smarty, streetview, opencorporates)
from .steps import cross_lookup

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


# Smarty 官网免费挖号通道按 IP 限速（1.1s/次），工人池并行时必须全队共享
# 一条队列，否则 4 个工人同时打 smarty.com 会瞬间触发 429。订阅枚举 API 后
# 此锁可以移除（API 按套餐并发，不吃 IP 限速）。
_mine_lock = threading.Lock()


def _detail_addr_better(cur: str, det: str) -> bool:
    """详情页地址是否比卡片地址更完整可用。

    为什么需要：OfficeSpace 的列表卡片**不显示城市/州**（只有 "501 Union Street"），
    Smarty 拿这种残缺地址必然查不到，会把好房源误淘汰。详情页有完整地址
    （"810 Dominican Drive, Nashville, TN 37228"），只要门牌号一致就采用它。
    """
    if not det or not re.match(r"^\d{1,6}", det):
        return False
    if not re.search(r"[A-Z]{2}\s+\d{5}", det):
        return False
    if re.search(r"\d{5}", cur or ""):        # 卡片已有邮编，不必换
        return False
    m1, m2 = re.match(r"^\d{1,6}", cur or ""), re.match(r"^\d{1,6}", det)
    return bool(m1 and m2 and m1.group(0) == m2.group(0))


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
        self.history: list = []      # 全量日志（刷新页面后 SSE 从这里回放）
        self.done = False
        self.stopped = False
        self.current_record = 0      # 正在核查的记录 id（中止时用它兜底改判）
        self.current_step = ""       # 正在执行的步骤名（终止进度提示用）
        self._stop_at = 0.0
        self._wid_tls = threading.local()   # 工人编号（emit 前缀用，线程本地）
        self._city_bad = False              # 任一源报城市无效 → 全局终止
        # ---- 进度快照（前端进度条 / 工人面板读取，只写不参与判定）----
        self.started_at = 0.0               # 任务开始时间戳（前端算已用时）
        self.approved = 0                   # 已通过数（approved 的镜像）
        self.processed = 0                  # 已开始处理的候选数
        self.worker_stats: dict = {}        # wid → {addr, last, level, ts, pass}
        self._stats_lock = threading.Lock()
        # ---- 全州模式（超级轮次）----
        # 一次任务遍历整州多个城市：当前城市在清单里的位置 / 总数 / 名字，
        # 已完成城市的累计通过数（每个城市内部计数从 0 开始，对外显示累计值）。
        self.city_index = 0
        self.city_total = 0
        self.city_name = ""
        self._base_approved = 0

    def _wstat(self, wid: int) -> dict:
        """取（必要时创建）某个工人的状态字典，供前端分栏展示。"""
        with self._stats_lock:
            return self.worker_stats.setdefault(
                wid, {"addr": "", "last": "", "level": "", "ts": 0.0,
                      "pass": 0, "started_at": 0.0})

    def _push(self, ev: dict) -> None:
        """日志双写：队列供旧 SSE 消费，history 供刷新后回放。"""
        wid = getattr(self._wid_tls, "wid", None)
        if wid:
            ev = dict(ev)
            ev["message"] = f"[工人{wid}] {ev.get('message', '')}"
            ev["worker"] = wid   # 结构化字段：前端据此把日志分到各工人的独立面板
        self.history.append(ev)
        self.events.put(ev)

    def stop(self) -> None:
        """协作式终止：当前操作完成后立即停止，不再处理后续候选。

        终止不是瞬时的（当前步骤要安全收尾），期间每 3 秒推送一次进度，
        让用户清楚看到"终止中"而不是以为页面卡死。"""
        if self.stopped:
            return
        self.stopped = True
        self._stop_at = time.time()
        # 立即置全局终止信号：让 loopnet 反爬冷却等长 sleep 立刻返回、
        # goto_retry 立刻抛异常，而不是等 30s 才 force_kill 浏览器（
        # 纯 time.sleep 根本不吃 force_kill，得靠这个信号打断）
        try:
            browser.request_stop()
        except Exception:
            pass
        step = self.current_step or "收尾"
        self._push({"time": datetime.now().strftime("%H:%M:%S"), "level": "warn",
                    "message": f"⏹ 已收到终止指令，当前步骤【{step}】正在安全收尾..."})

        def _watch():
            forced = False
            for _ in range(80):            # 最多播报 ~4 分钟，防止异常悬挂时刷屏
                if self.done:
                    return
                time.sleep(3)
                if self.done:
                    return
                wait = int(time.time() - self._stop_at)
                cur = self.current_step or "收尾"
                if wait >= 30 and not forced:
                    # 协作终止 30s 还没收尾 = 有 Playwright 调用僵死（页面挂起，
                    # 同步调用没有超时）。强制杀掉所有工人浏览器：卡死的调用
                    # 立即抛 TargetClosedError，工人几秒内退出，任务得以收尾。
                    forced = True
                    self._push({"time": datetime.now().strftime("%H:%M:%S"),
                                "level": "warn",
                                "message": f"⏹ 已等待 30s 仍未收尾（【{cur}】疑似卡死），"
                                           f"强制关闭全部工人浏览器强制终止..."})
                    try:
                        browser.force_kill_all()
                    except Exception:
                        pass
                    continue
                self._push({"time": datetime.now().strftime("%H:%M:%S"), "level": "warn",
                            "message": f"⏹ 终止中（已等待 {wait}s）：当前在【{cur}】，"
                                       f"完成后立即停止，不再处理后续候选..."})

        threading.Thread(target=_watch, daemon=True).start()

    def emit(self, msg: str, level: str = "info") -> None:
        wid = getattr(self._wid_tls, "wid", None)
        if wid:
            # 顺手记下该工人"最后一条日志"，前端工人面板显示当前在干什么
            st = self._wstat(wid)
            st["last"], st["level"], st["ts"] = msg, level, time.time()
        self._push(
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
            # 被中止/异常时，正在核查的那条记录还停在"核查中"，
            # 统一改判为不通过，保证半成品记录不进人工队列
            if self.current_record:
                try:
                    rec = store.get_record(self.current_record)
                    if rec and rec.get("conclusion") in ("待人工", "核查中") and not rec.get("notes"):
                        store.update_record(
                            self.current_record, conclusion="不通过",
                            notes="任务中止/异常，未完成验证，按淘汰处理")
                except Exception:
                    pass
            self.done = True
            if self.stopped:
                self._push({"time": datetime.now().strftime("%H:%M:%S"),
                            "level": "warn", "message": "—— 任务已手动终止 ——"})
            self._push({"__done__": True})

    def _run(self) -> None:
        """任务入口：普通模式跑单个城市；全州模式（scope=state）先发现州内城市，
        再逐个调用 _run_city，通过数在全州范围内累计。"""
        p = self.params
        if str(p.get("scope") or "").strip().lower() != "state":
            self._run_city(p["city"], p["state"], int(p.get("target") or 3))
            return
        self._run_state()

    def _run_state(self) -> None:
        """多城市轮次：逐个跑用户勾选的城市，累计通过数达到 target 即结束。

        城市清单来自前端多选（params["cities"]）；留空时兼容旧行为，
        自动发现州内全部城市（state_cities）。max_hours 只作兜底保险。
        """
        p = self.params
        state = p["state"]
        target = int(p.get("target") or 3)
        max_hours = float(p.get("max_hours") or 240)
        picked = []
        seen = set()
        for c in (p.get("cities") or []):
            c = str(c).strip()
            if c and c.lower() not in seen:
                seen.add(c.lower())
                picked.append(c)
        if picked:
            self.emit(f"多城市轮次启动：{state}，共 {len(picked)} 个城市"
                      f"（{'、'.join(picked)}），累计目标 {target} 个通过地址")
            cities = picked
        else:
            self.emit("未指定城市清单，回退为自动发现全州城市（首次约 10~40 秒，之后走缓存）...")
            cities = state_cities.discover(state)
            if not cities:
                self.emit(f"未能发现 {state} 的城市清单（站点结构变更或被拦截），任务结束", "error")
                return
            self.emit(f"城市清单：共 {len(cities)} 个："
                      + "、".join(cities[:12]) + ("…" if len(cities) > 12 else ""))
        self.city_total = len(cities)
        deadline = time.time() + max_hours * 3600
        approved_total = 0
        for i, city in enumerate(cities, 1):
            if self.stopped or approved_total >= target:
                break
            if time.time() > deadline:
                self.emit(f"已达最长运行时限 {max_hours:g} 小时，停止继续切换城市", "warn")
                break
            self.city_index, self.city_name = i, city
            self.emit(f"===== 轮次 {i}/{len(cities)}：{city}, {state}"
                      f"（累计已通过 {approved_total}/{target}）=====")
            try:
                got = self._run_city(city, state, max(1, target - approved_total))
            except Exception as e:
                self.emit(f"{city} 执行异常，跳过该城市继续下一个：{e}", "error")
                got = 0
            approved_total += int(got or 0)
            self._base_approved = approved_total
            self._city_bad = False      # 单城市无效不影响后续城市
            with self._stats_lock:
                self.approved = approved_total
            # 城市之间彻底释放浏览器，避免下一个城市撞上配置目录锁
            try:
                browser.close_current()
            except Exception:
                pass
        self._report_state_result(approved_total, target)

    def _report_state_result(self, approved_total: int, target: int) -> None:
        done = self.city_index
        if self.stopped:
            self.emit(f"轮次任务已终止：跑了 {done}/{self.city_total} 个城市，"
                      f"累计通过 {approved_total}/{target}", "warn")
        elif approved_total >= target:
            self.emit(f"轮次目标达成：跑了 {done}/{self.city_total} 个城市，"
                      f"累计通过 {approved_total}/{target}", "pass")
        else:
            self.emit(f"轮次任务完成：{done}/{self.city_total} 个城市已遍历，"
                      f"累计通过 {approved_total}/{target}。"
                      f"可过几天再跑（已翻过的页会被跳过，接着上次的进度继续）", "warn")

    def _run_city(self, city: str, state: str, target: int) -> int:
        """单城市流水线：步骤1 采集 → 步骤2/3/4 核查；返回本城市通过数。

        全州模式对每个城市各调一次，target 传「剩余配额」。
        """
        try:
            browser.reset_stop()   # 新任务启动：清掉上次任务的终止标记
        except Exception:
            pass
        if not self.started_at:
            self.started_at = time.time()   # 前端"已用时"的起点（全州模式只记首次）
        p = self.params
        price_min, price_max = int(p.get("price_min") or 0), int(p.get("price_max") or 2000)
        # 步骤1 数据源：支持单源（"loopnet"）或多源（"loopnet,crexi,commercialsearch"，
        # 前端多选）。多源按页轮询汇入同一条流水线，候选按归一化地址去重。
        sources = [s.strip().lower() for s in (p.get("source") or "loopnet").split(",") if s.strip()]
        sources = [s for s in sources if s in FEEDS] or ["loopnet"]
        source = sources[0]
        feed, src_label, _domain = feed_of(source)
        known_count = store.count_known()
        self.emit(f"开始核查：{city}, {state}，价格 ${price_min}-${price_max}/月，目标 {target} 个地址；"
                  f"步骤1 数据源：{' + '.join(feed_of(s)[1] for s in sources)}"
                  f"（{len(sources)} 个源轮询）；收录表共 {known_count} 条")

        manual_urls = [u for u in (p.get("listing_urls") or "").replace("\r", "").split("\n") if u.strip()]
        approved = 0
        seen_addrs: set = set()

        def _source_stream(source_key: str):
            """单数据源的候选流：先探出真实总页数，然后把「所有未检查过的页」打乱
            顺序全部遍历，每页先把"已收录/失败档案"的地址剔除，产出 (源, 批) 元组。

            - 不再抽固定数量页：一次排队全部页，顺序随机（用户要求"全查但顺序随机"）
            - 跨任务记住查过的页，下次从没查过的页里随机排在前面（断点续跑的基础）
            - 本源被反爬拦截/地区无覆盖时只结束本源，不影响其他源继续跑
            """
            _feed, _label, _domain = feed_of(source_key)
            # 单页源：站点完全忽略翻页参数（实测 commercialsearch.com 用大小写
            # Page/page 都返回第 1 页的同样内容），只采第 1 页，绝不空转翻页
            single_page = bool(getattr(_feed, "SINGLE_PAGE_ONLY", False))
            no_new_streak = 0          # 连续多少页无全新候选（第 2 页起计）
            # 各源每页条数不同：Crexi 60 / CityFeet 30 / Commercial* 30 / LoopNet 40
            per_page = {"crexi": 60, "cityfeet": 30, "commercialcafe": 30,
                        "commercialsearch": 30}.get(source_key, 40)
            skey = store.page_key(city, state, price_min, price_max, source=source_key)
            seen_pages = store.page_seen_set(skey)        # 历史任务翻过的页（跨任务记忆）
            used: set = set()
            visits = 0
            total_pages = 0
            cached_total = store.page_limit_get(skey)     # 上次实测的真实末页
            reset_done = False
            plan: list = []

            def _probe_last_page(upper: int) -> int:
                """二分探测真实末页。列表页上的「共 N 条」估值靠不住：实测 Chicago 办公室
                ≤$2000/月 报 1323 条（≈44 页），实际只能翻到第 16 页——第 17 页起站点
                既不报错也不返回空，而是把第 1 页原样再发一遍。所以只能用「请求页码 vs
                页面当前页标记」当信号做二分，log2(44)≈6 次请求探出真值。
                只有「列表页条数估值不可信 + 能上报页码标记」的数据源才做二分；
                其他源（LoopNet/CityFeet/Crexi 的分页条就是真值）直接沿用估值。"""
                if not (getattr(_feed, "PAGE_OK_SIGNAL", False)
                        and getattr(_feed, "PAGES_ESTIMATE_UNRELIABLE", False)):
                    return max(int(upper), 1)
                lo, hi = 1, max(int(upper), 1)
                while lo < hi and not self.stopped:
                    mid = (lo + hi + 1) // 2
                    r = _feed.search_candidates(city, state, price_min, price_max,
                                                limit=per_page, tag=f"task{self.task_id}",
                                                page=mid)
                    if r.get("page_ok") is False:       # 明确回落 → 该页不存在
                        hi = mid - 1
                    elif r.get("page_ok"):              # 页码标记匹配 → 该页存在
                        lo = mid
                    else:                               # 没给标记/加载失败 → 不敢下结论，收手
                        break
                return lo
            while not self.stopped and approved < target:
                if not plan:
                    if not total_pages and cached_total:
                        total_pages = cached_total
                        self.emit(f"  {_label} 该搜索条件实测末页为第 {total_pages} 页"
                                  f"（沿用上次探测结果，过期自动重探）")
                    if not total_pages:
                        # 总页数未知（数据源没暴露分页条）：必须逐页往下探，
                        # 否则会永远只跑第 1 页——实测 CityFeet 卡在首页反复抓同一批，
                        # 已收录的全跳过、页面一直停在同一个列表页
                        nxt = (max(used) + 1) if used else 1
                        plan = [nxt]
                        if nxt == 1:
                            self.emit(f"步骤1（{_label}）：先用第 1 页探测该搜索条件共有多少页...")
                        else:
                            self.emit(f"步骤1（{_label}）：总页数未知，顺序探测第 {nxt} 页"
                                      f"（遇到空页即认定到末页）...")
                    else:
                        remaining = [p for p in range(1, total_pages + 1)
                                     if p not in used and p not in seen_pages]
                        if not remaining:
                            if reset_done:
                                self.emit(f"{_label} 共 {total_pages} 页已全部遍历，"
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
                        self.emit(f"步骤1：{_label} 全部 {total_pages} 页按随机顺序遍历"
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
                    self.current_step = f"步骤1 {_label} 采集第{page_no}页"
                    self.emit(f"步骤1：{_label} 采集候选（第 {page_no}/{total_pages or '?'} 页，"
                              f"已访问 {visits} 页）...")
                    r1 = _feed.search_candidates(city, state, price_min, price_max,
                                                 limit=per_page, tag=f"task{self.task_id}",
                                                 page=page_no)
                    self.emit(f"步骤1结果（第{page_no}页）：{r1['message']}",
                              "pass" if r1["status"] == "pass" else "warn")
                    if visits == 1 and r1["screenshot"]:
                        self._push({"__screenshot__": {"step": "step1", "path": r1["screenshot"]}})
                    # 反爬拦截：本源后续页同样过不去 → 只结束本源，其他源继续跑
                    if r1["status"] == "fail" and "拦截" in r1["message"]:
                        self.emit(f"{_label} 被反爬拦截，本源停止采集"
                                  f"（其余数据源继续）", "error")
                        return
                    # 城市名不存在（站点回落成州级列表）：对所有源都成立 → 全局终止
                    if r1.get("bad_city"):
                        self.emit(r1["message"], "error")
                        self._city_bad = True
                        return
                    # 第 1 页就 404 = 该源对这个地区无覆盖（如把州名当城市填）：
                    # 只结束本源（其他源可能正常覆盖该城市）
                    if r1.get("not_found") and page_no == 1:
                        self.emit(r1["message"] + "（本源无该地区覆盖，其余源继续）", "warn")
                        return
                    # 越界页：404，或站点把第 1 页内容静默再发一遍（页码不匹配）
                    if r1.get("not_found"):
                        real = int(r1.get("total_pages") or 0) or max(page_no - 1, 0)
                        if real and (not total_pages or real < total_pages):
                            total_pages = real
                            why = ("页码越界（站点回落成第 1 页）" if r1.get("page_ok") is False
                                   else "空页/404")
                            self.emit(f"  实测第 {page_no} 页为{why}，真实总页数收窄为 "
                                      f"{total_pages} 页，后续只抽 1-{total_pages}", "warn")
                            if r1.get("page_ok") is False:   # 确定性信号才写缓存
                                store.page_limit_set(skey, total_pages)
                        continue
                    # 正常页（非 404）落库为「已检查」，后续任务随机跳页时直接跳过
                    store.page_mark(skey, page_no)
                    seen_pages.add(page_no)
                    if single_page:
                        if total_pages != 1:
                            total_pages = 1
                            self.emit(f"  {_label} 站点忽略翻页参数（每页都是第 1 页内容），"
                                      f"只采集第 1 页", "warn")
                    elif not total_pages and r1.get("total_pages"):
                        est = int(r1["total_pages"])
                        if (est > 1 and getattr(_feed, "PAGES_ESTIMATE_UNRELIABLE", False)
                                and getattr(_feed, "PAGE_OK_SIGNAL", False)):
                            self.emit(f"  列表页估值 {est} 页，正在二分探测真实末页"
                                      f"（约 6 次请求，避免在越界页上空转）...")
                        total_pages = _probe_last_page(est) if est > 1 else est
                        if total_pages < 2 <= est and est >= 5:
                            # 每个探测页都"回落成第 1 页"，多半不是真只有 1 页，
                            # 而是整站忽略了页码（重定向/参数被吃）→ 保守按估值遍历
                            self.emit(f"  {_label} 末页探测异常（全部回落，结果=1），"
                                      f"改按列表估值 {est} 页遍历", "warn")
                            total_pages = est
                        if total_pages < est:
                            self.emit(f"  {_label} 列表页写的是 {est} 页（约 {per_page} 条/页），"
                                      f"但实测末页只有第 {total_pages} 页——越界页码会被站点"
                                      f"静默回落成第 1 页，已按实测值走", "warn")
                        else:
                            self.emit(f"  {_label} 该搜索条件下共 {total_pages} 页"
                                      f"（约 {per_page} 条/页）")
                        store.page_limit_set(skey, total_pages)
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
                        c["source"] = source_key    # 记录归属哪条线（多源时按候选标记）
                        fresh.append(c)
                    if skip_known or skip_failed:
                        self.emit(f"  第{page_no}页剔除：已收录 {skip_known} 个 / 失败档案 {skip_failed} 个", "warn")
                    if not fresh:
                        if page_no > 1:
                            no_new_streak += 1
                            if no_new_streak >= 5:
                                self.emit(f"{_label} 连续 {no_new_streak} 页无全新候选"
                                          f"（站点翻页疑似失效，每页返回同一批房源），"
                                          f"本源提前停止采集，其余源继续", "warn")
                                return
                        self.emit(f"第 {page_no} 页无全新候选，继续随机换页...", "warn")
                        continue
                    no_new_streak = 0
                    # seen_addrs 的登记已移交给外层轮询生成器（yield 时登记）：
                    # 内层这里如果先登记，外层的跨源去重会把整批误判成"已见过"，
                    # 一条候选都出不去（实测 Tacoma 任务 0 记录的根因）
                    produced += len(fresh)
                    yield fresh
                plan = []
                if produced == 0 and not self.stopped and approved < target:
                    self.emit("本轮随机页均无全新候选，重新抽下一批页...", "warn")

        def _auto_candidate_stream():
            """多源轮询候选流：每个源各自独立翻页，按轮次轮流取一批候选汇入流水线。

            - 候选跨源去重（同一栋楼多处挂牌只处理一次，取先出现的源）
            - 单源被拦/无覆盖只退出该源，其余源继续（比单源模式抗风险）
            - 任何源报 bad_city → 全局终止（城市无效对所有源都成立）
            """
            self._city_bad = False
            live = {s: _source_stream(s) for s in sources}
            round_no = 0
            while live and not self._city_bad:
                round_no += 1
                for s in list(live):
                    if self.stopped or approved >= target or self._city_bad:
                        return
                    try:
                        batch = next(live[s])
                    except StopIteration:
                        self.emit(f"{feed_of(s)[1]} 候选已用尽，退出轮询")
                        del live[s]
                        continue
                    self.emit(f"—— 轮询第 {round_no} 轮：{feed_of(s)[1]} 产出 {len(batch)} 个新候选")
                    for c in batch:
                        norm = store.normalize(c["address"])
                        if norm in seen_addrs:      # 跨源去重：同楼多平台挂牌只跑一次
                            continue
                        seen_addrs.add(norm)
                        yield c

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
                self._push({"__screenshot__": {"step": "step1", "path": r1["screenshot"]}})
            if not r1["candidates"]:
                self.emit("手动链接未解析出候选，任务结束", "warn")
                return
            candidate_stream = iter(r1["candidates"])
        else:
            candidate_stream = _auto_candidate_stream()

        def _process_one(cand, wid: int):
            """单个候选的全流程核查（工人池里每个工人独立调用）。

            原主循环体平移而来：loop 层的 continue → return（结束本条），
            break → return（结束工人循环）。所有局部变量都是每条候选独立的。
            """
            nonlocal approved
            _t0 = time.time()

            def _lap(label: str) -> None:
                """分段计时：只写日志、不改任何判定，用来定位真实瓶颈。"""
                self.emit(f"  ⏱ 累计 {time.time() - _t0:5.1f}s ｜ {label}")

            # 多源模式：每条候选带着自己的源标记，详情页补抓/记录归属/交叉查询
            # 都要用候选对应的源（而不是任务默认源）
            c_source = (cand.get("source") or source).strip().lower()
            c_feed, c_label, _d = feed_of(c_source)
            # 清洗重复片段（"537 Long Point Road Road" / "413 & 415 415 E Remington st"）：
            # 脏地址会把 Google 地理编码带偏到别的城市（实测被编到洛杉矶）
            raw_addr = cand["address"]
            addr = clean_address(raw_addr)
            if addr != raw_addr:
                self.emit(f"—— 候选地址：{addr}（原文 {raw_addr}，已去重复）")
                cand["address"] = addr
            else:
                self.emit(f"—— 候选地址：{addr}")

            # 前端工人面板：这个工人当前在核查哪条地址 + 已开工条数
            self._wstat(wid)["addr"] = addr
            self._wstat(wid)["started_at"] = time.time()
            with self._stats_lock:
                self.processed += 1

            # 查重前置：与收录表比对
            if store.is_known_address(addr):
                self.emit(f"命中收录表（已收录过），跳过 → {addr}", "warn")
                return

            # 失败档案前置：此前失败过的地址快速跳过（不耗 Smarty 额度/流程时间）。
            # 精确级（含 Suite）命中任何候选都跳过；楼栋级只跳过"无房号"候选，
            # 带 Ste/Suite 的新候选不受楼栋失败牵连（区分楼和房，不杀错）
            failed_hit, f_reason = store.is_failed_address(addr)
            if failed_hit:
                self.emit(f"命中失败档案（{f_reason[:60]}），快速跳过 → {addr}", "warn")
                return

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
            detail_prices = bool(getattr(c_feed, "DETAIL_IS_PRICE_SOURCE", False))
            # 搜索接口已直接给出套间级月租（crexi 的 leaseDetails.totalMonthly）→
            # 价格口径就是详情页那套，不必再开页，省 3-8s/条。
            # 但电话缺失时仍要开页：Crexi 登录态下详情页才渲染 tel: 链接（电话硬字段）
            has_api_price = any(s.get("monthly") is not None
                                for s in (cand.get("spaces") or []))
            if has_api_price and str(cand.get("phone") or "").strip():
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
                "step1": (f"pass：{c_label} 采集（价格 {cand.get('price', '未知')}"
                          + (f"，可容纳 {cand['area_ppl']} PPL" if cand.get("area_ppl") else "")
                          + "）"),
                "source": c_source,   # 记录归属哪条线（多源时按候选实际来源）
                "screenshots": [],  # SOP：搜索页截图不入档
            })
            self.current_record = record_id
            inflight[wid] = record_id
            shots = []
            spaces: list = list(cand.get("spaces") or [])
            info: dict = {}

            # SOP：价格/中介姓名/联系方式/面积都是硬性字段——搜索卡片缺哪个就去详情页补抓
            if self.stopped:
                return
            if cand.get("listing_url") and need_detail:
                if getattr(c_feed, "NO_DETAIL_PAGE", False):
                    # 改架构：该源（LoopNet）只出候选不抓详情页，额度留给搜索页。
                    # info 置空 → spaces=[] 走 Crexi 接口兜底；电话走交叉查询 Crexi 通道。
                    self.emit(f"  {c_label} 不抓详情页（额度留给搜索页），"
                              f"价格/套间走 Crexi 兜底、电话走交叉查询")
                    info = {}
                else:
                    self.current_step = "补抓详情页"
                    self.emit("补抓详情页（价格/经纪人/面积/套间表）...")
                    info = c_feed.fetch_broker_info(cand.get("listing_url", ""))
                if info.get("gone"):
                    # 挂牌已下架：站点重定向到搜索兜底页，上面的数据是别人的房源
                    # （实测 10 栋楼全被安上同一个 "From $150/MO + 69 SF"）。
                    # 丢弃详情数据，按卡片价继续（真实报价走 Crexi 套间兜底）。
                    self.emit("  挂牌已下架（站点跳转搜索兜底页），丢弃详情数据",
                              "warn")
                    info = {}
                if info.get("blocked"):
                    # 详情页被速率拦截：字段缺失是"没抓到"而不是"房源没写"，
                    # 日志里必须区分，否则看结果的人会误以为这套房真没有价格
                    self.emit("  ⚠ 详情页被反爬拦截（已降级：套间走 Crexi 兜底、电话走交叉查询），"
                              "本条字段可能缺失", "warn")
                spaces = info.get("spaces") or []
                # 占位语（"请联系该官网工作人员"）不算已有值：详情页拿到真实
                # 电话/邮箱时要能覆盖它（OfficeSpace 卡片默认写占位语）
                _ph = officespace.OFFICIAL_CONTACT
                for k in ("agent", "phone", "email", "price", "area"):
                    iv = str(info.get(k) or "")
                    cv = str(cand.get(k) or "")
                    if iv and (not cv or cv == _ph):
                        cand[k] = info[k]
                if detail_prices and info.get("price"):
                    # 该数据源以详情页价格为准：卡片价直接换成详情页价，
                    # 避免同一房源出现"卡片说超预算、套间表说在预算内"两套数字
                    cand["price"] = info["price"]
                    det_area = str(info.get("area") or "")
                    card_area = str(cand.get("area") or "")
                    # 面积优先级：真实平方英尺 > 人数（"20 PPL"）> 空。
                    # 详情页若只给人数，不能盖掉卡片上的真实 SF（实测 501 Union 被盖成 125）
                    if det_area and (not card_area
                                     or (det_area.isdigit() and not card_area.isdigit())):
                        cand["area"] = det_area
                        cand["area_ppl"] = int(info.get("area_ppl") or 0)
                # 卡片地址缺城市/州/邮编时（OfficeSpace 卡片一律没有），用详情页完整地址
                det_addr = (info.get("address") or "").strip()
                if _detail_addr_better(addr, det_addr):
                    addr = clean_address(det_addr)
                    cand["address"] = addr
                    store.update_record(record_id, address=addr)
                    self.emit(f"  采用详情页完整地址：{addr}")
                if any(info.get(k) for k in ("agent", "phone", "email", "price", "area")):
                    store.update_record(record_id, agent=cand.get("agent", ""),
                                        phone=cand.get("phone", ""),
                                        email=cand.get("email", ""),
                                        price=cand.get("price", ""),
                                        area=cand.get("area", ""))
                    area_txt = str(cand.get("area") or "未知")
                    if area_txt.isdigit():
                        area_txt += " SF"        # 人数写法（"20 PPL"）后面不再加 SF
                    self.emit(f"  价格：{cand.get('price') or '未知'} | 中介：{cand.get('agent') or '未知'}"
                              f" | 电话：{cand.get('phone') or '未知'} | 邮箱：{cand.get('email') or '未知'}"
                              f" | 面积：{area_txt}")
                else:
                    self.emit("  详情页未解析到价格/经纪人信息，字段留空（自动流程继续）", "warn")
                if spaces:
                    self.emit("  解析到可租套间：" + "、".join(
                        f"{s['name']} {s['size'] + ' SF' if str(s['size']).isdigit() else s['size']}".strip()
                        for s in spaces[:6])
                        + f"（共 {len(spaces)} 个）")

            # 价格判定：有套间 → 按「套间月租」判断（选最小且落在预算内的那间）；
            # 没有套间 → 先跨源查 Crexi 套间价（自动兜底），实在没有才按整栋/整层算
            if not spaces:
                _ps0, _pm0 = _price_state(cand)
                if _ps0 != "pass" or info.get("blocked"):
                    # 整栋折算会拒 / 无法判定 / 详情页被拦 → 都值得查一次套间价：
                    # LoopNet 的楼层大空间常可分租，Crexi 接口直接给套间月租（免开详情页）
                    self.emit("  本地无套间数据，跨源查询 Crexi 套间月租（自动兜底）...")
                    try:
                        lk = crexi.lookup_suites_by_address(addr)
                    except Exception as e:
                        lk = {}
                        self.emit(f"  跨源查询异常：{type(e).__name__}: {e}", "warn")
                    if lk.get("spaces"):
                        spaces = lk["spaces"]
                        if lk.get("listing_url") and not cand.get("listing_url"):
                            cand["listing_url"] = lk["listing_url"]
                            store.update_record(record_id, listing_url=lk["listing_url"])
                        if lk.get("address"):
                            cand.setdefault("crexi_addr", lk["address"])
                        self.emit(f"  ✓ Crexi 命中该楼盘 {len(spaces)} 个套间，"
                                  f"按套间月租自动判定（无需人工）", "pass")
                    else:
                        self.emit("  Crexi 未命中同楼盘套间数据，按整栋/整层口径判定", "warn")

            fitted = [(s["monthly"], s) for s in spaces
                      if s.get("monthly") is not None
                      and price_min <= s["monthly"] <= price_max
                      and str(s.get("size") or "").replace(",", "").strip().isdigit()]
            in_budget_missing_size = [s for s in spaces
                                     if s.get("monthly") is not None
                                     and price_min <= s["monthly"] <= price_max
                                     and not str(s.get("size") or "").replace(",", "").strip().isdigit()]
            if spaces:
                if not fitted and in_budget_missing_size:
                    note = "预算内套间缺少有效面积，无法选择套间，直接淘汰"
                    store.update_record(record_id, conclusion="不通过", notes=note)
                    self.emit(f"  {note}", "warn")
                    return
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
                            return
                        # 区分「数据缺失」和「换算失败」：全部套间都没单价 = 数据源不报价，
                        # 文案要直接说"需询价"，别写成"无法换算"让人以为解析出问题
                        sizes = sorted(int(s["size"]) for s in spaces
                                       if str(s.get("size") or "").isdigit())
                        if suspect:
                            note = (f"套间单价异常（{suspect[0]['rate']}，疑似总价 "
                                    f"${suspect[0]['suspected_total']:,.0f}/月），本轮无法确认，跳过")
                            # 报价口径异常不是人工视觉核查事项；不写失败档案，后续重跑可再尝试。
                            store.update_record(record_id, conclusion="不通过", notes=note)
                            self.emit(f"  {note}", "warn")
                            return
                        if not any(s.get("rate") for s in spaces) and sizes:
                            # 套间完全未报价：不值得进入人工队列，直接淘汰。
                            note = (f"全部 {len(spaces)} 个套间均未报价"
                                    f"（{sizes[0]:,}–{sizes[-1]:,} SF），无报价，直接淘汰")
                            store.update_record(record_id, conclusion="不通过", notes=note)
                            self.emit(f"  {note}", "warn")
                            return
                        note = "套间无有效报价或无法换算成月租，直接淘汰"
                        store.update_record(record_id, conclusion="不通过", notes=note)
                        self.emit(f"  {note}", "warn")
                        return
                    cheapest = min(priced, key=lambda s: s["monthly"])
                    note = (f"所有套间均超预算：最小 {cheapest['name']} {cheapest['size']} SF "
                            f"≈ ${cheapest['monthly']:,.0f}/月（要求 ≤${price_max}/月）")
                    store.update_record(record_id, conclusion="不通过", notes=note)
                    self.emit(f"  {note} → 淘汰该房源", "warn")
                    return
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
                    if info.get("blocked"):
                        # 详情页未能确认套间明细：不进入人工视觉队列，跳过本轮候选。
                        note = (f"详情页被反爬拦截，整栋折算 ≈ ${pmonthly:,.0f}/月超预算，"
                                f"套间报价未确认，本轮跳过")
                        store.update_record(record_id, conclusion="不通过", notes=note)
                        self.emit(f"  {note}", "warn")
                        return
                    src_note = ("详情页已核实无套间明细" if need_detail and cand.get("listing_url")
                                else "未开详情页（卡片即整租价）")
                    note = (f"价格超区间：{cand.get('price') or '未知'} ≈ ${pmonthly:,.0f}/月，"
                            f"要求 ${price_min}-${price_max}/月（{src_note}，按整栋/整层算）")
                    store.update_record(record_id, conclusion="不通过", notes=note)
                    self.emit(f"  {note} → 淘汰该房源", "warn")
                    return
                if pstate == "unknown":
                    # 预算无法判定时不放行（否则 $/SF/YR 缺面积会算出错误月租）
                    if undisclosed:
                        # 完全没报价（Contact Broker For Pricing / Undisclosed）：
                        # 连价格口径都没有，无法判断是否符合预算 → 直接淘汰。
                        # 这类房源不再去扒经纪人电话：跟进一个"面议"房源没有意义。
                        seen = "详情页也只有 Undisclosed 且无套间报价" if need_detail else "未开详情页"
                        note = (f"价格面议/未披露（{seen}），无任何可换算的报价，"
                                f"无法判定是否 ≤${price_max}/月，直接淘汰")
                        store.update_record(record_id, conclusion="不通过", notes=note)
                        self.emit(f"  {note}", "warn")
                        return
                    # 有价格但缺面积/口径算不出月租 → 同样视为无法判定，直接淘汰；
                    # 但详情页被拦截导致字段缺失的不判死（可能详情页有套间表），转待人工
                    if info.get("blocked"):
                        note = (f"详情页被反爬拦截，价格字段缺失（卡片价 {cand.get('price') or '无'}），"
                                f"本轮无法确认报价，跳过")
                        store.update_record(record_id, conclusion="不通过", notes=note)
                        self.emit(f"  {note}", "warn")
                        return
                    note = (f"价格无法换算成月租：{cand.get('price') or '未抓到价格'}"
                            f"，面积 {cand.get('area') or '未知'} SF，"
                            f"无法判定是否 ≤${price_max}/月，直接淘汰")
                    store.update_record(record_id, conclusion="不通过", notes=note)
                    self.emit(f"  {note}", "warn")
                    return

            # 步骤1.5 办公房源预筛（SOP 补充：明显非办公房源直接不用，360 全景图优先）
            if self.stopped:
                return
            photos = []
            if cand.get("listing_url"):
                # 详情页已经拿到图片 URL → 直接下，不再开一次同样的详情页（省 ~10s/条）
                photo_urls = info.get("photo_urls") or []
                if photo_urls:
                    self.emit(f"下载房源照片（复用详情页图片 {len(photo_urls)} 张，免二次开页）...")
                    photos = streetview.download_photo_urls(
                        photo_urls, tag=f"r{record_id}", referer=cand["listing_url"])
                if not photos:
                    self.current_step = "下载房源照片 + AI 预筛"
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
                            return
                        self.emit(f"  AI 预筛：{oc}（置信度 {office.get('confidence', '?')}）"
                                  f" {office.get('reason', '')}",
                                  "pass" if oc == "office" else "warn")
                    except Exception as e:
                        self.emit(f"  AI 预筛调用失败（不阻断流程，稍后街景对比兜底）：{e}", "warn")
                else:
                    self.emit("  未抓到房源照片，跳过预筛（街景步骤将跳过外观比对）", "warn")
            _lap("步骤1 详情页 + 照片下载 + AI 预筛")

            # SOP：电话是硬字段。本源没给电话（Crexi 未登录打码 / CommercialCafe
            # 只有表单 / 占位语）→ 拿地址去其它平台交叉查同楼盘的挂牌。
            # 放在价格/预筛都过关之后：只给真正可能通过核查的候选花交叉查询的时间。
            if self.stopped:
                return
            _ph = officespace.OFFICIAL_CONTACT
            if not str(cand.get("phone") or "").strip() or cand.get("phone") == _ph:
                self.current_step = "跨源交叉查询经纪人电话"
                self.emit("  本源未提供经纪人电话 → 跨数据源交叉查询（Crexi 登录态详情页）...")
                try:
                    cr = cross_lookup.lookup_contact(addr, exclude_source=c_source)
                except Exception as e:
                    cr = {}
                    self.emit(f"  交叉查询异常（不阻断流程）：{e}", "warn")
                if cr.get("phone"):
                    cand["phone"] = cr["phone"]
                    if cr.get("agent") and not str(cand.get("agent") or "").strip():
                        cand["agent"] = cr["agent"]
                    if cr.get("email") and not str(cand.get("email") or "").strip():
                        cand["email"] = cr["email"]
                    self.emit(f"  ✓ 交叉命中（{cr.get('source')}）：{cr['phone']}", "pass")
                    store.update_record(record_id, phone=cand["phone"],
                                        agent=cand.get("agent", ""),
                                        email=cand.get("email", ""))
                else:
                    if cross_lookup.engines_down():
                        self.emit("  ⚠ 搜索引擎被限流（验证码熔断中），交叉查询暂停，电话留空"
                                  "——熔断自动恢复后重跑可补上", "warn")
                    else:
                        self.emit("  交叉查询未命中同楼盘挂牌，电话留空", "warn")

            # 步骤2 Smarty
            if self.stopped:
                return
            self.current_step = "步骤2 Smarty 验证"
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
                return

            # SOP 常见问题：缺房间号 → 挖号（房源页线索 → Smarty API → Smarty 官网部件）
            msg_low = r2["message"].lower()
            needs_suite = ("missing" in msg_low or "缺房间号" in r2["message"] or "n1" in msg_low)
            if r2["status"] == "fail" and needs_suite:
                # 优先用详情页已经给出的套间号直接验证（省掉 25-40s 挖号）
                hint = _suite_hint_addr(addr, cand.get("suite_no", ""))
                merged: list = []
                if hint:
                    self.emit(f"  {c_label} 详情页已给出套间（{cand.get('suite_name')}），"
                              f"先直接验证 {hint}", "pass")
                    merged = [hint]
                else:
                    self.emit("Smarty 提示缺房间号，在 Smarty 中挖掘该楼房间号...", "warn")
                    # 房间号只从 Smarty 挖（用户要求）：官方 API（订阅后自动生效）
                    # → 官网免费通道（楼层锚点猜 + DPV 判定，不耗我们账号的 lookups）
                    # 多工人共享一把锁：免费通道按 IP 限速，并行会 429
                    with _mine_lock:
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
                    with _mine_lock:
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
                        return
                    # 挖号通道被限流（429/401）时其实没查到，但仍按"验证不完整"淘汰：
                    # 不写失败档案，下次重跑还能重新挖（用户要求：不留待人工）
                    mine_reason = getattr(smarty, "_web_mine_last_error", "") or ""
                    if smarty.channel_down(mine_reason):
                        note = f"缺房间号且挖号通道限流（{mine_reason[:60]}），未验证完，按淘汰处理"
                        store.update_record(record_id, conclusion="不通过", notes=note,
                                            screenshots=shots)
                        self.emit(f"  {note}", "warn")
                        return
                    # Smarty 已明确该地址缺有效房间号（missing secondary / dpv_match=D）：
                    # 结论明确，直接淘汰，不转人工（人工也无法补出房间号）
                    note = (f"Smarty 确认该楼缺有效房间号（{r2['message'][:70]}），"
                            f"无完整可邮寄地址，直接淘汰")
                    store.update_record(record_id, conclusion="不通过",
                                        notes=note, screenshots=shots)
                    self.emit(f"  {note}", "warn")
                    return
            elif r2["status"] == "fail":
                note = f"Smarty 未通过：{r2['message'][:80]}"
                store.update_record(record_id, conclusion="不通过", notes=note)
                # 楼栋级登记：空置/后缀变化/无效等属楼栋属性，下次无房号候选直接跳过
                store.add_failed_address(addr, "Smarty", note, building=True)
                self.emit("该地址淘汰，处理下一个候选", "warn")
                return
            if r2["status"] == "suspect":
                # Smarty 未明确通过/失败时，不属于视觉人工核查；本轮直接跳过。
                # 不写失败地址档案，后续重跑仍可重新验证，避免人工队列被接口故障灌满。
                note = f"Smarty 未完成有效验证，本轮跳过：{r2['message'][:100]}"
                store.update_record(record_id, conclusion="不通过", notes=note,
                                    screenshots=shots)
                self.emit(f"  {note}（不进入人工队列）", "warn")
                return

            _lap("电话交叉查询 + 步骤2 Smarty")

            # SOP：步骤3/4 使用 Smarty 双绿后的标准化地址
            check_addr = r2.get("standard_address") or addr
            if check_addr != addr:
                self.emit(f"采用 Smarty 标准化地址：{check_addr}")

            # 步骤3 街景 + AI 对比（复用预筛阶段已下载的房源照片）
            # 与步骤4 查重并行：两者都用浏览器，但浏览器层已按线程隔离
            # （各自一个 Chrome 实例 + 独立配置目录），互不阻塞。
            # 实测单条：街景 ~26s、查重 ~18s，串行 45s → 并行 ~27s。
            if self.stopped:
                return
            self.current_step = "步骤3/4 街景AI对比 + 查重（并行）"
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
            # 终止后只等 20s：不拖着任务等街景跑完（正常跑完最多 420s）
            try:
                r3 = job.result(timeout=20 if self.stopped else 420)
            except Exception as e:
                if self.stopped:
                    r3 = {"status": "suspect", "screenshot": "", "extra_shots": [],
                          "message": "任务已终止，街景对比未完成"}
                    store.update_record(record_id, conclusion="待人工",
                                        notes="任务手动终止，该地址未完成核查，可重跑或人工复核")
                else:
                    r3 = {"status": "suspect", "screenshot": "", "extra_shots": [],
                          "message": f"街景步骤异常: {type(e).__name__}: {e}"}
            if not isinstance(r3, dict) or "status" not in r3:
                r3 = {"status": "suspect", "screenshot": "", "extra_shots": [],
                      "message": "街景步骤无有效返回"}

            _lap("步骤3 街景AI对比 + 步骤4 查重（并行）")

            shots.append(r3.get("screenshot", ""))
            shots.extend(r3.get("extra_shots") or [])
            self.emit(f"步骤3结果：{r3['message']}", "pass" if r3["status"] == "pass" else ("error" if r3["status"] == "fail" else "warn"))
            store.update_record(record_id, step3=f"{r3['status']}：{r3['message']}", screenshots=[s for s in shots if s])
            if r3["status"] != "pass":
                if r3["status"] == "fail":
                    reason = "AI 判定外观不符"
                    store.update_record(record_id, conclusion="不通过", notes=reason)
                    # 只有明确建筑矛盾才写入失败档案；临时渲染/证据不足不拉黑。
                    store.add_failed_address(addr, "街景", reason, building=False)
                    self.emit("步骤3明确外观不符，处理下一个候选", "warn")
                    return
                # 技术失败不是人工视觉判断：避免 Google/DeepSeek 暂态故障把队列灌满。
                # 只有 AI 已完成对比但结论为 suspect，才需要人工看图确认。
                msg = str(r3.get("message") or "")
                technical = ("访问失败" in msg or "未完成渲染" in msg or
                             "截图全黑" in msg or "调用失败" in msg or
                             "无有效返回" in msg or "未完成" in msg)
                if technical:
                    reason = f"步骤3技术问题，本轮跳过：{msg[:180]}"
                    store.update_record(record_id, conclusion="不通过", notes=reason)
                    self.emit(f"{reason}（不进入人工队列）", "warn")
                    return
                reason = "街景AI已完成但证据不足，待人工复核（未发现明确建筑矛盾）"
                store.update_record(record_id, conclusion="待人工", notes=reason)
                self.emit("步骤3 AI 证据不足，转待人工", "warn")
                return

            # 步骤4 OpenCorporates（结果已在上面与步骤3 并行取得，这里只报状态）
            self.emit("步骤4：OpenCorporates 查重完成（与步骤3 并行）")
            shots.append(r4.get("screenshot", ""))
            self.emit(f"步骤4结果：{r4['message']}", "pass" if r4["status"] == "pass" else ("error" if r4["status"] == "fail" else "warn"))
            store.update_record(record_id, step4=f"{r4['status']}：{r4['message']}", screenshots=[s for s in shots if s])

            if r4["status"] != "pass":
                if r4["status"] == "fail":
                    note = "查重公司数超标"
                    store.update_record(record_id, conclusion="不通过", notes=note)
                    store.add_failed_address(addr, "查重", note, building=True)
                else:
                    # 查重暂态/解析失败不是地址不合格，不进入人工视觉队列，也不写失败档案。
                    note = f"查重本轮未完成，跳过：{r4.get('message', '')[:160]}"
                    store.update_record(record_id, conclusion="不通过", notes=note)
                self.emit("该地址未完成查重，本轮跳过", "warn")
                return

            # 四步全过 → 通过并写入收录表（供后续查重）
            store.update_record(record_id, conclusion="通过")
            store.import_known_addresses([addr], source="auto")
            with approved_lock:
                approved += 1
            with self._stats_lock:
                # 全州模式下 approved 是"本城市内"的计数，对外显示需加上已完成城市
                self.approved = self._base_approved + approved
                self.worker_stats.setdefault(
                    wid, {"addr": addr, "last": "", "level": "", "ts": 0.0, "pass": 0,
                          "started_at": 0.0})["pass"] += 1
            self.emit(f"✓ 地址核查通过并归档：{addr}", "pass")
            inflight.pop(wid, None)

            if approved >= target:
                self.emit(f"已达成目标：{approved}/{target} 个通过地址", "pass")
                return

        # ---------------- 工人池调度 ----------------
        # 生产者：候选流（多源轮询）→ 队列；消费者：N 个工人并行跑全流程。
        # 每个工人：独立 Chrome（配置目录 _wN 隔离）+ 独立辅助道（街景/查重）。
        n_workers = max(1, min(int(self.params.get("workers") or 1), 6))
        self.emit(f"工人池：{n_workers} 个工人并行处理候选"
                  + ("" if n_workers == 1 else "（每工人独立浏览器，任务栏无图标）"))
        # 队列容量必须够大：生产者翻一页要 8~120s，工人几秒就能吃光小队列。
        # 容量太小 → 工人大部分时间在等生产者翻页（实测队列=4 时工人大量空转）。
        # 60 条库存足够工人连续吃几分钟，生产者期间继续从容翻页补货。
        q: queue.Queue = queue.Queue(maxsize=60)
        producer_done = threading.Event()
        approved_lock = threading.Lock()
        inflight: dict = {}          # wid → 正在核查的 record_id（异常兜底改判用）

        def _produce():
            # 生产者也要独立浏览器命名空间 "_p"：数据源搜索接口的 Chrome 用
            # chrome_profile_p 目录，绝不与工人1的主配置 chrome_profile 撞车
            # （同一 user-data-dir 不允许两个 Chrome 实例，否则互相杀进程）。
            # Crexi 搜索接口不带登录态也能出候选（登录态只在详情页电话上有用，
            # 那由工人1的主配置负责）。
            browser.set_worker_ns("_p")
            try:
                for cand in candidate_stream:
                    if self.stopped or self._city_bad:
                        break
                    while not self.stopped:
                        try:
                            q.put(cand, timeout=2)
                            break
                        except queue.Full:
                            if approved >= target:
                                return
                    if approved >= target:
                        break
            finally:
                browser.close_current()
                producer_done.set()

        def _dbg(msg: str):
            """临时调试：工人生命周期写文件（排查用，之后删）。"""
            try:
                with open(config.DATA_DIR / "_workers_dbg.log", "a", encoding="utf-8") as f:
                    import datetime as _dt
                    f.write(f"{_dt.datetime.now():%H:%M:%S} {msg}\n")
            except Exception:
                pass

        def _worker(wid: int):
            self._wid_tls.wid = wid
            # 工人1 沿用主配置（保留 Crexi 登录态）；工人2+ 用独立配置目录 _wN
            browser.set_worker_ns(f"_w{wid}" if wid > 1 else "")
            try:
                while not self.stopped:
                    if approved >= target:
                        return
                    try:
                        cand = q.get(timeout=2)
                    except queue.Empty:
                        if producer_done.is_set():
                            return
                        continue
                    if cand is None:
                        return
                    _dbg(f"worker {wid} got: {cand.get('address', '?')[:50]}")
                    try:
                        _process_one(cand, wid)
                        _dbg(f"worker {wid} done: {cand.get('address', '?')[:50]}")
                    except Exception:
                        _dbg(f"worker {wid} EXC: {traceback.format_exc(limit=8)}")
                        err = traceback.format_exc(limit=6)
                        self.emit(f"候选处理异常，按淘汰兜底：{err}", "error")
                        rec = inflight.pop(wid, None)
                        if rec:
                            try:
                                store.update_record(rec, conclusion="不通过",
                                                    notes=f"处理异常：{err[:120]}")
                            except Exception:
                                pass
                    else:
                        inflight.pop(wid, None)
            finally:
                # 收尾：本工人没跑完的半成品记录改判不通过（不进人工队列）
                rec = inflight.pop(wid, None)
                if rec:
                    try:
                        store.update_record(rec, conclusion="不通过",
                                            notes="任务结束，该地址未完成核查")
                    except Exception:
                        pass
                browser.close_current()

        wths = []
        if n_workers == 1:
            # 单工人也走同一套队列机制（行为与旧串行版一致，只是日志带 [工人1]）
            wths.append(threading.Thread(target=_worker, args=(1,), daemon=True,
                                         name="worker-1"))
        else:
            for i in range(1, n_workers + 1):
                wths.append(threading.Thread(target=_worker, args=(i,), daemon=True,
                                             name=f"worker-{i}"))
        threading.Thread(target=_produce, daemon=True, name="producer").start()
        for t in wths:
            t.start()
        for t in wths:
            t.join()

        # 兜底：任何仍停留在"核查中"的流水线记录（异常中断/进程收尾）改判不通过
        try:
            store.flush_running_records()
        except Exception:
            pass

        scope_state = str(self.params.get("scope") or "").lower() == "state"
        city_tag = f"{city} " if scope_state else ""
        if approved >= target:
            self.emit(f"{city_tag}任务完成：通过 {approved}/{target} 个地址", "pass")
        elif self.stopped:
            self.emit(f"{city_tag}任务已终止：通过 {approved}/{target} 个地址", "warn")
        elif scope_state:
            # 全州模式：单城市没跑满很正常，交给外层切下一个城市
            self.emit(f"{city_tag}本城市候选已用尽，通过 {approved}/{target}，"
                      f"切换到下一个城市", "warn")
        else:
            self.emit(f"任务完成：候选已用尽，本轮通过 {approved}/{target} 个地址。"
                      f"可换城市/放宽价格区间再跑，或直接粘房源链接补搜", "warn")
        return approved


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


# ---------- 人工判定后的补充核查（步骤3/4） ----------
#
# 场景：记录缺步骤3/4（如人工在待人工页直接判了通过），判定后自动补跑
# 街景AI对比 + 查重，把档案补全。主核查任务运行期间不允许判定/补充核查
# （浏览器和 Smarty 额度都归主任务用，混跑会互相干扰）。

_RECHECK: dict = {"running": False, "record_id": None}


def task_running() -> bool:
    """是否有主核查任务还在跑（含收尾阶段）。"""
    return any(not r.done for r in RUNNERS.values())


def active_task() -> tuple:
    """当前运行中的任务 (task_id, runner)；没有运行任务时返回 ("", None)。

    页面刷新后前端靠它找回 task_id，重新连接 SSE 继续看进度/终止任务。"""
    for tid, r in RUNNERS.items():
        if not r.done:
            return tid, r
    return "", None


def recheck_status() -> dict:
    return dict(_RECHECK)


def recheck_steps_34(record_id: int) -> dict:
    """为缺步骤3/4 的记录补跑这两步（异步启动，立即返回）。

    结果处理：
      * 步骤3/4 无明确矛盾 → 保留人工结论，只补全档案；
      * 任一步明确 fail → 退回「待人工」并加注（人工判定时还没看到这两步结果，
        有明确矛盾必须让人重新确认，不能让带矛盾的记录留在"通过"里）。
    """
    if task_running():
        return {"ok": False, "message": "主核查任务进行中，暂不能补充核查，请等任务结束"}
    if _RECHECK["running"]:
        return {"ok": False, "message": "已有补充核查在进行，请稍候"}
    rec = store.get_record(record_id)
    if rec is None:
        return {"ok": False, "message": "记录不存在"}
    if rec.get("step3") and rec.get("step4"):
        return {"ok": True, "started": False, "message": "该记录已有步骤3/4，无需补充"}
    if not (rec.get("address") or "").strip():
        return {"ok": False, "message": "记录缺少地址，无法补充核查"}

    _RECHECK.update(running=True, record_id=record_id)

    def _work():
        try:
            addr = rec["address"].strip()
            url = rec.get("listing_url") or ""
            tag = f"r{record_id}"
            shots = [s for s in (rec.get("screenshots") or []) if s]
            # 房源照片：优先复用已下载的，没有再抓一次（给 AI 做外观比对用）
            photos = [s for s in shots if "photo" in s.lower()][:3]
            if url and not photos:
                try:
                    photos = streetview.grab_listing_photos(url, tag=tag) or []
                except Exception:
                    photos = []
            job = browser.submit(lambda: streetview.check_street_view(
                addr, url, tag=tag, photos=photos))
            try:
                r4 = opencorporates.check_duplicates(addr, tag=tag)
            except Exception as e:
                r4 = {"status": "suspect", "screenshot": "",
                      "message": f"查重步骤异常: {type(e).__name__}: {e}"}
            try:
                r3 = job.result(timeout=420)
            except Exception as e:
                r3 = {"status": "suspect", "screenshot": "", "extra_shots": [],
                      "message": f"街景步骤异常: {type(e).__name__}: {e}"}
            for s in [r3.get("screenshot", ""), *(r3.get("extra_shots") or []),
                      r4.get("screenshot", "")]:
                if s and s not in shots:
                    shots.append(s)
            step3 = f"{r3['status']}：{r3['message']}"
            step4 = f"{r4['status']}：{r4['message']}"
            updates: dict = {"step3": step3, "step4": step4, "screenshots": shots}
            if r3["status"] == "fail" or r4["status"] == "fail":
                old_note = rec.get("notes") or ""
                suffix = "人工判定后补充步骤3/4发现明确异常，已退回待人工，请重新复核"
                updates.update(conclusion="待人工", reviewed=0,
                               notes=f"{old_note}；{suffix}" if old_note else suffix)
            elif r3["status"] != "pass" or r4["status"] != "pass":
                # 补充核查技术失败不应让原人工通过继续保持通过，也不应伪装成视觉待人工。
                old_note = rec.get("notes") or ""
                suffix = "人工判定后补充步骤3/4未完成，本轮暂不保留通过结论"
                updates.update(conclusion="不通过", reviewed=0,
                               notes=f"{old_note}；{suffix}" if old_note else suffix)
            store.update_record(record_id, **updates)
        except Exception:
            traceback.print_exc()
        finally:
            # 本线程的 Chrome 必须关掉，否则占住配置目录影响下次任务
            browser.close_current()
            _RECHECK.update(running=False, record_id=None)

    threading.Thread(target=_work, daemon=True, name=f"recheck-{record_id}").start()
    return {"ok": True, "started": True, "message": "补充核查已开始"}
