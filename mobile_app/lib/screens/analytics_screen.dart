import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/constants.dart';
import '../core/theme.dart';
import '../providers/auth_provider.dart';
import '../widgets/common.dart';

/// شاشة التحليلات — رسوم بيانية لإيراد الأسبوع، توزيع حالات أوامر الشغل،
/// والأكثر مبيعاً.
class AnalyticsScreen extends StatefulWidget {
  const AnalyticsScreen({super.key});

  @override
  State<AnalyticsScreen> createState() => _AnalyticsScreenState();
}

class _AnalyticsScreenState extends State<AnalyticsScreen> {
  late Future<Map<String, dynamic>> _future;

  @override
  void initState() {
    super.initState();
    _future = context.read<AuthProvider>().apiService.analytics();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('التحليلات')),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const StateView.loading();
          }
          if (snap.hasError) {
            return StateView.error('${snap.error}',
                onRetry: () => setState(() {
                      _future = context.read<AuthProvider>().apiService.analytics();
                    }));
          }
          final data = snap.data!;
          final revenue = (data['revenue_last_7_days'] as List? ?? const [])
              .map((e) => e as Map<String, dynamic>)
              .toList();
          final status = (data['work_order_status'] as Map?)?.cast<String, dynamic>() ?? {};
          final top = (data['top_products'] as List? ?? const [])
              .map((e) => e as Map<String, dynamic>)
              .toList();

          return ListView(
            padding: const EdgeInsets.all(12),
            children: [
              _sectionTitle('إيراد آخر 7 أيام'),
              _RevenueChart(revenue: revenue),
              const SizedBox(height: 20),
              _sectionTitle('توزيع حالات أوامر الشغل'),
              _StatusBars(status: status),
              const SizedBox(height: 20),
              _sectionTitle('الأكثر مبيعاً (30 يوم)'),
              _TopProducts(top: top),
            ],
          );
        },
      ),
    );
  }

  Widget _sectionTitle(String t) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Text(t, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
      );
}

class _RevenueChart extends StatelessWidget {
  const _RevenueChart({required this.revenue});
  final List<Map<String, dynamic>> revenue;

  @override
  Widget build(BuildContext context) {
    if (revenue.isEmpty) return const SizedBox.shrink();
    final values = revenue.map((e) => (e['total'] is num
            ? (e['total'] as num).toDouble()
            : double.tryParse('${e['total']}') ?? 0))
        .toList();
    final maxV = values.fold<double>(0, (a, b) => b > a ? b : a);

    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(8, 20, 16, 8),
        child: SizedBox(
          height: 220,
          child: BarChart(
            BarChartData(
              maxY: maxV == 0 ? 10 : maxV * 1.2,
              barTouchData: BarTouchData(
                touchTooltipData: BarTouchTooltipData(
                  getTooltipItem: (group, gi, rod, ri) => BarTooltipItem(
                    formatMoney(rod.toY), const TextStyle(color: Colors.white, fontSize: 11),
                  ),
                ),
              ),
              titlesData: FlTitlesData(
                leftTitles: const AxisTitles(sideTitles: SideTitles(showTitles: false)),
                topTitles: const AxisTitles(sideTitles: SideTitles(showTitles: false)),
                rightTitles: const AxisTitles(sideTitles: SideTitles(showTitles: false)),
                bottomTitles: AxisTitles(
                  sideTitles: SideTitles(
                    showTitles: true,
                    getTitlesWidget: (v, meta) {
                      final i = v.toInt();
                      if (i < 0 || i >= revenue.length) return const SizedBox.shrink();
                      final d = '${revenue[i]['date']}';
                      final label = d.length >= 10 ? d.substring(5) : d; // MM-DD
                      return Padding(
                        padding: const EdgeInsets.only(top: 6),
                        child: Text(label, style: const TextStyle(fontSize: 10)),
                      );
                    },
                  ),
                ),
              ),
              gridData: const FlGridData(show: false),
              borderData: FlBorderData(show: false),
              barGroups: [
                for (int i = 0; i < values.length; i++)
                  BarChartGroupData(x: i, barRods: [
                    BarChartRodData(
                      toY: values[i],
                      color: AppTheme.primary,
                      width: 16,
                      borderRadius: const BorderRadius.vertical(top: Radius.circular(4)),
                    ),
                  ]),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _StatusBars extends StatelessWidget {
  const _StatusBars({required this.status});
  final Map<String, dynamic> status;

  @override
  Widget build(BuildContext context) {
    if (status.isEmpty) {
      return const Card(child: Padding(padding: EdgeInsets.all(16), child: Text('لا توجد بيانات')));
    }
    final total = status.values.fold<int>(0, (a, b) => a + (b as int));
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            for (final st in WorkOrderStatus.flow)
              if (status.containsKey(st))
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 6),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          Text(WorkOrderStatus.label(st)),
                          Text('${status[st]}', style: const TextStyle(fontWeight: FontWeight.bold)),
                        ],
                      ),
                      const SizedBox(height: 4),
                      ClipRRect(
                        borderRadius: BorderRadius.circular(6),
                        child: LinearProgressIndicator(
                          value: total == 0 ? 0 : (status[st] as int) / total,
                          minHeight: 8,
                          backgroundColor: Colors.grey.shade200,
                          color: AppTheme.statusColor(st),
                        ),
                      ),
                    ],
                  ),
                ),
          ],
        ),
      ),
    );
  }
}

class _TopProducts extends StatelessWidget {
  const _TopProducts({required this.top});
  final List<Map<String, dynamic>> top;

  @override
  Widget build(BuildContext context) {
    if (top.isEmpty) {
      return const Card(child: Padding(padding: EdgeInsets.all(16), child: Text('لا توجد مبيعات')));
    }
    return Card(
      child: Column(
        children: [
          for (int i = 0; i < top.length; i++)
            ListTile(
              leading: CircleAvatar(child: Text('${i + 1}')),
              title: Text('${top[i]['name'] ?? '—'}'),
              trailing: Text('${top[i]['quantity']} قطعة',
                  style: const TextStyle(fontWeight: FontWeight.bold)),
            ),
        ],
      ),
    );
  }
}
