import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/modules.dart';
import '../providers/auth_provider.dart';
import '../widgets/common.dart';
import 'generic_form_screen.dart';

/// تفاصيل عامة لسجل — تعرض كل الحقول مع أزرار تعديل/حذف حسب صلاحيات الموديول.
class GenericDetailScreen extends StatefulWidget {
  const GenericDetailScreen({super.key, required this.module, required this.itemId});
  final ModuleDef module;
  final int itemId;

  @override
  State<GenericDetailScreen> createState() => _GenericDetailScreenState();
}

class _GenericDetailScreenState extends State<GenericDetailScreen> {
  Map<String, dynamic>? _item;
  Object? _error;
  bool _changed = false;

  ModuleDef get m => widget.module;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final api = context.read<AuthProvider>().apiService;
      final data = await api.rawGet(m.endpoint, widget.itemId);
      setState(() => _item = data);
    } catch (e) {
      setState(() => _error = e);
    }
  }

  Future<void> _edit() async {
    final changed = await Navigator.push<bool>(
      context,
      MaterialPageRoute(builder: (_) => GenericFormScreen(module: m, item: _item)),
    );
    if (changed == true) {
      _changed = true;
      _load();
    }
  }

  Future<void> _runAction(ModuleAction a) async {
    String? reason;
    if (a.needsReason) {
      final ctrl = TextEditingController();
      reason = await showDialog<String>(
        context: context,
        builder: (_) => AlertDialog(
          title: Text(a.label),
          content: TextField(
            controller: ctrl,
            decoration: const InputDecoration(hintText: 'السبب (اختياري)'),
            maxLines: 2,
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context), child: const Text('إلغاء')),
            TextButton(onPressed: () => Navigator.pop(context, ctrl.text), child: Text(a.label)),
          ],
        ),
      );
      if (reason == null) return; // أُلغي
    } else if (a.confirm) {
      final ok = await showDialog<bool>(
        context: context,
        builder: (_) => AlertDialog(
          title: Text(a.label),
          content: Text('تأكيد: ${a.label}؟'),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('إلغاء')),
            TextButton(onPressed: () => Navigator.pop(context, true), child: Text(a.label)),
          ],
        ),
      );
      if (ok != true) return;
    }
    try {
      await context.read<AuthProvider>().apiService.rawAction(
            m.endpoint, widget.itemId, a.slug,
            body: reason != null && reason.isNotEmpty ? {'notes': reason} : null,
          );
      if (!mounted) return;
      _changed = true;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('تم: ${a.label}')));
      _load();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('$e'), backgroundColor: Colors.redAccent),
      );
    }
  }

  Future<void> _delete() async {
    final confirm = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('تأكيد الحذف'),
        content: const Text('هل تريد حذف هذا السجل نهائياً؟'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('إلغاء')),
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('حذف', style: TextStyle(color: Colors.red)),
          ),
        ],
      ),
    );
    if (confirm != true) return;
    try {
      await context.read<AuthProvider>().apiService.rawDelete(m.endpoint, widget.itemId);
      if (!mounted) return;
      Navigator.pop(context, true);
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('$e'), backgroundColor: Colors.redAccent),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return WillPopScope(
      onWillPop: () async {
        Navigator.pop(context, _changed);
        return false;
      },
      child: Scaffold(
        appBar: AppBar(
          title: Text(m.title),
          actions: [
            if (m.canEdit && _item != null)
              IconButton(icon: const Icon(Icons.edit), onPressed: _edit),
            if (m.canDelete && _item != null)
              IconButton(icon: const Icon(Icons.delete_outline), onPressed: _delete),
          ],
        ),
        body: _buildBody(),
      ),
    );
  }

  Widget _buildBody() {
    if (_error != null) return StateView.error('$_error', onRetry: _load);
    if (_item == null) return const StateView.loading();
    final entries = _item!.entries
        .where((e) => e.key != 'id' && e.value is! List && e.value is! Map)
        .toList();
    return ListView(
      padding: const EdgeInsets.all(12),
      children: [
        Card(
          child: Padding(
            padding: const EdgeInsets.all(8),
            child: Column(
              children: [
                for (final e in entries)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 8),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Expanded(
                          flex: 2,
                          child: Text(fieldLabels[e.key] ?? e.key,
                              style: TextStyle(color: Colors.grey.shade700)),
                        ),
                        Expanded(
                          flex: 3,
                          child: Text(displayValue(e.value),
                              textAlign: TextAlign.end,
                              style: const TextStyle(fontWeight: FontWeight.w600)),
                        ),
                      ],
                    ),
                  ),
              ],
            ),
          ),
        ),
        if (m.actions.isNotEmpty) ...[
          const SizedBox(height: 8),
          Row(
            children: [
              for (final a in m.actions) ...[
                Expanded(
                  child: ElevatedButton.icon(
                    style: ElevatedButton.styleFrom(backgroundColor: a.color),
                    onPressed: () => _runAction(a),
                    icon: Icon(a.icon, size: 18),
                    label: Text(a.label),
                  ),
                ),
                const SizedBox(width: 8),
              ],
            ],
          ),
        ],
      ],
    );
  }
}
