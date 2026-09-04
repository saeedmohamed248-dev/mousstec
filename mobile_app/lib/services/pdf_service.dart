import 'package:pdf/pdf.dart';
import 'package:pdf/widgets.dart' as pw;
import 'package:printing/printing.dart';

import '../models/models.dart';
import '../widgets/common.dart';

/// توليد وطباعة/مشاركة أمر شغل كـ PDF عربي (RTL).
class WorkOrderPdf {
  WorkOrderPdf._();

  static Future<void> printOrder(WorkOrder o) async {
    // خط عربي من Google Fonts (يُجلب أول مرة ثم يُخزّن مؤقتاً).
    final base = await PdfGoogleFonts.cairoRegular();
    final bold = await PdfGoogleFonts.cairoBold();

    final doc = pw.Document();
    doc.addPage(
      pw.MultiPage(
        textDirection: pw.TextDirection.rtl,
        theme: pw.ThemeData.withFont(base: base, bold: bold),
        pageFormat: PdfPageFormat.a4,
        build: (ctx) => [
          _header(o),
          pw.SizedBox(height: 12),
          _infoBox(o),
          pw.SizedBox(height: 12),
          if (o.notes != null && o.notes!.trim().isNotEmpty) ...[
            _label('الشكوى / الملاحظات'),
            pw.Text(o.notes!),
            pw.SizedBox(height: 12),
          ],
          _label('قطع الغيار'),
          _itemsTable(o),
          pw.SizedBox(height: 12),
          if (o.services.isNotEmpty) ...[
            _label('الخدمات / المصنعيات'),
            _servicesTable(o),
            pw.SizedBox(height: 12),
          ],
          _totals(o),
          pw.SizedBox(height: 24),
          _signature(),
        ],
      ),
    );

    await Printing.layoutPdf(onLayout: (format) => doc.save());
  }

  static pw.Widget _header(WorkOrder o) => pw.Row(
        mainAxisAlignment: pw.MainAxisAlignment.spaceBetween,
        children: [
          pw.Column(crossAxisAlignment: pw.CrossAxisAlignment.start, children: [
            pw.Text('Mouss Tec', style: pw.TextStyle(fontSize: 22, fontWeight: pw.FontWeight.bold)),
            pw.Text('نظام إدارة الورش'),
          ]),
          pw.Column(crossAxisAlignment: pw.CrossAxisAlignment.end, children: [
            pw.Text('أمر شغل #${o.id}', style: pw.TextStyle(fontSize: 16, fontWeight: pw.FontWeight.bold)),
            pw.Text('الحالة: ${o.statusDisplay}'),
            if (o.dateCreated != null) pw.Text('التاريخ: ${o.dateCreated!.split('T').first}'),
          ]),
        ],
      );

  static pw.Widget _label(String t) => pw.Container(
        margin: const pw.EdgeInsets.only(bottom: 4),
        child: pw.Text(t, style: pw.TextStyle(fontWeight: pw.FontWeight.bold, fontSize: 13)),
      );

  static pw.Widget _infoBox(WorkOrder o) => pw.Container(
        padding: const pw.EdgeInsets.all(10),
        decoration: pw.BoxDecoration(
          border: pw.Border.all(color: PdfColors.grey400),
          borderRadius: pw.BorderRadius.circular(6),
        ),
        child: pw.Column(crossAxisAlignment: pw.CrossAxisAlignment.start, children: [
          pw.Text('العميل: ${o.customerName}   —   ${o.customerPhone}'),
          if (o.vehiclePlate != null) pw.Text('المركبة: ${o.vehiclePlate}'),
          if (o.mileage != null) pw.Text('العداد: ${o.mileage} كم'),
          if (o.branchName != null) pw.Text('الفرع: ${o.branchName}'),
        ]),
      );

  static pw.Widget _itemsTable(WorkOrder o) => pw.TableHelper.fromTextArray(
        headerStyle: pw.TextStyle(fontWeight: pw.FontWeight.bold),
        headerDecoration: const pw.BoxDecoration(color: PdfColors.grey200),
        cellAlignment: pw.Alignment.centerRight,
        headers: ['القطعة', 'الرقم', 'الكمية', 'السعر', 'الإجمالي'],
        data: o.items.isEmpty
            ? [['—', '', '', '', '']]
            : o.items
                .map((it) => [
                      it.productName,
                      it.partNumber,
                      '${it.quantity}',
                      formatMoney(it.unitPrice),
                      formatMoney(it.totalPrice),
                    ])
                .toList(),
      );

  static pw.Widget _servicesTable(WorkOrder o) => pw.TableHelper.fromTextArray(
        headerStyle: pw.TextStyle(fontWeight: pw.FontWeight.bold),
        headerDecoration: const pw.BoxDecoration(color: PdfColors.grey200),
        cellAlignment: pw.Alignment.centerRight,
        headers: ['الخدمة', 'الساعات', 'القيمة'],
        data: o.services
            .map((s) => [s.serviceName, '${s.hours}', formatMoney(s.price)])
            .toList(),
      );

  static pw.Widget _totals(WorkOrder o) => pw.Container(
        alignment: pw.Alignment.centerLeft,
        child: pw.Column(crossAxisAlignment: pw.CrossAxisAlignment.start, children: [
          _totalRow('الإجمالي', o.totalAmount, bold: true),
          _totalRow('المدفوع', o.paidAmount),
          if (o.dueAmount > 0) _totalRow('المتبقّي', o.dueAmount),
        ]),
      );

  static pw.Widget _totalRow(String label, double v, {bool bold = false}) => pw.Container(
        width: 220,
        padding: const pw.EdgeInsets.symmetric(vertical: 2),
        child: pw.Row(mainAxisAlignment: pw.MainAxisAlignment.spaceBetween, children: [
          pw.Text(label, style: pw.TextStyle(fontWeight: bold ? pw.FontWeight.bold : pw.FontWeight.normal)),
          pw.Text(formatMoney(v), style: pw.TextStyle(fontWeight: bold ? pw.FontWeight.bold : pw.FontWeight.normal)),
        ]),
      );

  static pw.Widget _signature() => pw.Row(
        mainAxisAlignment: pw.MainAxisAlignment.spaceBetween,
        children: [
          pw.Text('توقيع العميل: ____________________'),
          pw.Text('المسؤول: ____________________'),
        ],
      );
}
