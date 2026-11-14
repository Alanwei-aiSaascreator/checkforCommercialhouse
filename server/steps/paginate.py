"""列表页「当前页码」识别 —— 所有数据源共用。

为什么需要这层：多数房源站在页码越界时不报错、也不返回空页，而是把**第 1 页
原样再发一遍**（CommercialCafe 实测 Chicago 办公室从第 17 页起全部回落成第 1 页）。
上层如果只按「这一页有没有候选」来判断，就会在越界页上无限空转：日志一直在
"换页"，浏览器里却始终是同一个列表页。

判定方式只有一个可靠信号：请求的页码 vs 页面上「当前页」标记是否一致。
"""

# 尽量只认分页容器里的高亮项，避免误读页面上别的 .active
_MARKER_JS = r"""() => {
    const sels = ['[aria-current="page"]', '[aria-current="true"]',
                  '.pagination .active', '.pagination .current', '.pagination__link--active',
                  '.page-item.active', '.pager .current', '.pager .active',
                  '[class*="agination"] [class*="active"]',
                  '[class*="agination"] [class*="current"]'];
    for (const s of sels) {
        const els = document.querySelectorAll(s);
        for (const el of els) {
            const vals = [el.innerText || "", el.textContent || "",
                          el.getAttribute("href") || "", el.getAttribute("data-page") || "",
                          el.getAttribute("aria-label") || ""];
            for (const v of vals) {
                const m = /(\d+)/.exec(v || "");
                if (m) return +m[1];
            }
        }
    }
    // 兜底：分页条文案 "Page 4 of 16" / "Showing Results 1 - 40, Page 4 of 19"
    const body = document.body.innerText || "";
    const m = /page\s*(\d+)\s*(?:of|\/)\s*\d+/i.exec(body);
    return m ? +m[1] : 0;
}"""


def _js_quote(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def current_page(page) -> int:
    """页面上的当前页码；读不到返回 0（不代表第 0 页，代表"没有标记"）。"""
    try:
        return int(page.evaluate(_MARKER_JS) or 0)
    except Exception:
        return 0


def check(page, page_no: int) -> dict:
    """返回 {"page_ok": True/False/None, "actual": int}。

    page_ok=None 表示站点没给可用的页码标记 → 上层不要据此下结论。
    """
    actual = current_page(page)
    if not actual:
        return {"page_ok": None, "actual": 0}
    return {"page_ok": actual == int(page_no), "actual": actual}


def out_of_range(page_no: int, actual: int) -> dict:
    """统一的「页码越界」返回结构（各数据源 message 自己拼）。"""
    return {"status": "warn", "not_found": True, "page_ok": False,
            "candidates": [], "total_pages": 0,
            "message": f"请求第 {page_no} 页、站点实际给的是第 {actual} 页（越界回落）"}
