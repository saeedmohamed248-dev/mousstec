import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/i18n.dart';
import '../../providers/auth_provider.dart';
import '../../widgets/common.dart';
import '../../core/modules.dart';
import 'print_constants.dart';
import '../generic_list_screen.dart';
import 'print_orders_screen.dart';
import 'production_board_screen.dart';

/// 📊 الشاشة الرئيسية للمطبعة.
class PrintDashboardScreen extends StatefulWidget {
  const PrintDashboardScreen({super.key});

  @override
  State<PrintDashboardScreen> createState() => _PrintDashboardScreenState();
}

class _PrintDashboardScreenState extends State<PrintDashboardScreen> {
  late Future<Map<String, dynamic>> _future;

  @override
  void initState() {
    super.initState();
    _load();
  }

  void _load() => _future = context.read<AuthProvider>().apiService.printDashboard();

  Future<void> _refresh() async {
    setState(_load);
    await _future;
  }

  void _open(Widget screen) => Navigator.push(context, MaterialPageRoute(builder: (_) => screen));

  void _openModule(String key) {
    final mod = moduleRegistry.firstWhere((m) => m.key == key);
    _open(GenericListScreen(module: mod));
  }

  @override
  Widget build(BuildContext context) {
    final auth = context.watch<AuthProvider>();
    return Scaffold(
      appBar: AppBar(title: Text(auth.tenant.name.isNotEmpty ? auth.tenant.name : tr('الرئيسية'))),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: FutureBuilder<Map<String, dynamic>>(
          future: _future,
          builder: (context, snap) {
            if (snap.connectionState == ConnectionState.waiting) return const StateView.loading();
            if (snap.hasError) {
              return ListView(children: [
                SizedBox(
                  height: MediaQuery.of(context).size.height * 0.7,
                  child: StateView.error('${snap.error}', onRetry: _refresh),
                ),
              ]);
            }
            final d = snap.data!;
            int n(String k) => (d[k] as num?)?.toInt() ?? 0;
            final stages = (d['jobs_by_stage'] as Map?) ?? const {};
            return ListView(
              padding: const EdgeInsets.all(12),
              children: [
                Text(trf('أهلاً، {name} 👋', {'name': auth.user?.fullName ?? ''}),
                    style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
                const SizedBox(height: 4),
                Text(tr('ملخّص المطبعة النهارده'), style: TextStyle(color: Colors.grey.shade600)),
                const SizedBox(height: 12),
                GridView.count(
                  crossAxisCount: 2,
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  childAspectRatio: 1.15,
                  children: [
                    MetricCard(
                      title: tr('طلبات مفتوحة'),
                      value: '${n('open_orders')}',
                      icon: Icons.receipt_long_outlined,
                      color: Colors.blue,
                      onTap: () => _open(const PrintOrdersScreen(initialStatus: 'open')),
                    ),
                    MetricCard(
                      title: tr('جاهز للتسليم'),
                      value: '${n('ready')}',
                      icon: Icons.check_circle_outline,
                      color: Colors.green,
                      onTap: () => _open(const PrintOrdersScreen(initialStatus: 'ready')),
                    ),
                    MetricCard(
                      title: tr('متأخر عن ميعاده'),
                      value: '${n('late_orders')}',
                      icon: Icons.alarm,
                      color: Colors.redAccent,
                      onTap: () => _open(const PrintOrdersScreen(initialStatus: 'open')),
                    ),
                    MetricCard(
                      title: tr('تسليم النهارده'),
                      value: '${n('due_today')}',
                      icon: Icons.today,
                      color: Colors.orange,
                    ),
                    if (d['sales_today'] != null)
                      MetricCard(
                        title: tr('فواتير النهارده'),
                        value: moneyOrDash(d['sales_today']),
                        icon: Icons.payments_outlined,
                        color: Colors.teal,
                      ),
                    MetricCard(
                      title: tr('بروفات مستنية العميل'),
                      value: '${n('pending_proofs')}',
                      icon: Icons.fact_check_outlined,
                      color: Colors.amber.shade800,
                      onTap: auth.tenant.can('view_printproof') ? () => _openModule('print-proofs') : null,
                    ),
                    MetricCard(
                      title: tr('العميل طلب تعديل'),
                      value: '${n('changes_requested')}',
                      icon: Icons.edit_note,
                      color: Colors.deepOrange,
                    ),
                    MetricCard(
                      title: tr('خامات ناقصة'),
                      value: '${n('low_stock_materials')}',
                      icon: Icons.warning_amber_outlined,
                      color: Colors.red,
                      onTap:
                          auth.tenant.can('view_printmaterial') ? () => _openModule('print-materials') : null,
                    ),
                    MetricCard(
                      title: tr('العملاء'),
                      value: '${n('total_customers')}',
                      icon: Icons.people_outline,
                      color: Colors.indigo,
                      onTap:
                          auth.tenant.can('view_printcustomer') ? () => _openModule('print-customers') : null,
                    ),
                  ],
                ),
                const SizedBox(height: 16),
                Card(
                  child: InkWell(
                    borderRadius: BorderRadius.circular(16),
                    onTap:
                        auth.tenant.can('view_printjob') ? () => _open(const ProductionBoardScreen()) : null,
                    child: Padding(
                      padding: const EdgeInsets.all(16),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(children: [
                            Expanded(
                              child: Text(tr('الإنتاج'),
                                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
                            ),
                            Text(trf('{n} مهمة شغالة', {'n': n('active_jobs')}),
                                style: TextStyle(color: Colors.grey.shade600)),
                          ]),
                          const SizedBox(height: 12),
                          for (final e in productionStages.entries.where((e) => e.key != 'done'))
                            Padding(
                              padding: const EdgeInsets.symmetric(vertical: 4),
                              child: Row(
                                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                                children: [
                                  Text(tr(e.value)),
                                  Text('${stages[e.key] ?? 0}',
                                      style: const TextStyle(fontWeight: FontWeight.bold)),
                                ],
                              ),
                            ),
                        ],
                      ),
                    ),
                  ),
                ),
              ],
            );
          },
        ),
      ),
    );
  }
}
