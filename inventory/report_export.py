# 📤 تصدير التقارير إلى Excel (.xlsx) و PDF — مُوحّد لكل التقارير.
#
# كل تقرير بيبني بنية موحّدة `tables` = قائمة جداول:
#   [{"name": "...", "columns": [...], "rows": [[...], ...], "total": [...]}]
# وبعدين بننادي report_to_xlsx / report_to_pdf.
from decimal import Decimal

from django.http import HttpResponse


def _cell_value(v):
    """أرقام Decimal بتتحوّل float عشان Excel يتعامل معها كأرقام."""
    if isinstance(v, Decimal):
        return float(v)
    return v


def report_to_xlsx(filename, title, subtitle, tables):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "التقرير"
    ws.sheet_view.rightToLeft = True

    r = 1
    ws.cell(r, 1, title).font = Font(bold=True, size=14)
    r += 1
    if subtitle:
        ws.cell(r, 1, subtitle).font = Font(size=10, color="666666")
        r += 1
    r += 1

    max_cols = 1
    hdr_fill = PatternFill("solid", fgColor="1F2937")
    for t in tables:
        max_cols = max(max_cols, len(t.get("columns") or []))
        if t.get("name"):
            ws.cell(r, 1, t["name"]).font = Font(bold=True, size=12)
            r += 1
        for c, col in enumerate(t.get("columns") or [], 1):
            cell = ws.cell(r, c, col)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = hdr_fill
        r += 1
        for row in t.get("rows") or []:
            for c, val in enumerate(row, 1):
                ws.cell(r, c, _cell_value(val))
            r += 1
        if t.get("total"):
            for c, val in enumerate(t["total"], 1):
                cell = ws.cell(r, c, _cell_value(val))
                cell.font = Font(bold=True)
            r += 1
        r += 1

    for c in range(1, max_cols + 1):
        ws.column_dimensions[get_column_letter(c)].width = 24

    resp = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
    wb.save(resp)
    return resp


def report_to_pdf(request, filename, title, subtitle, tables):
    from django.template.loader import render_to_string
    html = render_to_string("inventory/report_print.html", {
        "title": title, "subtitle": subtitle, "tables": tables,
    })
    try:
        from weasyprint import HTML
        pdf = HTML(string=html, base_url=request.build_absolute_uri('/')).write_pdf()
    except Exception:
        # لو weasyprint مش متاح لأي سبب، نرجّع الـ HTML للطباعة من المتصفح
        return HttpResponse(html)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
    return resp


def export_report(request, filename, title, subtitle, tables):
    """يرجّع Response حسب ?export=xlsx|pdf، أو None لو مفيش تصدير مطلوب."""
    exp = (request.GET.get("export") or "").strip().lower()
    if exp == "xlsx":
        return report_to_xlsx(filename, title, subtitle, tables)
    if exp == "pdf":
        return report_to_pdf(request, filename, title, subtitle, tables)
    return None
