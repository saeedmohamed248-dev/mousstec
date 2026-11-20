import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/i18n.dart';
import '../../providers/auth_provider.dart';
import '../../widgets/common.dart';
import 'print_constants.dart';
import 'print_order_detail_screen.dart';
import 'print_order_form_screen.dart';

/// 🧾 طلبات/فواتير المطبعة: فلتر بالحالة + بحث + طلب جديد.
class PrintOrdersScreen extends StatefulWidget {
  const PrintOrdersScreen({super.key, this.initialStatus = 'open'});
  final String initialStatus;

  @override
  State<PrintOrdersScreen> createState() => _PrintOrdersScreenState();
}

class _PrintOrdersScreenState extends State<PrintOrdersScreen> {
  final List<Map<String, dynamic>> _items = [];
  final _scroll = ScrollController();
  late String _status = widget.initialStatus;
  String _search = '';
  int _page = 1;
  bool _hasNext = true;
  bool _loading = false;
  Object? _error;

  static const _filters = ['open', 'draft', 'ready', 'delivered', 'cancelled', ''];

  @override
  void initState() {
    super.initState();
    _reload();
    _scroll.addListener(() {
      if (_scroll.position.pixels >= _scroll.position.maxScrollExtent - 200) _loadMore();
    });
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _reload() async {
    setState(() {
      _items.clear();
      _page = 1;
      _hasNext = true;
      _error = null;
    });
    await _loadMore();
  }

  Future<void> _loadMore() async {
    if (_loading || !_hasNext) return;
    setState(() => _loading = true);
    try {
      final page = await context.read<AuthProvider>().apiService.rawList('/print/orders/', query: {
        'page': _page,
        if (_status.isNotEmpty) 'status': _status,
        if (_search.isNotEmpty) 'search': _search,
      });
      final rows = (page['results'] as List? ?? const []).cast<Map<String, dynamic>>();
      setState(() {
        _items.addAll(rows);
        _hasNext = page['next'] != null && rows.isNotEmpty;
        _page += 1;
      });
    } catch (e) {
      setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  String _filterLabel(String f) {
    if (f == 'open') return tr('المفتوحة');
    if (f.isEmpty) return tr('الكل');
    return tr(printOrderStatuses[f] ?? f);
  }

  Future<void> _newOrder() async {
    final created =
        await Navigator.push<int>(context, MaterialPageRoute(builder: (_) => const PrintOrderFormScreen()));
    if (created != null && mounted) {
      await Navigator.push(
          context, MaterialPageRoute(builder: (_) => PrintOrderDetailScreen(orderId: created)));
      _reload();
    }
  }

  @override
  Widget build(BuildContext context) {
    final canCreate = context.watch<AuthProvider>().tenant.can('add_printorder');
    return Scaffold(
      appBar: AppBar(title: Text(tr('الطلبات والفواتير'))),
      floatingActionButton: canCreate
          ? FloatingActionButton.extended(
              onPressed: _newOrder, icon: const Icon(Icons.add), label: Text(tr('فاتورة جديدة')))
          : null,
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 4),
            child: TextField(
              decoration: InputDecoration(
                hintText: tr('بحث برقم الطلب أو العميل…'),
                prefixIcon: const Icon(Icons.search),
              ),
              textInputAction: TextInputAction.search,
              onSubmitted: (v) {
                _search = v.trim();
                _reload();
              },
            ),
          ),
          SizedBox(
            height: 48,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 8),
              children: [
                for (final f in _filters)
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 4),
                    child: ChoiceChip(
                      label: Text(_filterLabel(f)),
                      selected: _status == f,
                      onSelected: (_) {
                        _status = f;
                        _reload();
                      },
                    ),
                  ),
              ],
            ),
          ),
          Expanded(child: RefreshIndicator(onRefresh: _reload, child: _body())),
        ],
      ),
    );
  }

  Widget _body() {
    if (_error != null && _items.isEmpty) {
      return ListView(children: [SizedBox(height: 400, child: StateView.error('$_error', onRetry: _reload))]);
    }
    if (_items.isEmpty && _loading) return const StateView.loading();
    if (_items.isEmpty) {
      return ListView(children: [SizedBox(height: 400, child: StateView.empty(tr('مفيش طلبات')))]);
    }
    return ListView.builder(
      controller: _scroll,
      padding: const EdgeInsets.all(8),
      itemCount: _items.length + (_hasNext ? 1 : 0),
      itemBuilder: (context, i) {
        if (i >= _items.length) {
          return const Padding(
              padding: EdgeInsets.all(16), child: Center(child: CircularProgressIndicator()));
        }
        final o = _items[i];
        final remaining = num.tryParse('${o['remaining']}') ?? 0;
        return Card(
          child: ListTile(
            onTap: () async {
              await Navigator.push(context,
                  MaterialPageRoute(builder: (_) => PrintOrderDetailScreen(orderId: o['id'] as int)));
              _reload();
            },
            title:
                Text(displayValue(o['customer_name']), style: const TextStyle(fontWeight: FontWeight.bold)),
            subtitle: Text([
              '#${o['order_number']}',
              if (o['date_due'] != null) trf('التسليم {date}', {'date': displayValue(o['date_due'])}),
              if (remaining > 0) trf('الباقي {amount}', {'amount': moneyOrDash(remaining)}),
            ].join(' • ')),
            trailing: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Text(moneyOrDash(o['net_total']), style: const TextStyle(fontWeight: FontWeight.bold)),
                const SizedBox(height: 4),
                StatusChip(
                  status: '${o['status']}',
                  label: tr(printOrderStatuses['${o['status']}'] ?? '${o['status_display']}'),
                ),
              ],
            ),
          ),
        );
      },
    );
  }
}
