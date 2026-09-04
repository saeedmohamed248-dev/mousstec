import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/constants.dart';
import '../core/theme.dart';
import '../models/models.dart';
import '../providers/auth_provider.dart';
import '../services/pdf_service.dart';
import '../widgets/common.dart';

class WorkOrderDetailScreen extends StatefulWidget {
  const WorkOrderDetailScreen({super.key, required this.orderId});
  final int orderId;

  @override
  State<WorkOrderDetailScreen> createState() => _WorkOrderDetailScreenState();
}

class _WorkOrderDetailScreenState extends State<WorkOrderDetailScreen> {
  late Future<WorkOrder> _future;
  bool _updating = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  void _load() {
    _future = context.read<AuthProvider>().apiService.workOrder(widget.orderId);
  }

  Future<void> _changeStatus(String newStatus) async {
    setState(() => _updating = true);
    try {
      await context.read<AuthProvider>().apiService.updateWorkOrderStatus(widget.orderId, newStatus);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('تم تحديث الحالة إلى: ${WorkOrderStatus.label(newStatus)}')),
      );
      setState(_load);
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('$e'), backgroundColor: Colors.redAccent),
      );
    } finally {
      if (mounted) setState(() => _updating = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('أمر شغل #${widget.orderId}')),
      body: FutureBuilder<WorkOrder>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const StateView.loading();
          }
          if (snap.hasError) {
            return StateView.error('${snap.error}', onRetry: () => setState(_load));
          }
          final o = snap.data!;
          return ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          Expanded(
                            child: Text(o.customerName,
                                style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
                          ),
                          StatusChip(status: o.status, label: o.statusDisplay),
                        ],
                      ),
                      const SizedBox(height: 6),
                      _line(Icons.phone, o.customerPhone),
                      if (o.vehiclePlate != null) _line(Icons.directions_car, o.vehiclePlate!),
                      if (o.mileage != null) _line(Icons.speed, 'العداد: ${o.mileage} كم'),
                      if (o.branchName != null) _line(Icons.store, o.branchName!),
                    ],
                  ),
                ),
              ),
              if (o.notes != null && o.notes!.trim().isNotEmpty)
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('ملاحظات / شكوى العميل',
                            style: TextStyle(fontWeight: FontWeight.bold)),
                        const SizedBox(height: 6),
                        Text(o.notes!),
                      ],
                    ),
                  ),
                ),
              _section('قطع الغيار (${o.items.length})', [
                for (final it in o.items)
                  ListTile(
                    dense: true,
                    title: Text(it.productName),
                    subtitle: Text('${it.partNumber} • ${it.quantity} × ${formatMoney(it.unitPrice)}'),
                    trailing: Text(formatMoney(it.totalPrice),
                        style: const TextStyle(fontWeight: FontWeight.bold)),
                  ),
                if (o.items.isEmpty) const ListTile(dense: true, title: Text('لا توجد قطع')),
              ]),
              _section('الخدمات / المصنعيات (${o.services.length})', [
                for (final s in o.services)
                  ListTile(
                    dense: true,
                    title: Text(s.serviceName),
                    subtitle: Text('${s.hours} ساعة'),
                    trailing: Text(formatMoney(s.price),
                        style: const TextStyle(fontWeight: FontWeight.bold)),
                  ),
                if (o.services.isEmpty) const ListTile(dense: true, title: Text('لا توجد خدمات')),
              ]),
              Card(
                color: AppTheme.primary.withOpacity(0.05),
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    children: [
                      _totalRow('الإجمالي', o.totalAmount, bold: true),
                      _totalRow('المدفوع', o.paidAmount),
                      if (o.dueAmount > 0) _totalRow('المتبقّي', o.dueAmount, color: Colors.redAccent),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              OutlinedButton.icon(
                onPressed: () async {
                  try {
                    await WorkOrderPdf.printOrder(o);
                  } catch (e) {
                    if (!mounted) return;
                    ScaffoldMessenger.of(context).showSnackBar(
                      SnackBar(content: Text('تعذّرت الطباعة: $e'), backgroundColor: Colors.redAccent),
                    );
                  }
                },
                icon: const Icon(Icons.print),
                label: const Text('طباعة / مشاركة PDF'),
              ),
              const SizedBox(height: 8),
              const Text('تغيير حالة أمر الشغل',
                  style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final st in WorkOrderStatus.flow)
                    ActionChip(
                      avatar: Icon(Icons.circle, size: 12, color: AppTheme.statusColor(st)),
                      label: Text(WorkOrderStatus.label(st)),
                      onPressed: (_updating || st == o.status) ? null : () => _changeStatus(st),
                      backgroundColor: st == o.status ? AppTheme.statusColor(st).withOpacity(0.18) : null,
                    ),
                ],
              ),
              if (_updating) const Padding(
                padding: EdgeInsets.only(top: 16),
                child: Center(child: CircularProgressIndicator()),
              ),
            ],
          );
        },
      ),
    );
  }

  Widget _line(IconData icon, String text) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(children: [
          Icon(icon, size: 16, color: Colors.grey.shade600),
          const SizedBox(width: 8),
          Expanded(child: Text(text)),
        ]),
      );

  Widget _section(String title, List<Widget> children) => Card(
        child: Padding(
          padding: const EdgeInsets.all(8),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(8, 8, 8, 0),
                child: Text(title, style: const TextStyle(fontWeight: FontWeight.bold)),
              ),
              ...children,
            ],
          ),
        ),
      );

  Widget _totalRow(String label, double value, {bool bold = false, Color? color}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(label, style: TextStyle(fontWeight: bold ? FontWeight.bold : FontWeight.normal)),
            Text(formatMoney(value),
                style: TextStyle(
                    fontWeight: bold ? FontWeight.bold : FontWeight.w600, color: color)),
          ],
        ),
      );
}
