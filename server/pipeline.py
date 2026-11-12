"""核查流水线：查重前置 → 步骤1-4 → 结论归档。"""
import queue
import random
import threading
import traceback
from datetime import datetime

from . import config, deepseek_client, store
from .steps import loopnet, smarty, streetview, opencorporates


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
        known_count = store.count_known()
        self.emit(f"开始核查：{city}, {state}，价格 ${price_min}-${price_max}/月，目标 {target} 个地址；收录表共 {known_count} 条")

        manual_urls = [u for u in (p.get("listing_urls") or "").replace("\r", "").split("\n") if u.strip()]
        approved = 0
        seen_addrs: set = set()

        def _auto_candidate_stream():
            """自动搜索：先访问第 1 页探测真实总页数，再从总页数里随机抽页，
            每页先把"已收录/失败档案"的地址剔除，只产出全新候选。

            为什么随机抽：顺序翻页（1/2/3...）永远落在历史核查过的前几页，
            整页都是已收录地址 → 实际有效页数极少、目标数量跑不满（用户反馈）。
            随机抽样把撞到旧页的概率按比例摊薄。
            总访问页数上限 = 单轮页数 × 2，避免把整池页数扫空。
            """
            per_page = max(target * 6, 12)
            batch = max(config.LOOPNET_MAX_PAGES, 1)      # 每轮随机抽几页
            max_visits = batch * 2                        # 单个任务总访问页数上限
            used: set = set()
            visits = 0
            total_pages = 0
            plan = [1]                                    # 第 1 页顺带探测总页数
            while not self.stopped and approved < target and visits < max_visits:
                if not plan:
                    pool = total_pages or max(config.LOOPNET_PAGE_POOL, 1)
                    remaining = [p for p in range(1, pool + 1) if p not in used]
                    if not remaining:
                        return
                    plan = random.sample(remaining, min(batch, len(remaining)))
                    self.emit(f"步骤1：LoopNet 随机抽 {len(plan)} 页（共 {pool} 页）："
                              + "、".join(f"第{p}页" for p in plan)
                              + f"，每页上限 {per_page}")
                produced = 0
                for page_no in list(plan):
                    if self.stopped or approved >= target or visits >= max_visits:
                        return
                    used.add(page_no)
                    visits += 1
                    self.emit(f"步骤1：LoopNet 采集候选（第 {page_no}/{total_pages or '?'} 页，"
                              f"已访问 {visits}/{max_visits}）...")
                    r1 = loopnet.search_candidates(city, state, price_min, price_max,
                                                   limit=per_page, tag=f"task{self.task_id}",
                                                   page=page_no)
                    self.emit(f"步骤1结果（第{page_no}页）：{r1['message']}",
                              "pass" if r1["status"] == "pass" else "warn")
                    if visits == 1 and r1["screenshot"]:
                        self.events.put({"__screenshot__": {"step": "step1", "path": r1["screenshot"]}})
                    if not total_pages and r1.get("total_pages"):
                        total_pages = int(r1["total_pages"])
                        self.emit(f"  LoopNet 该搜索条件下共 {total_pages} 页（约 40 条/页）")
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

        if manual_urls:
            self.emit(f"检测到 {len(manual_urls)} 条手动房源链接，跳过自动搜索（反爬兜底模式）...")
            r1 = loopnet.candidates_from_urls(manual_urls)
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
            addr = cand["address"]
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

            record_id = store.insert_record({
                "address": addr, "price": cand.get("price", ""),
                "agent": cand.get("agent", ""), "phone": cand.get("phone", ""),
                "email": cand.get("email", ""), "area": cand.get("area", ""),
                "listing_url": cand.get("listing_url", ""),
                "step1": f"pass：LoopNet 采集（价格 {cand.get('price', '未知')}）",
                "screenshots": [],  # SOP：LoopNet 搜索页截图不入档
            })
            shots = []

            # SOP：价格/中介姓名/联系方式/面积都是硬性字段——搜索卡片缺哪个就去详情页补抓
            if (not cand.get("agent") or not cand.get("phone")
                    or not cand.get("email") or not cand.get("price")
                    or not cand.get("area")):
                self.emit("补抓价格/经纪人姓名/电话/邮箱/面积（详情页）...")
                info = loopnet.fetch_broker_info(cand.get("listing_url", ""))
                for k in ("agent", "phone", "email", "price", "area"):
                    if info.get(k) and not cand.get(k):
                        cand[k] = info[k]
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

            # 步骤1.5 办公房源预筛（SOP 补充：明显非办公房源直接不用，360 全景图优先）
            if self.stopped:
                break
            photos = []
            if cand.get("listing_url"):
                self.emit("下载房源照片并做办公适用性 AI 预筛...")
                photos = streetview.grab_listing_photos(cand["listing_url"], tag=f"r{record_id}")
                if photos:
                    self.emit(f"  已下载 {len(photos)} 张房源照片（360/全景图优先）")
                    shots.extend(photos[:3])  # LoopNet 房源图入档（记录页展示用）
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

            # SOP 常见问题：缺房间号 → 四级挖号（Smarty API→Smarty官网部件→LoopNet线索→常见编号）
            msg_low = r2["message"].lower()
            needs_suite = ("missing" in msg_low or "缺房间号" in r2["message"] or "n1" in msg_low)
            if r2["status"] == "fail" and needs_suite:
                self.emit("Smarty 提示缺房间号，在 Smarty 中挖掘该楼房间号...", "warn")
                # 房间号只从 Smarty 挖（用户要求）：官方 API（订阅后自动生效）
                # → Smarty 官网部件（免费、不耗 API 额度）。LoopNet 不参与挖号。
                suites = smarty.api_mine_suites(addr, max_suites=4)
                if suites is None:
                    self.emit("  API 未订阅房间号枚举，改用 Smarty 官网部件查询该楼合法房间号...", "warn")
                    suites = smarty.web_mine_suites(addr, max_suites=4)
                elif suites:
                    self.emit(f"  Smarty API 返回 {len(suites)} 个房间号")
                merged = list(suites or [])[:4]
                if merged:
                    self.emit(f"  Smarty 挖到 {len(merged)} 个房间号，逐一验证："
                              + "、".join(s.split(" Ste ")[-1].split(",")[0] for s in merged))
                else:
                    self.emit("  Smarty 未能枚举到该楼房间号 → 该地址淘汰", "warn")
                suite_pass = None
                for s_addr in merged:
                    if self.stopped:
                        break
                    self.emit(f"  验证 {s_addr}")
                    r2s = smarty.verify_address(s_addr, tag=f"r{record_id}")
                    shots.append(r2s.get("screenshot", ""))
                    if r2s["status"] == "quota":
                        self.emit(r2s["message"], "error")
                        self.stopped = True
                        break
                    if r2s["status"] == "pass":
                        suite_pass = s_addr
                        self.emit(f"  ✓ Suite 地址双绿通过：{s_addr}", "pass")
                        break
                    self.emit(f"  ✗ {r2s['message'][:60]}")
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
                    note = f"Smarty 未通过（含 Suite 尝试）：{r2['message'][:80]}"
                    store.update_record(record_id, conclusion="不通过",
                                        notes=note, screenshots=shots)
                    store.add_failed_address(addr, "Smarty", note, building=True)
                    self.emit("该地址所有 Suite 均未通过，淘汰，处理下一个候选", "warn")
                    continue
            elif r2["status"] == "fail":
                note = f"Smarty 未通过：{r2['message'][:80]}"
                store.update_record(record_id, conclusion="不通过", notes=note)
                # 楼栋级登记：空置/后缀变化/无效等属楼栋属性，下次无房号候选直接跳过
                store.add_failed_address(addr, "Smarty", note, building=True)
                self.emit("该地址淘汰，处理下一个候选", "warn")
                continue
            if r2["status"] == "suspect":
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
            if self.stopped:
                break
            self.emit("步骤3：Google Maps 街景 + DeepSeek 视觉对比...")
            r3 = streetview.check_street_view(check_addr, cand.get("listing_url", ""),
                                              tag=f"r{record_id}", photos=photos)
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

            # 步骤4 OpenCorporates
            if self.stopped:
                break
            self.emit("步骤4：OpenCorporates 查重...")
            r4 = opencorporates.check_duplicates(check_addr, tag=f"r{record_id}")
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
    # 用独立线程跑流水线（而非事件循环的 executor）：executor 线程池被 uvicorn
    # 复用后可能带事件循环上下文，Playwright Sync API 会报
    # "using Sync API inside the asyncio loop"。全新线程则干净无环。
    threading.Thread(target=runner.run, daemon=True, name=f"task-{task_id}").start()
    return runner
