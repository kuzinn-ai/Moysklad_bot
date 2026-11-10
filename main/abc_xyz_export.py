"""Create a compact, formula-free ABC-XYZ workbook from one saved snapshot."""

from io import BytesIO
from datetime import datetime
from math import isfinite
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT = "http://schemas.openxmlformats.org/package/2006/content-types"
ET.register_namespace("", MAIN)
ET.register_namespace("r", REL)


def _xml(element):
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def _column(index):
    name = ""
    while index:
        index, digit = divmod(index - 1, 26)
        name = chr(65 + digit) + name
    return name


def _cell(row, column, value, style=0):
    attributes = {"r": f"{_column(column)}{row}"}
    if style:
        attributes["s"] = str(style)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value):
        cell = ET.Element(f"{{{MAIN}}}c", attributes)
        ET.SubElement(cell, f"{{{MAIN}}}v").text = str(value)
    else:
        cell = ET.Element(f"{{{MAIN}}}c", {**attributes, "t": "inlineStr"})
        inline = ET.SubElement(cell, f"{{{MAIN}}}is")
        ET.SubElement(inline, f"{{{MAIN}}}t").text = "" if value is None else str(value)
    return cell


def _sheet(rows, widths, filter_row=None, freeze_row=None):
    root = ET.Element(f"{{{MAIN}}}worksheet")
    views = ET.SubElement(root, f"{{{MAIN}}}sheetViews")
    view = ET.SubElement(views, f"{{{MAIN}}}sheetView", {"workbookViewId": "0"})
    if freeze_row:
        ET.SubElement(view, f"{{{MAIN}}}pane", {"ySplit": str(freeze_row), "topLeftCell": f"A{freeze_row + 1}", "activePane": "bottomLeft", "state": "frozen"})
    columns = ET.SubElement(root, f"{{{MAIN}}}cols")
    for index, width in enumerate(widths, 1):
        ET.SubElement(columns, f"{{{MAIN}}}col", {"min": str(index), "max": str(index), "width": str(width), "customWidth": "1"})
    sheet_data = ET.SubElement(root, f"{{{MAIN}}}sheetData")
    for number, values in enumerate(rows, 1):
        row = ET.SubElement(sheet_data, f"{{{MAIN}}}row", {"r": str(number)})
        for index, value in enumerate(values, 1):
            style = 1 if number == filter_row else 2 if isinstance(value, (int, float)) and index == 3 and len(widths) > 2 else 0
            row.append(_cell(number, index, value, style))
    if filter_row:
        ET.SubElement(root, f"{{{MAIN}}}autoFilter", {"ref": f"A{filter_row}:{_column(len(widths))}{len(rows)}"})
    return _xml(root)


def build_abc_xyz_xlsx(snapshot):
    """Return XLSX bytes; all user-provided names are literal text, never formulas."""
    items = snapshot["items"]
    counts = snapshot.get("counts", {})
    def display_date(value):
        return ".".join(reversed(value.split("-")))

    period = f'{display_date(snapshot["period_from"])} — {display_date(snapshot["period_to"])}'
    calculated_at = snapshot.get("calculated_at", "")
    if calculated_at:
        calculated_at = datetime.fromisoformat(calculated_at.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")
    summary = [
        ["ABC-XYZ анализ Paradise"],
        ["Период продаж", period],
        ["Рассчитано", calculated_at],
        ["Товаров", len(items)],
        ["Выручка, ₽", snapshot.get("total_revenue", 0)],
        ["Остаток", "Текущий на момент расчёта; не исторический"],
        ["Примечание", "Мало данных: продажи менее чем в 3 неделях периода"],
        [],
        ["Класс", "Товаров"],
        *[[f"{abc}{xyz}", counts.get(f"{abc}{xyz}", 0)] for abc in "ABC" for xyz in "XYZ"],
    ]
    goods = [["Товар", "Класс", "Выручка, ₽", "Продано, шт.", "Среднее, шт./нед.", "Текущий остаток, шт.", "Недель с продажами", "Коэф. вариации", "Примечание"]]
    for item in items:
        goods.append([item.get("name", ""), item.get("class", ""), item.get("revenue", 0),
                      item.get("quantity", 0), item.get("weekly_average", 0), item.get("stock", 0),
                      item.get("active_weeks", 0), item.get("cv"), item.get("note", "")])

    types = ET.Element(f"{{{CONTENT}}}Types")
    for extension, content_type in (("rels", "application/vnd.openxmlformats-package.relationships+xml"), ("xml", "application/xml")):
        ET.SubElement(types, f"{{{CONTENT}}}Default", {"Extension": extension, "ContentType": content_type})
    for part, content_type in (("/xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"),
                               ("/xl/worksheets/sheet1.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"),
                               ("/xl/worksheets/sheet2.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"),
                               ("/xl/styles.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml")):
        ET.SubElement(types, f"{{{CONTENT}}}Override", {"PartName": part, "ContentType": content_type})
    root_rels = ET.Element(f"{{{PKG_REL}}}Relationships")
    ET.SubElement(root_rels, f"{{{PKG_REL}}}Relationship", {"Id": "rId1", "Type": f"{REL}/officeDocument", "Target": "xl/workbook.xml"})
    workbook = ET.Element(f"{{{MAIN}}}workbook")
    sheets = ET.SubElement(workbook, f"{{{MAIN}}}sheets")
    ET.SubElement(sheets, f"{{{MAIN}}}sheet", {"name": "Итоги", "sheetId": "1", f"{{{REL}}}id": "rId1"})
    ET.SubElement(sheets, f"{{{MAIN}}}sheet", {"name": "Товары", "sheetId": "2", f"{{{REL}}}id": "rId2"})
    workbook_rels = ET.Element(f"{{{PKG_REL}}}Relationships")
    for number in (1, 2):
        ET.SubElement(workbook_rels, f"{{{PKG_REL}}}Relationship", {"Id": f"rId{number}", "Type": f"{REL}/worksheet", "Target": f"worksheets/sheet{number}.xml"})
    ET.SubElement(workbook_rels, f"{{{PKG_REL}}}Relationship", {"Id": "rId3", "Type": f"{REL}/styles", "Target": "styles.xml"})
    styles = ET.Element(f"{{{MAIN}}}styleSheet")
    ET.SubElement(styles, f"{{{MAIN}}}fonts", {"count": "2"})
    fonts = styles.find(f"{{{MAIN}}}fonts")
    ET.SubElement(fonts, f"{{{MAIN}}}font")
    bold = ET.SubElement(fonts, f"{{{MAIN}}}font")
    ET.SubElement(bold, f"{{{MAIN}}}b")
    fills = ET.SubElement(styles, f"{{{MAIN}}}fills", {"count": "2"})
    ET.SubElement(fills, f"{{{MAIN}}}fill").append(ET.Element(f"{{{MAIN}}}patternFill", {"patternType": "none"}))
    ET.SubElement(fills, f"{{{MAIN}}}fill").append(ET.Element(f"{{{MAIN}}}patternFill", {"patternType": "gray125"}))
    borders = ET.SubElement(styles, f"{{{MAIN}}}borders", {"count": "1"})
    ET.SubElement(borders, f"{{{MAIN}}}border")
    ET.SubElement(styles, f"{{{MAIN}}}cellStyleXfs", {"count": "1"}).append(ET.Element(f"{{{MAIN}}}xf", {"numFmtId": "0", "fontId": "0", "fillId": "0", "borderId": "0"}))
    xfs = ET.SubElement(styles, f"{{{MAIN}}}cellXfs", {"count": "3"})
    for font, format_id in ((0, 0), (1, 0), (0, 2)):
        ET.SubElement(xfs, f"{{{MAIN}}}xf", {"numFmtId": str(format_id), "fontId": str(font), "fillId": "0", "borderId": "0", "xfId": "0", "applyNumberFormat": "1"})

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in (("[Content_Types].xml", _xml(types)), ("_rels/.rels", _xml(root_rels)),
                           ("xl/workbook.xml", _xml(workbook)), ("xl/_rels/workbook.xml.rels", _xml(workbook_rels)),
                           ("xl/styles.xml", _xml(styles)), ("xl/worksheets/sheet1.xml", _sheet(summary, [27, 55])),
                           ("xl/worksheets/sheet2.xml", _sheet(goods, [52, 10, 17, 16, 22, 24, 23, 20, 18], 1, 1))):
            archive.writestr(name, data)
    return output.getvalue()
