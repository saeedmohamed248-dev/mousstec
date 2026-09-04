import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../providers/auth_provider.dart';
import '../widgets/common.dart';
import 'barcode_scanner_screen.dart';
import 'product_detail_screen.dart';

class InventoryScreen extends StatefulWidget {
  const InventoryScreen({super.key});

  @override
  State<InventoryScreen> createState() => _InventoryScreenState();
}

class _InventoryScreenState extends State<InventoryScreen> {
  late Future<Paginated<Product>> _future;
  final _searchCtrl = TextEditingController();
  String _search = '';
  bool _lowOnly = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _searchCtrl.dispose();
    super.dispose();
  }

  Future<void> _scan() async {
    final code = await Navigator.push<String>(
      context,
      MaterialPageRoute(builder: (_) => const BarcodeScannerScreen()),
    );
    if (code != null && code.isNotEmpty) {
      _search = code;
      _searchCtrl.text = code;
      if (_lowOnly) _lowOnly = false;
      _refresh();
    }
  }

  void _load() {
    final api = context.read<AuthProvider>().apiService;
    _future = _lowOnly ? api.lowStock() : api.products(search: _search);
  }

  Future<void> _refresh() async {
    setState(_load);
    await _future;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('المخزون'),
        actions: [
          IconButton(
            tooltip: 'مسح باركود',
            icon: const Icon(Icons.qr_code_scanner),
            onPressed: _scan,
          ),
          IconButton(
            tooltip: 'نقص المخزون فقط',
            icon: Icon(_lowOnly ? Icons.warning_amber : Icons.warning_amber_outlined),
            color: _lowOnly ? Colors.amberAccent : null,
            onPressed: () {
              _lowOnly = !_lowOnly;
              _refresh();
            },
          ),
        ],
      ),
      body: Column(
        children: [
          if (!_lowOnly)
            Padding(
              padding: const EdgeInsets.all(12),
              child: TextField(
                controller: _searchCtrl,
                decoration: InputDecoration(
                  hintText: 'ابحث بالاسم / رقم القطعة / الباركود',
                  prefixIcon: const Icon(Icons.search),
                  suffixIcon: _search.isNotEmpty
                      ? IconButton(
                          icon: const Icon(Icons.clear),
                          onPressed: () {
                            _search = '';
                            _searchCtrl.clear();
                            _refresh();
                            FocusScope.of(context).unfocus();
                          })
                      : null,
                ),
                textInputAction: TextInputAction.search,
                onSubmitted: (v) {
                  _search = v.trim();
                  _refresh();
                },
              ),
            ),
          if (_lowOnly)
            Container(
              width: double.infinity,
              color: Colors.amber.shade100,
              padding: const EdgeInsets.all(10),
              child: const Text('عرض القطع التي وصلت لحد الأمان أو أقل',
                  textAlign: TextAlign.center),
            ),
          Expanded(
            child: RefreshIndicator(
              onRefresh: _refresh,
              child: FutureBuilder<Paginated<Product>>(
                future: _future,
                builder: (context, snap) {
                  if (snap.connectionState == ConnectionState.waiting) {
                    return const StateView.loading();
                  }
                  if (snap.hasError) {
                    return ListView(children: [
                      SizedBox(height: 400, child: StateView.error('${snap.error}', onRetry: _refresh)),
                    ]);
                  }
                  final items = snap.data!.results;
                  if (items.isEmpty) {
                    return ListView(children: const [
                      SizedBox(height: 400, child: StateView.empty('لا توجد قطع')),
                    ]);
                  }
                  return ListView.builder(
                    padding: const EdgeInsets.all(8),
                    itemCount: items.length,
                    itemBuilder: (context, i) => _ProductTile(product: items[i]),
                  );
                },
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _ProductTile extends StatelessWidget {
  const _ProductTile({required this.product});
  final Product product;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: ListTile(
        onTap: () => Navigator.push(
          context,
          MaterialPageRoute(builder: (_) => ProductDetailScreen(productId: product.id)),
        ),
        leading: CircleAvatar(
          backgroundColor: product.isLowStock ? Colors.red.shade50 : Colors.blue.shade50,
          child: Text('${product.totalQuantity}',
              style: TextStyle(
                  color: product.isLowStock ? Colors.red : Colors.blue,
                  fontWeight: FontWeight.bold)),
        ),
        title: Text(product.name, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text('${product.partNumber} • ${product.brand}'
            '${product.carModel != null ? ' • ${product.carModel}' : ''}'),
        trailing: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Text(formatMoney(product.retailPrice),
                style: const TextStyle(fontWeight: FontWeight.bold)),
            if (product.isLowStock)
              const Text('نقص', style: TextStyle(color: Colors.red, fontSize: 12)),
          ],
        ),
      ),
    );
  }
}
