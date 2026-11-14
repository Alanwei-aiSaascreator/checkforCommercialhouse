"""SOP 输出物：核查结果导出 Excel。

格式对齐用户样例（微信文件《（7个预算 2000 一个月）加洲.xlsx》）：
  列：编码 | 序号 | 地址 | 价格 | 面积 | 中介姓名 | 电话 | 网址 | smarty截图 | 街景截图 | 查重截图 | 邮箱
  smarty截图/街景截图/查重截图 直接内嵌图片（样例为 WPS DISPIMG，程序导出用 openpyxl 原生图片）。
  截图文件按文件名归类：*smarty* / *maps|streetview* / *opencorp*。
"""
import time
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import config

COLUMNS = ["编码", "序号", "地址", "价格", "面积/人数", "中介姓名", "电话",
           "网址", "smarty截图", "街景截图", "查重截图", "邮箱", "数据源"]

# 列宽（字符数；截图列略宽以容纳缩略图）
_WIDTHS = [8, 6, 42, 10, 9, 16, 15, 40, 16, 16, 16, 26, 12]
_IMG_W = 150  # 内嵌图显示宽度（像素）
_ROW_H = 100  # 截图行高（像素换算 pt：px * 0.75）


def _price_display(price: str, area: str) -> str:
    """价格列直接写「该套间/该房源一个月多少钱」。
    挂牌价常见 $16.00 SF/YR 这种每英尺单价，甲方看不懂；
    按 面积 × 每英尺单价 ÷ 12 折成月租显示（算不出则保留原文）。"""
    try:
        from .steps.loopnet import monthly_price

        monthly = monthly_price(price, area)
    except Exception:
        monthly = None
    if monthly is None:
        return price or ""
    return f"${monthly:,.0f}/月"


_SOURCE_NAMES = {
    "loopnet": "LoopNet",
    "crexi": "Crexi",
    "cityfeet": "CityFeet",
    "commercialcafe": "CommercialCafe",
    "commercialsearch": "CommercialSearch",
    "officespace": "OfficeSpace",
    "cbre": "CBRE",
}


def _source_display(source: str) -> str:
    """数据源列：7 个数据源都要有中文/品牌名（否则导出这列是空的）。"""
    s = (source or "").strip().lower()
    return _SOURCE_NAMES.get(s, s.upper() if s else "")


def _classify_shots(screenshots: list) -> tuple:
    """把记录的截图列表归到 smarty / 街景 / 查重 三类（各取第一张）。"""
    s_shot = sv_shot = oc_shot = ""
    for p in screenshots or []:
        low = Path(p).name.lower()
        if "smarty" in low and not s_shot:
            s_shot = p
        elif ("maps" in low or "streetview" in low) and not sv_shot:
            sv_shot = p
        elif "opencorp" in low and not oc_shot:
            oc_shot = p
    return s_shot, sv_shot, oc_shot


def _embed(ws, col_idx: int, row_idx: int, img_path: str):
    """把图片内嵌到 (row_idx, col_idx) 单元格，等比缩放到 _IMG_W 宽。"""
    if not img_path or not Path(img_path).is_file():
        ws.cell(row=row_idx, column=col_idx, value="截图缺失" if img_path else "")
        return
    try:
        img = XLImage(img_path)
        ratio = min(_IMG_W / img.width, 96 / max(img.height, 1))
        img.width = int(img.width * ratio)
        img.height = int(img.height * ratio)
        img.anchor = f"{get_column_letter(col_idx)}{row_idx}"
        ws.add_image(img)
    except Exception:
        ws.cell(row=row_idx, column=col_idx, value="截图加载失败")


def export_records(records: list, sheet_name: str = "核查通过记录",
                   out_path: str = "") -> str:
    """records：store.list_records() 过滤后的列表（顺序按导出先后）。返回 xlsx 路径。"""
    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_name or "核查记录")[:31]  # Excel sheet 名上限 31 字符

    header_font = Font(name="Arial", bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", start_color="0B6B4F")
    body_font = Font(name="Arial", size=11)
    center = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center", wrap_text=True)
    thin = Border(*[Side(style="thin", color="D1D5DB")] * 4)

    # 表头
    for c, name in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=c, value=name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = thin
        ws.column_dimensions[get_column_letter(c)].width = _WIDTHS[c - 1]
    ws.row_dimensions[1].height = 22

    for i, r in enumerate(records, 1):
        row = i + 1
        s_shot, sv_shot, oc_shot = _classify_shots(r.get("screenshots"))
        values = [
            "",                      # 编码（内部编码，留空由人工补）
            i,                       # 序号
            r.get("address", ""),
            _price_display(r.get("price", ""), r.get("area", "")),
            r.get("area", ""),
            r.get("agent", ""),
            r.get("phone", ""),
            r.get("listing_url", ""),
            "", "", "",              # 三个截图列由内嵌图片填充
            r.get("email", ""),
            _source_display(r.get("source", "")),
        ]
        for c, v in enumerate(values, 1):
            cell = ws.cell(row=row, column=c, value=v)
            cell.font = body_font
            cell.border = thin
            cell.alignment = left if c in (3, 8, 12) else center
        _embed(ws, 9, row, s_shot)
        _embed(ws, 10, row, sv_shot)
        _embed(ws, 11, row, oc_shot)
        ws.row_dimensions[row].height = _ROW_H * 0.75

    if not out_path:
        out_dir = config.DATA_DIR / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / f"核查导出_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
    wb.save(out_path)
    return out_path
