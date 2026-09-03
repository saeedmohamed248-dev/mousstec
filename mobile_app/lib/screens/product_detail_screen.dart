import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../providers/auth_provider.dart';
import '../widgets/common.dart';

class ProductDetailScreen extends StatefulWidget {
  const ProductDetailScreen({super.key, required this.productId});
  final int productId;

  @override
  State<ProductDetailScreen> createState() => _ProductDetailScreenState();
}

class _ProductDetailScreenState extends State<ProductDetailScreen> {
  late Future<Product> _future;

  @override
  void initState() {
    super.initState();
    _future = context.read<AuthProvider>().apiService.product(widget.productId);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('تفاصيل القطعة')),
      body: FutureBuilder<Product>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const StateView.loading();
          }
          if (snap.hasError) {
            return StateView.error('${snap.error}',
                onRetry: () => setState(() {
                      _future = context.read<AuthProvider>().apiService.product(widget.productId);
                    }));
          }
          final p = snap.data!;
          return ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(p.name, style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
                      const SizedBox(height: 4),
                      Text(p.partNumber, style: TextStyle(color: Colors.grey.shade600)),
                      const Divider(height: 24),
                      _kv('الماركة', p.brand),
                      if (p.carModel != null) _kv('الموديلات', p.carModel!),
                      if (p.condition != null) _kv('الحالة', p.condition!),
                      if (p.barcode != null) _kv('الباركود', p.barcode!),
                      if (p.warrantyMonths != null && p.warrantyMonths! > 0)
                        _kv('الضمان', '${p.warrantyMonths} شهر'),
                      _kv('سعر البيع', formatMoney(p.retailPrice)),
                      _kv('إجمالي المتاح', '${p.totalQuantity}'),
                      _kv('حد الأمان', '${p.minStockLevel}'),
                    ],
                  ),
                ),
              ),
              if (p.isLowStock)
                Card(
                  color: Colors.red.shade50,
                  child: const ListTile(
                    leading: Icon(Icons.warning_amber, color: Colors.red),
                    title: Text('المخزون منخفض — يُنصح بإعادة الطلب'),
                  ),
                ),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(8),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Padding(
                        padding: EdgeInsets.all(8),
                        child: Text('التوزيع على الفروع',
                            style: TextStyle(fontWeight: FontWeight.bold)),
                      ),
                      if (p.stockByBranch.isEmpty)
                        const ListTile(dense: true, title: Text('لا يوجد رصيد في أي فرع')),
                      for (final loc in p.stockByBranch)
                        ListTile(
                          dense: true,
                          leading: const Icon(Icons.store_outlined),
                          title: Text(loc.branchName),
                          subtitle: loc.shelf != null ? Text('الرف: ${loc.shelf}') : null,
                          trailing: Text('${loc.quantity}',
                              style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
                        ),
                    ],
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }

  Widget _kv(String k, String v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(k, style: TextStyle(color: Colors.grey.shade700)),
            Flexible(child: Text(v, textAlign: TextAlign.end, style: const TextStyle(fontWeight: FontWeight.w600))),
          ],
        ),
      );
}
