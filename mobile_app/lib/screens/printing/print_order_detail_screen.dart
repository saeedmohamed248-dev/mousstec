import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../../core/i18n.dart';
import '../../providers/auth_provider.dart';
import '../../widgets/common.dart';
import 'print_constants.dart';

/// 🧾 تفاصيل طلب طباعة: البنود ومراحلها، الحساب، وأزرار (تأكيد / اتسلّم / إلغاء / المرحلة الجاية).
class PrintOrderDetailScreen extends StatefulWidget {
  const PrintOrderDetailScreen({super.key, required this.orderId});
  final int orderId;

  @override
  State<PrintOrderDetailScreen> createState() => _PrintOrderDetailScreenState();
}

class _PrintOrderDetailScreenState extends State<PrintOrderDetailScreen> {
  Map<String, dynamic>? _order;
  Object? _error;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final o = await context.read<AuthProvider>().apiService.printOrder(widget.orderId);
      if (mounted) {
        setState(() {
          _order = o;
          _error = null;
        });
      }
    } catch (e) {
      if (mounted) setState(() => _error = e);
    }
  }

  void _toast(String msg, {bool error = false}) {
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(msg), backgroundColor: error ? Colors.redAccent : null));
  }

  Future<void> _runAction(String action) async {
    final label = tr(printOrderActions[action] ?? action);
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(label),
        content: Text(action == 'deliver'
            ? tr('كل البنود هتتعلّم «خلصت» والطلب يتحسب إيراد. متأكد؟')
            : trf('تأكيد: {action}؟', {'action': label})),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: Text(tr('إلغاء'))),
          TextButton(onPressed: () => Navigator.pop(ctx, true), child: Text(label)),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    setState(() => _busy = true);
    try {
      final o = await context.read<AuthProvider>().apiService.printOrderAction(widget.orderId, action);
      if (!mounted) return;
      setState(() => _order = o);
      final warnings = (o['warnings'] as List? ?? const []).map((e) => '$e').toList();
      _toast(warnings.isNotEmpty ? warnings.join('\n') : trf('تم: {action}', {'action': label}));
    } catch (e) {
      if (mounted) _toast('$e', error: true);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _advance(Map<String, dynamic> job) async {
    setState(() => _busy = true);
    try {
      await context.read<AuthProvider>().apiService.advancePrintJob(job['id'] as int);
      await _load();
    } catch (e) {
      if (mounted) _toast('$e', error: true);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _proofs(Map<String, dynamic> job) async {
    final api = context.read<AuthProvider>().apiService;
    try {
      final proofs = await api.printJobProofs(job['id'] as int);
      if (!mounted) return;
      showModalBottomSheet(
        context: context,
        builder: (ctx) => SafeArea(
          child: proofs.isEmpty
              ? Padding(
                  padding: const EdgeInsets.all(24),
                  child: Text(tr('مفيش بروفات على البند ده — ارفعها من الويب.')))
              : ListView(
                  shrinkWrap: true,
                  children: [
                    for (final p in proofs)
                      ListTile(
                        leading:
                            StatusChip(status: '${p['status']}', label: trf('نسخة {v}', {'v': p['version']})),
                        title: Text(displayValue(p['status_display'])),
                        subtitle: (p['customer_comment'] ?? '').toString().isNotEmpty
                            ? Text('${p['customer_comment']}')
                            : null,
                        trailing: IconButton(
                          tooltip: tr('نسخ رابط البروفة للعميل'),
                          icon: const Icon(Icons.link),
                          onPressed: () {
                            Clipboard.setData(ClipboardData(text: '${p['share_url']}'));
                            Navigator.pop(ctx);
                            _toast(tr('اتنسخ رابط البروفة — ابعته للعميل على واتساب.'));
                          },
                        ),
                      ),
                  ],
                ),
        ),
      );
    } catch (e) {
      _toast('$e', error: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final o = _order;
    return Scaffold(
      appBar: AppBar(title: Text(o == null ? tr('الطلب') : '#${o['order_number']}')),
      body: _error != null
          ? StateView.error('$_error', onRetry: _load)
          : o == null
              ? const StateView.loading()
              : RefreshIndicator(onRefresh: _load, child: _body(o)),
    );
  }

  Widget _body(Map<String, dynamic> o) {
    final tenant = context.watch<AuthProvider>().tenant;
    final actions = (o['allowed_actions'] as List? ?? const []).map((e) => '$e').toList();
    final canChangeOrder = tenant.can('change_printorder');
    final canMoveJobs = tenant.can('change_printjob');
    final jobs = (o['jobs'] as List? ?? const []).cast<Map<String, dynamic>>();
    final remaining = num.tryParse('${o['remaining']}') ?? 0;
    return ListView(
      padding: const EdgeInsets.all(12),
      children: [
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(children: [
                  Expanded(
                    child: Text(displayValue(o['customer_name']),
                        style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
                  ),
                  StatusChip(
                    status: '${o['status']}',
                    label: tr(printOrderStatuses['${o['status']}'] ?? '${o['status_display']}'),
                  ),
                ]),
                if ((o['customer_phone'] ?? '').toString().isNotEmpty)
                  Text('${o['customer_phone']}',
                      textDirection: TextDirection.ltr, style: TextStyle(color: Colors.grey.shade700)),
                const Divider(height: 24),
                _kv(tr('الإجمالي'), moneyOrDash(o['net_total'])),
                if ((num.tryParse('${o['discount']}') ?? 0) > 0) _kv(tr('الخصم'), moneyOrDash(o['discount'])),
                _kv(tr('المدفوع'), moneyOrDash(o['paid_amount'])),
                _kv(tr('الباقي'), moneyOrDash(o['remaining']),
                    color: remaining > 0 ? Colors.redAccent : Colors.green),
                if (o['date_due'] != null) _kv(tr('موعد التسليم'), displayValue(o['date_due'])),
                if (o['date_delivered'] != null) _kv(tr('اتسلّم في'), displayValue(o['date_delivered'])),
                if ((o['notes'] ?? '').toString().isNotEmpty) _kv(tr('ملاحظات'), '${o['notes']}'),
              ],
            ),
          ),
        ),
        const SizedBox(height: 8),
        Text(trf('البنود ({n})', {'n': jobs.length}),
            style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
        for (final j in jobs)
          _jobCard(j,
              canMove: canMoveJobs && ['confirmed', 'in_progress', 'ready'].contains(o['status']),
              canProofs: tenant.can('view_printproof')),
        const SizedBox(height: 12),
        if (canChangeOrder)
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final a in actions)
                SizedBox(
                  width: (MediaQuery.of(context).size.width - 40) / (actions.length > 1 ? 2 : 1),
                  child: ElevatedButton.icon(
                    style: ElevatedButton.styleFrom(
                      backgroundColor: a == 'cancel'
                          ? Colors.redAccent
                          : a == 'deliver'
                              ? Colors.green
                              : null,
                    ),
                    onPressed: _busy ? null : () => _runAction(a),
                    icon: Icon(a == 'cancel'
                        ? Icons.cancel
                        : a == 'deliver'
                            ? Icons.local_shipping
                            : Icons.check),
                    label: Text(tr(printOrderActions[a] ?? a)),
                  ),
                ),
            ],
          ),
        if (o['status'] != 'cancelled' && remaining > 0)
          Padding(
            padding: const EdgeInsets.only(top: 12),
            child: Text(tr('التحصيل والمرتجعات ورد الفلوس من الويب عشان الخزنة والدفاتر.'),
                style: TextStyle(color: Colors.grey.shade600, fontSize: 12), textAlign: TextAlign.center),
          ),
        const SizedBox(height: 24),
      ],
    );
  }

  Widget _jobCard(Map<String, dynamic> j, {required bool canMove, required bool canProofs}) {
    final next = j['next_stage'] as String?;
    final blocked = j['blocked'] == true;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              Expanded(
                child: Text('${j['description']}', style: const TextStyle(fontWeight: FontWeight.w600)),
              ),
              StatusChip(
                  status: '${j['stage']}',
                  label: tr(productionStages['${j['stage']}'] ?? '${j['stage_display']}')),
            ]),
            const SizedBox(height: 4),
            Text('${j['quantity']} × ${moneyOrDash(j['unit_price'])} = ${moneyOrDash(j['total_price'])}',
                style: TextStyle(color: Colors.grey.shade700)),
            if (j['machine_name'] != null)
              Text(trf('الماكينة: {m}', {'m': j['machine_name']}),
                  style: TextStyle(color: Colors.grey.shade700)),
            if (blocked)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(tr('⏳ مستني موافقة العميل على البروفة'),
                    style: TextStyle(color: Colors.amber.shade900, fontWeight: FontWeight.w600)),
              ),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                if (canProofs)
                  TextButton.icon(
                    onPressed: () => _proofs(j),
                    icon: const Icon(Icons.fact_check_outlined, size: 18),
                    label: Text(tr('البروفات')),
                  ),
                if (canMove && next != null && !blocked)
                  TextButton.icon(
                    onPressed: _busy ? null : () => _advance(j),
                    icon: const Icon(Icons.arrow_forward, size: 18),
                    label: Text(trf('لـ «{stage}»', {'stage': tr(productionStages[next] ?? next)})),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _kv(String k, String v, {Color? color}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(k, style: TextStyle(color: Colors.grey.shade700)),
            Flexible(
              child: Text(v,
                  textAlign: TextAlign.end, style: TextStyle(fontWeight: FontWeight.bold, color: color)),
            ),
          ],
        ),
      );
}
