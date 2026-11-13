"""仿真测试：除 LoopNet 外 6 个数据源，各筛出 5 个符合条件（Nashville, TN / ≤$2,500/月）。"""
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import browser                                      # noqa: E402
from server.steps import (cbre, cityfeet, commercialcafe,        # noqa: E402
                          commercialsearch, crexi, officespace)
from server.steps.loopnet import monthly_price                   # noqa: E402

CITY, STATE, PMAX, AMIN, NEED = "Nashville", "TN", 2500, 200, 5
SOURCES = [("Crexi", crexi), ("CityFeet", cityfeet),
           ("CommercialCafe", commercialcafe), ("CommercialSearch", commercialsearch),
           ("OfficeSpace", officespace), ("CBRE", cbre)]


def monthly_of(cand, spaces):
    ms = [s["monthly"] for s in spaces if s.get("monthly")]
    if ms:
        return min(ms)
    price, area = cand.get("price") or "", cand.get("area") or ""
    up = price.upper().replace(",", "")
    # "$1,000/MO" 这类本身就是月租（executive suite 常按套/按人计价，没有 SF）
    if up and "SF" not in up and ("/MO" in up or "/MONTH" in up):
        m = re.search(r"\$(\d+(?:\.\d+)?)", up)
        if m:
            return float(m.group(1))
    if price and str(area).isdigit():
        return monthly_price(price, area)
    return None


report = []
try:
    for name, mod in SOURCES:
        t0 = time.time()
        problems, cands = [], []
        for page in (1, 2):
            try:
                r = mod.search_candidates(CITY, STATE, 0, PMAX, page=page)
            except Exception as e:
                problems.append(f"第{page}页异常 {str(e)[:60]}")
                break
            if r.get("status") != "pass":
                problems.append(f"第{page}页 {r.get('message', '')[:60]}")
            got = r.get("candidates") or []
            cands += got
            print(f"  [{name}] 第{page}页 {len(got)} 条 | {r.get('message', '')[:60]}", flush=True)
            if not got or len(cands) >= 120:
                break

        seen, uniq = set(), []
        for c in cands:
            key = re.sub(r"[^a-z0-9]+", "", (c.get("address") or "").lower())
            if not key or key in seen:
                continue
            seen.add(key)
            uniq.append(c)

        hits, detail_budget = [], (12 if name == "CityFeet" else 0)
        for c in uniq:
            if len(hits) >= NEED:
                break
            spaces = c.get("spaces") or []
            if not spaces and detail_budget > 0 and c.get("listing_url"):
                detail_budget -= 1
                try:
                    info = mod.fetch_broker_info(c["listing_url"])
                    spaces = info.get("spaces") or []
                    c["price"] = c.get("price") or info.get("price", "")
                    c["area"] = c.get("area") or info.get("area", "")
                    c["agent"] = c.get("agent") or info.get("agent", "")
                except Exception as e:
                    problems.append(f"详情失败 {str(e)[:40]}")
            m = monthly_of(c, spaces)
            if m is None or m > PMAX:
                continue
            area = c.get("area") or ""
            priced = [x for x in spaces if x.get("monthly")]
            if priced:
                s = min(priced, key=lambda x: x["monthly"])
                area = s.get("size") or area
            if area and str(area).isdigit() and int(area) < AMIN:
                continue
            hits.append((c.get("address", ""), m, area, c.get("agent", "")))

        cost = round(time.time() - t0, 1)
        report.append((name, len(uniq), len(hits), cost, problems, hits))
        print(f"=== {name}: 候选 {len(uniq)} | 命中 {len(hits)}/{NEED} | 耗时 {cost}s "
              f"| 问题 {len(problems)}", flush=True)
        for h in hits:
            print(f"    ✓ {h[0][:52]} | ${h[1]:,.0f}/月 | {h[2]} SF | {h[3][:26]}", flush=True)
        for p in dict.fromkeys(problems):
            print(f"    ! {p}", flush=True)
finally:
    try:
        browser.close_current()
    except Exception:
        pass

print("\n\n===== 汇总 =====", flush=True)
for name, n_c, n_h, cost, problems, _ in report:
    print(f"{name:17s} 候选 {n_c:3d} | 命中 {n_h}/{NEED} | {cost:6.1f}s | 问题 {len(set(problems))}",
          flush=True)
