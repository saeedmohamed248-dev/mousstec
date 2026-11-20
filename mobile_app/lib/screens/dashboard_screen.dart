import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/i18n.dart';

import '../models/models.dart';
import '../providers/auth_provider.dart';
import '../widgets/common.dart';
import 'analytics_screen.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key});

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  late Future<DashboardSummary> _future;

  @override
  void initState() {
    super.initState();
    _load();
  }

  void _load() {
    _future = context.read<AuthProvider>().apiService.dashboard();
  }

  Future<void> _refresh() async {
    setState(_load);
    await _future;
  }

  @override
  Widget build(BuildContext context) {
    final user = context.watch<AuthProvider>().user;
    return Scaffold(
      appBar: AppBar(
        title: Text(tr('الرئيسية')),
        actions: [
          IconButton(
            tooltip: tr('التحليلات'),
            icon: const Icon(Icons.bar_chart),
            onPressed: () => Navigator.push(
              context,
              MaterialPageRoute(builder: (_) => const AnalyticsScreen()),
            ),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: FutureBuilder<DashboardSummary>(
          future: _future,
          builder: (context, snap) {
            if (snap.connectionState == ConnectionState.waiting) {
              return const StateView.loading();
            }
            if (snap.hasError) {
              return ListView(children: [
                SizedBox(
                  height: MediaQuery.of(context).size.height * 0.7,
                  child: StateView.error('${snap.error}', onRetry: _refresh),
                ),
              ]);
            }
            final d = snap.data!;
            return ListView(
              padding: const EdgeInsets.all(12),
              children: [
                Text(trf('أهلاً، {name} 👋', {'name': user?.fullName ?? ''}),
                    style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
                const SizedBox(height: 4),
                Text(tr('ملخّص اليوم'), style: TextStyle(color: Colors.grey.shade600)),
                const SizedBox(height: 12),
                GridView.count(
                  crossAxisCount: 2,
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  childAspectRatio: 1.15,
                  children: [
                    MetricCard(
                      title: tr('أوامر شغل مفتوحة'),
                      value: '${d.openWorkOrders}',
                      icon: Icons.build_circle_outlined,
                      color: Colors.orange,
                    ),
                    MetricCard(
                      title: tr('جاهز للتسليم'),
                      value: '${d.readyForDelivery}',
                      icon: Icons.check_circle_outline,
                      color: Colors.green,
                    ),
                    // 🐛 [FIX]: الفني/الكاشير كان بيشوف «إيراد اليوم = 0» (السيرفر مابيبعتهوش ليهم)
                    if (d.revenueToday != null)
                      MetricCard(
                        title: tr('إيراد اليوم'),
                        value: formatMoney(d.revenueToday!),
                        icon: Icons.payments_outlined,
                        color: Colors.teal,
                      ),
                    MetricCard(
                      title: tr('تنبيهات نقص المخزون'),
                      value: '${d.lowStockAlerts}',
                      icon: Icons.warning_amber_outlined,
                      color: Colors.redAccent,
                    ),
                    MetricCard(
                      title: tr('العملاء'),
                      value: '${d.totalCustomers}',
                      icon: Icons.people_outline,
                      color: Colors.indigo,
                    ),
                    MetricCard(
                      title: tr('قطع الغيار'),
                      value: '${d.totalProducts}',
                      icon: Icons.inventory_2_outlined,
                      color: Colors.blueGrey,
                    ),
                    MetricCard(
                      title: tr('المركبات'),
                      value: '${d.totalVehicles}',
                      icon: Icons.directions_car_outlined,
                      color: Colors.blue,
                    ),
                    MetricCard(
                      title: tr('فواتير شراء معلّقة'),
                      value: '${d.pendingPurchases}',
                      icon: Icons.receipt_long_outlined,
                      color: Colors.deepPurple,
                    ),
                    MetricCard(
                      title: tr('أعطال غير محلولة'),
                      value: '${d.unresolvedFaults}',
                      icon: Icons.error_outline,
                      color: Colors.red,
                    ),
                    MetricCard(
                      title: tr('موظفون نشطون'),
                      value: '${d.activeEmployees}',
                      icon: Icons.badge_outlined,
                      color: Colors.green,
                    ),
                    MetricCard(
                      title: tr('إجازات معلّقة'),
                      value: '${d.pendingLeaves}',
                      icon: Icons.event_busy_outlined,
                      color: Colors.orange,
                    ),
                  ],
                ),
                const SizedBox(height: 16),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(tr('حالة أوامر الشغل'),
                            style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
                        const SizedBox(height: 12),
                        _row(tr('عروض أسعار'), d.quotations),
                        _row(tr('قيد العمل'), d.inProgress),
                        _row(tr('فحص الجودة'), d.qualityCheck),
                        _row(tr('جاهز للتسليم'), d.readyForDelivery),
                      ],
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

  Widget _row(String label, int value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(label),
            Text('$value', style: const TextStyle(fontWeight: FontWeight.bold)),
          ],
        ),
      );
}
