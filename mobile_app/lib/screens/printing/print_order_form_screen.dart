import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/i18n.dart';
import '../../providers/auth_provider.dart';
import '../../widgets/common.dart';

/// ⚡ فاتورة/طلب جديد من الموبايل — نفس «الفاتورة السريعة» في الويب:
/// العميل (مسجّل أو اسم + موبايل)، البنود، خصم، «اتسلّمت؟»، والمدفوع دلوقتي.
/// بيرجّع رقم الطلب الجديد لما يتحفظ.
class PrintOrderFormScreen extends StatefulWidget {
  const PrintOrderFormScreen({super.key});

  @override
  State<PrintOrderFormScreen> createState() => _PrintOrderFormScreenState();
}

class _Line {
  final desc = TextEditingController();
  final qty = TextEditingController(text: '1');
  final price = TextEditingController();

  void dispose() {
    desc.dispose();
    qty.dispose();
    price.dispose();
  }

  num get total => (int.tryParse(qty.text) ?? 0) * (num.tryParse(price.text) ?? 0);
}

class _PrintOrderFormScreenState extends State<PrintOrderFormScreen> {
  final _formKey = GlobalKey<FormState>();
  final _name = TextEditingController();
  final _phone = TextEditingController();
  final _discount = TextEditingController(text: '0');
  final _paid = TextEditingController(text: '0');
  final _notes = TextEditingController();
  final List<_Line> _lines = [_Line()];
  Map<String, dynamic>? _customer;
  List<Map<String, dynamic>> _treasuries = const [];
  int? _treasury;
  bool _delivered = false;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    final auth = context.read<AuthProvider>();
    if (auth.tenant.can('add_printtransaction')) {
      auth.apiService.printTreasuries().then((t) {
        if (!mounted) return;
        setState(() {
          _treasuries = t;
          _treasury = t.isNotEmpty ? t.first['id'] as int : null;
        });
      }).catchError((_) {});
    }
  }

  @override
  void dispose() {
    for (final c in [_name, _phone, _discount, _paid, _notes]) {
      c.dispose();
    }
    for (final l in _lines) {
      l.dispose();
    }
    super.dispose();
  }

  num get _subtotal => _lines.fold<num>(0, (s, l) => s + l.total);
  num get _net => _subtotal - (num.tryParse(_discount.text) ?? 0);

  Future<void> _pickCustomer() async {
    final api = context.read<AuthProvider>().apiService;
    final picked = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      isScrollControlled: true,
      builder: (ctx) => _CustomerPicker(load: (q) async {
        final page = await api.rawList('/print/customers/', query: {if (q.isNotEmpty) 'search': q});
        return (page['results'] as List? ?? const []).cast<Map<String, dynamic>>();
      }),
    );
    if (picked != null) {
      setState(() {
        _customer = picked;
        _name.text = '${picked['name']}';
        _phone.text = '${picked['phone'] ?? ''}';
      });
    }
  }

  Future<void> _save() async {
    if (!_formKey.currentState!.validate()) return;
    final paid = num.tryParse(_paid.text) ?? 0;
    final body = <String, dynamic>{
      if (_customer != null) 'customer': _customer!['id'],
      'customer_name': _name.text.trim(),
      'customer_phone': _phone.text.trim(),
      'lines': [
        for (final l in _lines)
          {
            'description': l.desc.text.trim(),
            'quantity': int.tryParse(l.qty.text) ?? 1,
            'unit_price': l.price.text.trim()
          },
      ],
      'discount': _discount.text.trim().isEmpty ? '0' : _discount.text.trim(),
      'delivered': _delivered,
      'paid': paid.toString(),
      if (paid > 0) 'treasury': _treasury,
      'notes': _notes.text.trim(),
    };
    setState(() => _saving = true);
    try {
      final o = await context.read<AuthProvider>().apiService.createPrintOrder(body);
      if (!mounted) return;
      final warnings = (o['warnings'] as List? ?? const []).map((e) => '$e').toList();
      if (warnings.isNotEmpty) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(warnings.join('\n'))));
      }
      Navigator.pop(context, o['id'] as int);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$e'), backgroundColor: Colors.redAccent));
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  String? _required(String? v) => (v == null || v.trim().isEmpty) ? tr('مطلوب') : null;

  String? _money(String? v) {
    if (v == null || v.trim().isEmpty) return null;
    final n = num.tryParse(v.trim());
    return (n == null || n < 0) ? tr('رقم مش مظبوط') : null;
  }

  @override
  Widget build(BuildContext context) {
    final canCollect = _treasuries.isNotEmpty;
    return Scaffold(
      appBar: AppBar(title: Text(tr('فاتورة جديدة'))),
      body: Form(
        key: _formKey,
        onChanged: () => setState(() {}),
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Row(children: [
              Expanded(
                child: Text(tr('العميل'), style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
              ),
              TextButton.icon(
                onPressed: _pickCustomer,
                icon: const Icon(Icons.person_search),
                label: Text(tr('اختار عميل مسجّل')),
              ),
            ]),
            TextFormField(
              controller: _name,
              decoration:
                  InputDecoration(labelText: tr('اسم العميل'), prefixIcon: const Icon(Icons.person_outline)),
              validator: (v) => _customer == null ? _required(v) : null,
              onChanged: (_) => _customer = null,
            ),
            const SizedBox(height: 12),
            TextFormField(
              controller: _phone,
              keyboardType: TextInputType.phone,
              textDirection: TextDirection.ltr,
              decoration: InputDecoration(labelText: tr('الموبايل'), prefixIcon: const Icon(Icons.phone)),
              onChanged: (_) => _customer = null,
            ),
            const SizedBox(height: 20),
            Text(tr('البنود'), style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
            for (var i = 0; i < _lines.length; i++) _lineCard(i),
            TextButton.icon(
              onPressed: () => setState(() => _lines.add(_Line())),
              icon: const Icon(Icons.add),
              label: Text(tr('بند كمان')),
            ),
            const SizedBox(height: 8),
            TextFormField(
              controller: _discount,
              keyboardType: const TextInputType.numberWithOptions(decimal: true),
              decoration: InputDecoration(labelText: tr('الخصم')),
              validator: _money,
            ),
            const SizedBox(height: 8),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              value: _delivered,
              onChanged: (v) => setState(() => _delivered = v),
              title: Text(tr('اتسلّمت للعميل خلاص')),
              subtitle: Text(tr('لو لأ، الطلب هيدخل الإنتاج (تصميم ← طباعة ← تشطيب).')),
            ),
            if (canCollect) ...[
              TextFormField(
                controller: _paid,
                keyboardType: const TextInputType.numberWithOptions(decimal: true),
                decoration: InputDecoration(labelText: tr('المدفوع دلوقتي')),
                validator: _money,
              ),
              const SizedBox(height: 8),
              DropdownButtonFormField<int>(
                initialValue: _treasury,
                decoration: InputDecoration(labelText: tr('الخزنة')),
                items: [
                  for (final t in _treasuries)
                    DropdownMenuItem(value: t['id'] as int, child: Text('${t['name']}')),
                ],
                onChanged: (v) => setState(() => _treasury = v),
              ),
            ],
            const SizedBox(height: 8),
            TextFormField(
              controller: _notes,
              maxLines: 2,
              decoration: InputDecoration(labelText: tr('ملاحظات')),
            ),
            const SizedBox(height: 16),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(tr('الإجمالي'), style: const TextStyle(fontWeight: FontWeight.bold)),
                    Text(moneyOrDash(_net),
                        style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 18)),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 12),
            ElevatedButton.icon(
              onPressed: _saving ? null : _save,
              icon: _saving
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                  : const Icon(Icons.check),
              label: Text(tr('حفظ الفاتورة')),
            ),
            const SizedBox(height: 24),
          ],
        ),
      ),
    );
  }

  Widget _lineCard(int i) {
    final l = _lines[i];
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          children: [
            Row(children: [
              Expanded(
                child: TextFormField(
                  controller: l.desc,
                  decoration: InputDecoration(
                      labelText: trf('البند {n}', {'n': i + 1}), hintText: tr('مثال: 500 كارت بزنس')),
                  validator: _required,
                ),
              ),
              if (_lines.length > 1)
                IconButton(
                  icon: const Icon(Icons.delete_outline, color: Colors.redAccent),
                  onPressed: () => setState(() => _lines.removeAt(i).dispose()),
                ),
            ]),
            const SizedBox(height: 8),
            Row(children: [
              Expanded(
                child: TextFormField(
                  controller: l.qty,
                  keyboardType: TextInputType.number,
                  decoration: InputDecoration(labelText: tr('الكمية')),
                  validator: (v) => (int.tryParse(v ?? '') ?? 0) < 1 ? tr('1 أو أكتر') : null,
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: TextFormField(
                  controller: l.price,
                  keyboardType: const TextInputType.numberWithOptions(decimal: true),
                  decoration: InputDecoration(labelText: tr('سعر الوحدة')),
                  validator: (v) => _required(v) ?? _money(v),
                ),
              ),
            ]),
          ],
        ),
      ),
    );
  }
}

class _CustomerPicker extends StatefulWidget {
  const _CustomerPicker({required this.load});
  final Future<List<Map<String, dynamic>>> Function(String query) load;

  @override
  State<_CustomerPicker> createState() => _CustomerPickerState();
}

class _CustomerPickerState extends State<_CustomerPicker> {
  late Future<List<Map<String, dynamic>>> _future = widget.load('');

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: EdgeInsets.only(bottom: MediaQuery.of(context).viewInsets.bottom),
        child: SizedBox(
          height: MediaQuery.of(context).size.height * 0.7,
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.all(12),
                child: TextField(
                  autofocus: true,
                  decoration: InputDecoration(
                      hintText: tr('بحث بالاسم أو الموبايل…'), prefixIcon: const Icon(Icons.search)),
                  onSubmitted: (q) => setState(() => _future = widget.load(q.trim())),
                ),
              ),
              Expanded(
                child: FutureBuilder<List<Map<String, dynamic>>>(
                  future: _future,
                  builder: (context, snap) {
                    if (snap.connectionState != ConnectionState.done) return const StateView.loading();
                    if (snap.hasError) return StateView.error('${snap.error}');
                    final rows = snap.data!;
                    if (rows.isEmpty) return StateView.empty(tr('مفيش عملاء بالاسم ده'));
                    return ListView(
                      children: [
                        for (final c in rows)
                          ListTile(
                            leading: const Icon(Icons.person),
                            title: Text('${c['name']}'),
                            subtitle: Text('${c['phone'] ?? ''}', textDirection: TextDirection.ltr),
                            onTap: () => Navigator.pop(context, c),
                          ),
                      ],
                    );
                  },
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
