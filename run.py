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
import queue
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from server import store
from server import excel_export
from server.steps import smarty
from server.pipeline import FEEDS, RUNNERS, start_task

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
_CONCLUSIONS = ("通过", "不通过", "待人工")


@app.get("/api/records")
def api_records(conclusion: str = "", keyword: str = "", source: str = ""):
    return store.list_records(conclusion=conclusion.strip(), keyword=keyword.strip(),
                              source=source.strip().lower())


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


# ---------- 任务 ----------

@app.post("/api/tasks")
def api_task_start(body: dict):
    source = (body.get("source") or "loopnet").strip().lower()
    if source not in FEEDS:
        raise HTTPException(400, "source 只能是 " + " / ".join(FEEDS.keys()))
    params = {
        "city": (body.get("city") or "").strip(),
        "state": (body.get("state") or "").strip().upper(),
        "price_min": body.get("price_min") or 0,
        "price_max": body.get("price_max") or 2000,
        "target": body.get("target") or 3,
        "listing_urls": body.get("listing_urls") or "",
        "source": source,
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
        while True:
            try:
                ev = runner.events.get(timeout=15)
            except queue.Empty:
                if runner.done:
                    yield "event: done\ndata: {}\n\n"
                    return
                yield ": ping\n\n"   # 心跳，防代理断连
                continue
            if ev.get("__done__"):
                yield "event: done\ndata: {}\n\n"
                return
            if "__screenshot__" in ev:
                yield "event: screenshot\n" + "data: " + _json(ev["__screenshot__"]) + "\n\n"
                continue
            yield "data: " + _json(ev) + "\n\n"

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
