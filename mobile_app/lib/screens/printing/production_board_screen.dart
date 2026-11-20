import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/i18n.dart';
import '../../providers/auth_provider.dart';
import '../../widgets/common.dart';
import 'print_constants.dart';
import 'print_order_detail_screen.dart';

/// 🏭 لوحة الإنتاج: تاب لكل مرحلة (تصميم/بروفة/طباعة/تشطيب) وزرار «المرحلة الجاية».
/// الطباعة مقفولة لحد ما العميل يوافق على البروفة (السيرفر بيرفض كمان).
class ProductionBoardScreen extends StatefulWidget {
  const ProductionBoardScreen({super.key});

  @override
  State<ProductionBoardScreen> createState() => _ProductionBoardScreenState();
}

class _ProductionBoardScreenState extends State<ProductionBoardScreen> {
  static const _stages = ['design', 'proof', 'printing', 'finishing'];
  List<Map<String, dynamic>> _jobs = const [];
  bool _loading = true;
  Object? _error;
  final Set<int> _busy = {};

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => _loading = true);
    try {
      final api = context.read<AuthProvider>().apiService;
      final all = <Map<String, dynamic>>[];
      var page = 1;
      // اللوحة بتجيب كل المهام الشغالة (عادةً قليلة) — صفحات لحد ما تخلص بحد أقصى 10
      while (page <= 10) {
        final data = await api.rawList('/print/jobs/', query: {'page': page});
        all.addAll((data['results'] as List? ?? const []).cast<Map<String, dynamic>>());
        if (data['next'] == null) break;
        page++;
      }
      if (mounted) {
        setState(() {
          _jobs = all;
          _error = null;
        });
      }
    } catch (e) {
      if (mounted) setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _advance(Map<String, dynamic> job) async {
    final id = job['id'] as int;
    setState(() => _busy.add(id));
    try {
      final updated = await context.read<AuthProvider>().apiService.advancePrintJob(id);
      if (!mounted) return;
      setState(() {
        _jobs = [
          for (final j in _jobs)
            if (j['id'] == id) updated else j,
        ].where((j) => j['is_complete'] != true).toList();
      });
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(trf('«{job}» اتنقلت لـ «{stage}»', {
          'job': '${job['description']}',
          'stage': tr(productionStages['${updated['stage']}'] ?? '${updated['stage_display']}'),
        })),
      ));
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$e'), backgroundColor: Colors.redAccent));
      }
    } finally {
      if (mounted) setState(() => _busy.remove(id));
    }
  }

  @override
  Widget build(BuildContext context) {
    final canMove = context.watch<AuthProvider>().tenant.can('change_printjob');
    return DefaultTabController(
      length: _stages.length,
      child: Scaffold(
        appBar: AppBar(
          title: Text(tr('لوحة الإنتاج')),
          bottom: TabBar(
            isScrollable: true,
            labelColor: Colors.white,
            unselectedLabelColor: Colors.white70,
            indicatorColor: Colors.white,
            tabs: [
              for (final s in _stages)
                Tab(text: '${tr(productionStages[s]!)} (${_jobs.where((j) => j['stage'] == s).length})'),
            ],
          ),
        ),
        body: _error != null
            ? StateView.error('$_error', onRetry: _load)
            : _loading && _jobs.isEmpty
                ? const StateView.loading()
                : TabBarView(
                    children: [for (final s in _stages) _column(s, canMove)],
                  ),
      ),
    );
  }

  Widget _column(String stage, bool canMove) {
    final jobs = _jobs.where((j) => j['stage'] == stage).toList();
    return RefreshIndicator(
      onRefresh: _load,
      child: jobs.isEmpty
          ? ListView(children: [SizedBox(height: 400, child: StateView.empty(tr('مفيش شغل في المرحلة دي')))])
          : ListView.builder(
              padding: const EdgeInsets.all(8),
              itemCount: jobs.length,
              itemBuilder: (context, i) {
                final j = jobs[i];
                final next = j['next_stage'] as String?;
                final blocked = j['blocked'] == true;
                final due = j['date_due'] as String?;
                final late = due != null && DateTime.tryParse(due)?.isBefore(DateTime.now()) == true;
                return Card(
                  child: ListTile(
                    onTap: () async {
                      await Navigator.push(
                          context,
                          MaterialPageRoute(
                              builder: (_) => PrintOrderDetailScreen(orderId: j['order_id'] as int)));
                      _load();
                    },
                    title: Text('${j['description']}', style: const TextStyle(fontWeight: FontWeight.bold)),
                    subtitle: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('${j['customer_name']} • #${j['order_number']} • ${j['quantity']}'),
                        if (due != null)
                          Text(trf('التسليم {date}', {'date': displayValue(due)}),
                              style: TextStyle(
                                  color: late ? Colors.redAccent : null,
                                  fontWeight: late ? FontWeight.bold : null)),
                        if (blocked)
                          Text(tr('⏳ مستني موافقة العميل على البروفة'),
                              style: TextStyle(color: Colors.amber.shade900)),
                      ],
                    ),
                    isThreeLine: true,
                    trailing: canMove && next != null && !blocked
                        ? _busy.contains(j['id'])
                            ? const SizedBox(
                                width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2))
                            : IconButton.filledTonal(
                                tooltip: trf('لـ «{stage}»', {'stage': tr(productionStages[next] ?? next)}),
                                icon: const Icon(Icons.arrow_forward),
                                onPressed: () => _advance(j),
                              )
                        : null,
                  ),
                );
              },
            ),
    );
  }
}
