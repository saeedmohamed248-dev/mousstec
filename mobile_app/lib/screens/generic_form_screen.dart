import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../core/field_spec.dart';
import '../core/modules.dart';
import '../providers/auth_provider.dart';
import '../widgets/common.dart';

/// نموذج عام لإنشاء/تعديل سجل — يُبنى تلقائياً من حقول الموديول.
class GenericFormScreen extends StatefulWidget {
  const GenericFormScreen({super.key, required this.module, this.item});
  final ModuleDef module;
  final Map<String, dynamic>? item;

  @override
  State<GenericFormScreen> createState() => _GenericFormScreenState();
}

class _GenericFormScreenState extends State<GenericFormScreen> {
  final _formKey = GlobalKey<FormState>();
  final Map<String, TextEditingController> _text = {};
  final Map<String, dynamic> _values = {};
  final Map<String, List<Map<String, dynamic>>> _fkOptions = {};
  bool _saving = false;

  ModuleDef get m => widget.module;
  bool get isEdit => widget.item != null;

  @override
  void initState() {
    super.initState();
    for (final f in m.fields) {
      final initial = widget.item?[f.key];
      switch (f.type) {
        case FieldType.boolean:
          _values[f.key] = initial is bool ? initial : false;
          break;
        case FieldType.fk:
        case FieldType.choice:
          _values[f.key] = initial;
          if (f.type == FieldType.fk && f.fkEndpoint != null) _loadFk(f);
          break;
        case FieldType.date:
          _values[f.key] = initial;
          _text[f.key] = TextEditingController(text: initial?.toString() ?? '');
          break;
        default:
          _text[f.key] = TextEditingController(text: initial?.toString() ?? '');
      }
    }
  }

  @override
  void dispose() {
    for (final c in _text.values) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _loadFk(FieldSpec f) async {
    try {
      final opts = await context.read<AuthProvider>().apiService.fkOptions(f.fkEndpoint!);
      if (mounted) setState(() => _fkOptions[f.key] = opts);
    } catch (_) {}
  }

  Future<void> _save() async {
    if (!_formKey.currentState!.validate()) return;
    final body = <String, dynamic>{};
    for (final f in m.fields) {
      dynamic v;
      switch (f.type) {
        case FieldType.boolean:
        case FieldType.fk:
        case FieldType.choice:
        case FieldType.date:
          v = _values[f.key];
          break;
        default:
          final raw = _text[f.key]!.text.trim();
          v = raw.isEmpty ? null : raw;
      }
      if (v != null) body[f.key] = v;
    }

    setState(() => _saving = true);
    try {
      final api = context.read<AuthProvider>().apiService;
      if (isEdit) {
        await api.rawUpdate(m.endpoint, widget.item!['id'] as int, body);
      } else {
        await api.rawCreate(m.endpoint, body);
      }
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(isEdit ? 'تم الحفظ' : 'تمت الإضافة')),
      );
      Navigator.pop(context, true);
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('$e'), backgroundColor: Colors.redAccent),
      );
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('${isEdit ? 'تعديل' : 'إضافة'} — ${m.title}')),
      body: Form(
        key: _formKey,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            for (final f in m.fields) ...[
              _buildField(f),
              const SizedBox(height: 16),
            ],
            const SizedBox(height: 8),
            ElevatedButton.icon(
              onPressed: _saving ? null : _save,
              icon: _saving
                  ? const SizedBox(height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                  : const Icon(Icons.save),
              label: Text(isEdit ? 'حفظ التعديلات' : 'حفظ'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildField(FieldSpec f) {
    switch (f.type) {
      case FieldType.boolean:
        return SwitchListTile(
          title: Text(f.label),
          value: _values[f.key] == true,
          onChanged: (v) => setState(() => _values[f.key] = v),
          contentPadding: EdgeInsets.zero,
        );

      case FieldType.choice:
        return DropdownButtonFormField<String>(
          value: _values[f.key] as String?,
          decoration: InputDecoration(labelText: f.label),
          items: f.choices
              .map((c) => DropdownMenuItem(value: c.value, child: Text(c.label)))
              .toList(),
          onChanged: (v) => setState(() => _values[f.key] = v),
          validator: (v) => (f.required && v == null) ? 'مطلوب' : null,
        );

      case FieldType.fk:
        final opts = _fkOptions[f.key];
        if (opts == null) {
          return InputDecorator(
            decoration: InputDecoration(labelText: f.label),
            child: const Row(children: [
              SizedBox(height: 16, width: 16, child: CircularProgressIndicator(strokeWidth: 2)),
              SizedBox(width: 8),
              Text('جارٍ التحميل…'),
            ]),
          );
        }
        return DropdownButtonFormField<int>(
          value: _values[f.key] is int ? _values[f.key] as int : null,
          isExpanded: true,
          decoration: InputDecoration(labelText: f.label),
          items: opts
              .map((o) => DropdownMenuItem(
                    value: o['id'] as int,
                    child: Text(displayValue(o[f.fkLabelKey] ?? o['name'] ?? o['id']),
                        maxLines: 1, overflow: TextOverflow.ellipsis),
                  ))
              .toList(),
          onChanged: (v) => setState(() => _values[f.key] = v),
          validator: (v) => (f.required && v == null) ? 'مطلوب' : null,
        );

      case FieldType.date:
        return TextFormField(
          controller: _text[f.key],
          readOnly: true,
          decoration: InputDecoration(
            labelText: f.label,
            suffixIcon: const Icon(Icons.calendar_today),
          ),
          onTap: () async {
            final now = DateTime.now();
            final picked = await showDatePicker(
              context: context,
              initialDate: now,
              firstDate: DateTime(now.year - 5),
              lastDate: DateTime(now.year + 5),
            );
            if (picked != null) {
              final iso = picked.toIso8601String().substring(0, 10);
              _text[f.key]!.text = iso;
              _values[f.key] = iso;
            }
          },
          validator: (v) => (f.required && (v == null || v.isEmpty)) ? 'مطلوب' : null,
        );

      default:
        final isNum = f.type == FieldType.integer || f.type == FieldType.decimal;
        return TextFormField(
          controller: _text[f.key],
          keyboardType: f.type == FieldType.multiline
              ? TextInputType.multiline
              : isNum
                  ? const TextInputType.numberWithOptions(decimal: true)
                  : TextInputType.text,
          maxLines: f.type == FieldType.multiline ? 3 : 1,
          inputFormatters: f.type == FieldType.integer
              ? [FilteringTextInputFormatter.digitsOnly]
              : f.type == FieldType.decimal
                  ? [FilteringTextInputFormatter.allow(RegExp(r'[0-9.]'))]
                  : null,
          decoration: InputDecoration(labelText: f.label + (f.required ? ' *' : '')),
          validator: (v) => (f.required && (v == null || v.trim().isEmpty)) ? 'مطلوب' : null,
        );
    }
  }
}
