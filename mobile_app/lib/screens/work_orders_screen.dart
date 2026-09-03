import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/constants.dart';
import '../models/models.dart';
import '../providers/auth_provider.dart';
import '../widgets/common.dart';
import 'work_order_detail_screen.dart';

class WorkOrdersScreen extends StatefulWidget {
  const WorkOrdersScreen({super.key});

  @override
  State<WorkOrdersScreen> createState() => _WorkOrdersScreenState();
}

class _WorkOrdersScreenState extends State<WorkOrdersScreen> {
  late Future<Paginated<WorkOrder>> _future;
  String _filter = 'open';
  String _search = '';

  final _filters = const {
    'open': 'الكل المفتوح',
    'in_progress': 'قيد العمل',
    'quality_check': 'فحص الجودة',
    'ready': 'جاهز',
    'posted': 'تم التسليم',
  };

  @override
  void initState() {
    super.initState();
    _load();
  }

  void _load() {
    _future = context.read<AuthProvider>().apiService.workOrders(
          status: _filter,
          search: _search,
        );
  }

  Future<void> _refresh() async {
    setState(_load);
    await _future;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('أوامر الشغل')),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 4),
            child: TextField(
              decoration: InputDecoration(
                hintText: 'ابحث باسم العميل / الموبايل / اللوحة',
                prefixIcon: const Icon(Icons.search),
                suffixIcon: _search.isNotEmpty
                    ? IconButton(
                        icon: const Icon(Icons.clear),
                        onPressed: () {
                          _search = '';
                          _refresh();
                          FocusScope.of(context).unfocus();
                        },
                      )
                    : null,
              ),
              textInputAction: TextInputAction.search,
              onSubmitted: (v) {
                _search = v.trim();
                _refresh();
              },
            ),
          ),
          SizedBox(
            height: 44,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 8),
              children: _filters.entries.map((e) {
                final selected = _filter == e.key;
                return Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 4),
                  child: ChoiceChip(
                    label: Text(e.value),
                    selected: selected,
                    onSelected: (_) {
                      _filter = e.key;
                      _refresh();
                    },
                  ),
                );
              }).toList(),
            ),
          ),
          Expanded(
            child: RefreshIndicator(
              onRefresh: _refresh,
              child: FutureBuilder<Paginated<WorkOrder>>(
                future: _future,
                builder: (context, snap) {
                  if (snap.connectionState == ConnectionState.waiting) {
                    return const StateView.loading();
                  }
                  if (snap.hasError) {
                    return _scrollable(StateView.error('${snap.error}', onRetry: _refresh));
                  }
                  final orders = snap.data!.results;
                  if (orders.isEmpty) {
                    return _scrollable(const StateView.empty('لا توجد أوامر شغل'));
                  }
                  return ListView.builder(
                    padding: const EdgeInsets.all(8),
                    itemCount: orders.length,
                    itemBuilder: (context, i) => _WorkOrderTile(
                      order: orders[i],
                      onTap: () async {
                        await Navigator.push(
                          context,
                          MaterialPageRoute(
                            builder: (_) => WorkOrderDetailScreen(orderId: orders[i].id),
                          ),
                        );
                        _refresh();
                      },
                    ),
                  );
                },
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _scrollable(Widget child) => ListView(
        children: [SizedBox(height: 400, child: child)],
      );
}

class _WorkOrderTile extends StatelessWidget {
  const _WorkOrderTile({required this.order, required this.onTap});
  final WorkOrder order;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: ListTile(
        onTap: onTap,
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
        title: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Expanded(child: Text(order.customerName, style: const TextStyle(fontWeight: FontWeight.bold))),
            StatusChip(status: order.status, label: WorkOrderStatus.label(order.status)),
          ],
        ),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 6),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('أمر شغل #${order.id}'
                  '${order.vehiclePlate != null ? ' • ${order.vehiclePlate}' : ''}'),
              const SizedBox(height: 2),
              Text('الإجمالي: ${formatMoney(order.totalAmount)}'
                  '${order.dueAmount > 0 ? ' • متبقّي: ${formatMoney(order.dueAmount)}' : ''}',
                  style: TextStyle(color: Colors.grey.shade700, fontSize: 12)),
            ],
          ),
        ),
        trailing: const Icon(Icons.chevron_left),
      ),
    );
  }
}
