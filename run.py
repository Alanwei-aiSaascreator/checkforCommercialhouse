"""服务入口：uvicorn 启动 FastAPI（端口 8000）。

    .venv\\Scripts\\python.exe run.py

接口：
  GET  /                      前端页面
  GET  /api/records           核查记录列表（?conclusion=通过&keyword=xxx 可筛选）
  POST /api/records           手动新增核查记录
  PUT  /api/records/{id}      修改核查记录（字段可部分更新；改为"通过"自动写入收录表）
  DELETE /api/records/{id}    删除核查记录
  GET  /api/known             收录地址表
  POST /api/known             批量导入收录地址（{addresses:[...]} 或 {text:"一行一个"}）
  POST /api/tasks             新建核查任务 → {task_id}
  GET  /api/tasks/{id}/events SSE 任务日志（含截图事件）
  POST /api/tasks/{id}/stop   终止任务
  GET  /api/screenshot?path=  读取 data/screenshots 下的截图
  GET  /api/export?conclusion=导出核查结果 Excel（默认"通过"，12 列内嵌截图）
"""
import asyncio
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from server import store
from server import excel_export
from server.steps import smarty
from server.pipeline import (FEEDS, RUNNERS, start_task, task_running, active_task,
                             recheck_status, recheck_steps_34)

BASE = Path(__file__).resolve().parent
SHOTS = Path(store.config.SHOTS_DIR).resolve()

app = FastAPI(title="地址核查自动化")


# ---------- 页面 ----------

@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


# ---------- 核查记录 ----------

_EDITABLE_FIELDS = ("address", "price", "area", "agent", "phone", "email",
                    "listing_url", "conclusion", "notes", "reviewed")
_CONCLUSIONS = ("通过", "不通过", "待人工", "核查中")


@app.get("/api/records")
def api_records(conclusion: str = "", keyword: str = "", source: str = ""):
    return store.list_records(conclusion=conclusion.strip(), keyword=keyword.strip(),
                              source=source.strip().lower())


@app.get("/api/records/stats")
def api_records_stats(keyword: str = "", source: str = ""):
    """记录计数：各结论条数 + 总数 + 已人工复核数（不受结论筛选影响）。"""
    return store.records_stats(keyword=keyword.strip(), source=source.strip().lower())


@app.post("/api/email-enrich")
def api_email_enrich(body: dict):
    """邮箱旁路反查（手动触发，独立于主线流水线，不占浏览器/Akamai）。

    body: {"limit": 50, "min_conf": 50, "dry_run": false}
    """
    if not store.config.APOLLO_API_KEY and not store.config.HUNTER_API_KEY:
        return {"ok": False, "error": "未配置邮箱反查 key（data/config.json 的 apollo_api_key 或 hunter_api_key）"}
    from server.email_enrich import find_email, is_missing

    limit = int(body.get("limit") or 50)
    min_conf = int(body.get("min_conf") or store.config.EMAIL_ENRICH_MIN_CONF)
    dry_run = bool(body.get("dry_run"))

    recs = store.list_records(conclusion="通过")
    todo = [r for r in recs if is_missing(r.get("email")) and (r.get("agent") or "").strip()]
    todo.sort(key=lambda r: r.get("id") or 0)

    hit = miss = skip = 0
    done = []
    for r in todo[:limit]:
        email, score, status = find_email(r.get("agent"))
        if status == "hit" and score >= min_conf:
            hit += 1
            if not dry_run:
                store.update_record(r["id"], email=email)
            done.append({"id": r["id"], "agent": r.get("agent"), "email": email, "score": score})
        elif status == "miss":
            miss += 1
        else:
            skip += 1
    return {"ok": True, "pending": len(todo), "hit": hit, "miss": miss,
            "skip": skip, "dry_run": dry_run, "done": done}


@app.post("/api/records")
def api_record_create(body: dict):
    addr = (body.get("address") or "").strip()
    if not addr:
        raise HTTPException(400, "地址必填")
    conclusion = (body.get("conclusion") or "待人工").strip()
    if conclusion not in _CONCLUSIONS:
        raise HTTPException(400, f"conclusion 只能是 {'/'.join(_CONCLUSIONS)}")
    rid = store.insert_record({
        "address": addr,
        "price": (body.get("price") or "").strip(),
        "agent": (body.get("agent") or "").strip(),
        "phone": (body.get("phone") or "").strip(),
        "email": (body.get("email") or "").strip(),
        "area": (body.get("area") or "").strip(),
        "listing_url": (body.get("listing_url") or "").strip(),
        "step1": (body.get("step1") or "pass：手动录入"),
        "screenshots": [],
        "notes": (body.get("notes") or "").strip(),
        "conclusion": conclusion,
    })
    if conclusion == "通过":
        store.import_known_addresses([addr], source="manual")
    return {"ok": True, "id": rid}


@app.put("/api/records/{record_id}")
def api_record_update(record_id: int, body: dict):
    old = store.get_record(record_id)
    if old is None:
        raise HTTPException(404, "记录不存在")
    fields = {}
    for k in _EDITABLE_FIELDS:
        if k in body:
            v = body[k]
            if v is None:
                v = ""
            v = str(v).strip() if k != "reviewed" else int(bool(v))
            fields[k] = v
    conclusion = fields.get("conclusion")
    if conclusion is not None and conclusion not in _CONCLUSIONS:
        raise HTTPException(400, f"conclusion 只能是 {'/'.join(_CONCLUSIONS)}")
    addr = fields.get("address", old.get("address", ""))
    if "address" in fields and not addr:
        raise HTTPException(400, "地址不能为空")
    if not fields:
        raise HTTPException(400, "没有可更新的字段")
    # “通过”本身只是人工标注；是否进入步骤3/4由前端单独询问，
    # 因此主任务运行时仍可以先标注通过，但不能启动补充核查。
    # conclusion 手动改为"通过" → 同步写入收录表（与流水线行为一致）；
    # 未传 address 时用记录现有地址
    if conclusion == "通过" and addr:
        store.import_known_addresses([addr], source="manual")
    store.update_record(record_id, **fields)
    return {"ok": True}


@app.delete("/api/records/{record_id}")
def api_record_delete(record_id: int):
    if not store.delete_record(record_id):
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


# ---------- 运行状态 / 补充核查 ----------

@app.get("/api/debug/last_task")
def api_debug_last_task(tid: str = "", keyword: str = "交叉"):
    """临时调试：返回任务日志历史（排查交叉查询/工人池用）。

    tid 空 = 列出全部任务摘要；keyword 为空返回全部日志尾部，否则只返回匹配行。"""
    if not tid:
        return {"tasks": [{"task_id": k, "done": v.done,
                           "history_len": len(v.history)} for k, v in RUNNERS.items()]}
    r = RUNNERS.get(tid)
    if r is None:
        raise HTTPException(404, "task not found")
    hist = r.history
    if keyword:
        hist = [e for e in hist if keyword in e.get("message", "")]
    return {"task_id": tid, "done": r.done, "stopped": r.stopped,
            "history_len": len(r.history), "matches": len(hist),
            "tail": hist[:60]}


@app.get("/api/status")
def api_status():
    """主任务与补充核查的运行状态（前端用它禁止任务进行中的人工判定）。

    附带当前任务 id 与步骤：页面刷新后靠它恢复运行界面、重连 SSE。"""
    st = recheck_status()
    tid, runner = active_task()
    p = (getattr(runner, "params", None) or {}) if runner else {}
    return {"task_running": task_running(), "task_id": tid,
            "current_step": getattr(runner, "current_step", "") or "",
            # ---- 进度面板用：时间 + 计数 + 每个工人的实时状态 ----
            "started_at": getattr(runner, "started_at", 0.0) or 0.0,
            "approved": getattr(runner, "approved", 0) or 0,
            "processed": getattr(runner, "processed", 0) or 0,
            "target": int(p.get("target") or 0),
            "workers_n": int(p.get("workers") or 0),
            "city": p.get("city") or "", "state": p.get("state") or "",
            "price_min": p.get("price_min") or 0, "price_max": p.get("price_max") or 0,
            "workers": {str(k): v for k, v in (getattr(runner, "worker_stats", {}) or {}).items()},
            "recheck_running": st.get("running", False),
            "recheck_record_id": st.get("record_id")}


@app.post("/api/records/{record_id}/recheck34")
def api_record_recheck34(record_id: int):
    """人工判定后补跑步骤3/4（街景AI对比 + 查重），异步执行。"""
    if task_running():
        raise HTTPException(409, "主核查任务进行中，暂不能补充核查，请等任务结束")
    r = recheck_steps_34(record_id)
    if not r.get("ok"):
        raise HTTPException(409, r.get("message", "无法开始补充核查"))
    return r


# ---------- 历史清理 ----------

@app.get("/api/history/stats")
def api_history_stats():
    """各历史表条数（清空前确认）。"""
    return store.history_stats()


@app.post("/api/history/clear")
def api_history_clear(body: dict):
    """清空核查记录 + 失败档案；include_known/include_smarty_cache 可选。"""
    out = store.clear_history(
        include_known=bool(body.get("include_known")),
        include_smarty_cache=bool(body.get("include_smarty_cache")),
    )
    return {"ok": True, **out}


# ---------- 调试 ----------

@app.post("/api/debug/mine-suites")
def api_debug_mine_suites(body: dict):
    """调试：测试大楼房间号挖掘（Smarty API → 官网部件，调用生产函数）。"""
    address = (body.get("address") or "").strip()
    if not address:
        raise HTTPException(400, "address 必填")
    api = smarty.api_mine_suites(address, max_suites=6)
    if api is not None:
        return {"source": "api", "suites": api}
    suites = smarty.web_mine_suites(address, max_suites=6)
    return {"source": "web-widget", "suites": suites,
            "error": smarty._web_mine_last_error}


# ---------- 收录地址表 ----------

@app.get("/api/known")
def api_known():
    return store.list_known()


@app.post("/api/known")
def api_known_import(body: dict):
    items = list(body.get("addresses") or [])
    text = body.get("text") or ""
    if text:
        items += text.replace("\r", "").split("\n")
    items = [i.strip() for i in items if i and i.strip()]
    if not items:
        raise HTTPException(400, "没有可导入的地址")
    added = store.import_known_addresses(items, source="manual")
    return {"ok": True, "added": added}


@app.put("/api/known/{kid}")
def api_known_update(kid: int, body: dict):
    status = store.update_known_address(kid, body.get("address") or "")
    if status == "empty":
        raise HTTPException(400, "地址必填")
    if status == "duplicate":
        raise HTTPException(400, "该地址已存在于收录表（归一化后重复）")
    if status == "notfound":
        raise HTTPException(404, "收录地址不存在")
    return {"ok": True}


@app.delete("/api/known/{kid}")
def api_known_delete(kid: int):
    if not store.remove_known_by_id(kid):
        raise HTTPException(404, "收录地址不存在")
    return {"ok": True}


@app.post("/api/known/batch_delete")
def api_known_batch_delete(body: dict):
    deleted = store.remove_known_ids(body.get("ids") or [])
    if not deleted:
        raise HTTPException(400, "没有可删除的条目")
    return {"ok": True, "deleted": deleted}


@app.post("/api/known/batch_update")
def api_known_batch_update(body: dict):
    r = store.update_known_batch(body.get("items") or [])
    if r["ok"] == 0 and (r["duplicate"] or r["empty"] or r["notfound"]):
        detail = []
        if r["duplicate"]:
            detail.append(f"{r['duplicate']} 条与其他地址重复")
        if r["empty"]:
            detail.append(f"{r['empty']} 条地址为空")
        if r["notfound"]:
            detail.append(f"{r['notfound']} 条 ID 不存在")
        raise HTTPException(400, "；".join(detail))
    return {"ok": True, **r}


@app.post("/api/known/import_excel")
async def api_known_import_excel(file: UploadFile = File(...)):
    """导入 Excel 收录地址（.xlsx）。

    解析规则：逐行逐格扫描，凡是像地址的文本（含逗号或数字开头，长度 ≥8）
    都收进来；表头/备注等非地址文本会被过滤。与流水线同一套归一化去重。"""
    import io
    import re as _re
    import openpyxl

    data = await file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    if len(data) > 4 and data[:4] != b"PK\x03\x04":
        raise HTTPException(400, "只支持 .xlsx 格式（旧版 .xls 请先另存为 .xlsx）")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise HTTPException(400, f"Excel 读取失败: {type(e).__name__}")
    addrs = []
    try:
        for row in wb.active.iter_rows(values_only=True):
            for cell in row:
                if not isinstance(cell, str):
                    continue
                s = cell.strip()
                if len(s) < 8:
                    continue
                if _re.match(r"^\d", s) and ("," in s or " " in s):
                    addrs.append(s)
                elif _re.match(r"^[A-Z0-9]", s) and s.count(",") >= 2:
                    addrs.append(s)   # "Ste 302A, 1150 N State St, Chicago, IL" 类倒装
    finally:
        wb.close()
    if not addrs:
        raise HTTPException(400, "Excel 里没有解析到地址（需要一行一个地址的文本列）")
    added = store.import_known_addresses(addrs, source="excel")
    return {"ok": True, "total": len(addrs), "added": added}


@app.get("/api/known/export")
def api_known_export():
    """导出收录地址表 Excel（编号 + 地址两列，按 ID 倒序与页面一致）。"""
    import io
    import urllib.parse
    import openpyxl

    items = store.list_known()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "收录地址表"
    ws.append(["编号", "地址"])
    for k in items:
        ws.append([k["id"], k["raw"]])
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 60
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = urllib.parse.quote("收录地址表.xlsx")
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})


# ---------- 任务 ----------

@app.post("/api/tasks")
def api_task_start(body: dict):
    # 数据源支持多选："loopnet" 或 "loopnet,crexi,..."（流水线按页轮询汇流）
    raw = str(body.get("source") or "loopnet")
    sel = [s.strip().lower() for s in raw.split(",") if s.strip()] or ["loopnet"]
    bad = [s for s in sel if s not in FEEDS]
    if bad:
        raise HTTPException(400, "未知数据源：" + ",".join(bad)
                            + "（可选：" + " / ".join(FEEDS.keys()) + "）")
    params = {
        "city": (body.get("city") or "").strip(),
        "state": (body.get("state") or "").strip().upper(),
        "price_min": body.get("price_min") or 0,
        "price_max": body.get("price_max") or 2000,
        "target": min(int(body.get("target") or 3), 100),
        "workers": min(max(int(body.get("workers") or 1), 1), 6),
        "listing_urls": body.get("listing_urls") or "",
        "source": ",".join(sel),
    }
    if not params["city"] or not params["state"]:
        raise HTTPException(400, "城市和州缩写必填")
    task_id = uuid.uuid4().hex[:8]
    start_task(task_id, params)
    return {"task_id": task_id}


@app.get("/api/tasks/{task_id}/events")
def api_task_events(task_id: str):
    runner = RUNNERS.get(task_id)
    if runner is None:
        raise HTTPException(404, "任务不存在")

    def gen():
        # 基于日志历史的游标推送：新连接先回放 history（刷新页面后能看到
        # 之前的进度），再等新日志；任务完成且日志发完即结束。
        idx = 0
        while True:
            hist = runner.history
            while idx < len(hist):
                ev = hist[idx]
                idx += 1
                if ev.get("__done__"):
                    yield "event: done\ndata: {}\n\n"
                    return
                if "__screenshot__" in ev:
                    yield "event: screenshot\n" + "data: " + _json(ev["__screenshot__"]) + "\n\n"
                    continue
                yield "data: " + _json(ev) + "\n\n"
            if runner.done and idx >= len(runner.history):
                yield "event: done\ndata: {}\n\n"
                return
            time.sleep(0.4)   # 轮询日志历史，事件流低频足够

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/tasks/{task_id}/stop")
def api_task_stop(task_id: str):
    runner = RUNNERS.get(task_id)
    if runner is None:
        raise HTTPException(404, "任务不存在")
    runner.stop()
    return {"ok": True}


# ---------- 截图 ----------

@app.get("/api/screenshot")
def api_screenshot(path: str):
    p = Path(path).resolve()
    if not str(p).startswith(str(SHOTS)) or not p.is_file():
        raise HTTPException(404, "截图不存在")
    return FileResponse(p)


# ---------- Excel 导出（SOP 输出物） ----------

@app.get("/api/export")
def api_export(conclusion: str = "通过", sheet: str = "", source: str = "", ids: str = ""):
    """导出核查结果 Excel（12 列 + 数据源列 + 内嵌 smarty/街景/查重截图）。

    ids 可选：指定要导出的记录 id（逗号分隔，如 "178,180"）——此时忽略 conclusion
    与 source，按给定顺序导出（记录页「导出」按钮/勾选批量导出走这条）；
    sheet 可选：自定义 Sheet 名（如 "加洲 2000一个月"）；
    source 可选：只导出某条线（loopnet / crexi）。
    """
    picked = []
    for x in (ids or "").replace(" ", "").split(","):
        if x.isdigit():
            picked.append(int(x))
    if picked:
        by_id = {r["id"]: r for r in store.list_records()}
        recs = [by_id[i] for i in picked if i in by_id]
        if not recs:
            raise HTTPException(404, "所选记录不存在（可能已被删除）")
        default_sheet = "核查记录" if len(recs) > 1 else "核查记录1条"
    else:
        src = source.strip().lower()
        recs = [r for r in store.list_records(source=src) if r.get("conclusion") == conclusion]
        if not recs:
            who = {"crexi": "（Crexi 线）", "loopnet": "（LoopNet 线）"}.get(src, "")
            raise HTTPException(404, f"没有结论为「{conclusion}」的核查记录{who}")
        recs.reverse()  # list_records 为倒序，导出按时间正序
        default_sheet = f"核查{conclusion}记录"
    path = excel_export.export_records(recs, sheet_name=sheet or default_sheet)
    return FileResponse(path, filename=Path(path).name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
