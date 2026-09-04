import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/modules.dart';
import '../providers/auth_provider.dart';
import '../widgets/common.dart';
import 'generic_detail_screen.dart';
import 'generic_form_screen.dart';

/// قائمة عامة تُبنى تلقائياً من تعريف الموديول: بحث + ترقيم + إنشاء.
class GenericListScreen extends StatefulWidget {
  const GenericListScreen({super.key, required this.module});
  final ModuleDef module;

  @override
  State<GenericListScreen> createState() => _GenericListScreenState();
}

class _GenericListScreenState extends State<GenericListScreen> {
  final List<Map<String, dynamic>> _items = [];
  final _scroll = ScrollController();
  int _page = 1;
  bool _loading = false;
  bool _hasNext = true;
  String _search = '';
  Object? _error;

  ModuleDef get m => widget.module;

  @override
  void initState() {
    super.initState();
    _reload();
    _scroll.addListener(() {
      if (_scroll.position.pixels >= _scroll.position.maxScrollExtent - 200) {
        _loadMore();
      }
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
      final api = context.read<AuthProvider>().apiService;
      final page = await api.rawList(m.endpoint, query: {
        'page': _page,
        if (_search.isNotEmpty) 'search': _search,
      });
      final results = (page['results'] as List? ?? const [])
          .map((e) => e as Map<String, dynamic>)
          .toList();
      setState(() {
        _items.addAll(results);
        _hasNext = page['next'] != null && results.isNotEmpty;
        _page += 1;
        _error = null;
      });
    } catch (e) {
      setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _openForm([Map<String, dynamic>? item]) async {
    final changed = await Navigator.push<bool>(
      context,
      MaterialPageRoute(builder: (_) => GenericFormScreen(module: m, item: item)),
    );
    if (changed == true) _reload();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(m.title)),
      floatingActionButton: m.canCreate
          ? FloatingActionButton.extended(
              onPressed: () => _openForm(),
              icon: const Icon(Icons.add),
              label: const Text('إضافة'),
            )
          : null,
      body: Column(
        children: [
          if (m.searchable)
            Padding(
              padding: const EdgeInsets.all(12),
              child: TextField(
                decoration: InputDecoration(
                  hintText: 'بحث…',
                  prefixIcon: const Icon(Icons.search),
                  suffixIcon: _search.isNotEmpty
                      ? IconButton(
                          icon: const Icon(Icons.clear),
                          onPressed: () {
                            _search = '';
                            _reload();
                            FocusScope.of(context).unfocus();
                          },
                        )
                      : null,
                ),
                textInputAction: TextInputAction.search,
                onSubmitted: (v) {
                  _search = v.trim();
                  _reload();
                },
              ),
            ),
          Expanded(
            child: RefreshIndicator(
              onRefresh: _reload,
              child: _buildBody(),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildBody() {
    if (_error != null && _items.isEmpty) {
      return ListView(children: [
        SizedBox(height: 400, child: StateView.error('$_error', onRetry: _reload)),
      ]);
    }
    if (_items.isEmpty && _loading) {
      return const StateView.loading();
    }
    if (_items.isEmpty) {
      return ListView(children: const [SizedBox(height: 400, child: StateView.empty('لا توجد بيانات'))]);
    }
    return ListView.builder(
      controller: _scroll,
      padding: const EdgeInsets.all(8),
      itemCount: _items.length + (_hasNext ? 1 : 0),
      itemBuilder: (context, i) {
        if (i >= _items.length) {
          return const Padding(
            padding: EdgeInsets.all(16),
            child: Center(child: CircularProgressIndicator()),
          );
        }
        return _row(_items[i]);
      },
    );
  }

  Widget _row(Map<String, dynamic> item) {
    final subtitle = m.subtitleKeys
        .map((k) => displayValue(item[k]))
        .where((s) => s != '—')
        .join(' • ');
    final status = m.statusKey != null ? item[m.statusKey] : null;
    return Card(
      child: ListTile(
        onTap: () async {
          final changed = await Navigator.push<bool>(
            context,
            MaterialPageRoute(builder: (_) => GenericDetailScreen(module: m, itemId: item['id'] as int)),
          );
          if (changed == true) _reload();
        },
        title: Text(displayValue(item[m.titleKey]),
            maxLines: 1, overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontWeight: FontWeight.bold)),
        subtitle: subtitle.isEmpty ? null : Text(subtitle),
        trailing: _trailing(item, status),
      ),
    );
  }

  Widget? _trailing(Map<String, dynamic> item, dynamic status) {
    if (m.trailingKey != null) {
      final v = item[m.trailingKey];
      return Text(
        m.trailingMoney ? moneyOrDash(v) : displayValue(v),
        style: const TextStyle(fontWeight: FontWeight.bold),
      );
    }
    if (status != null) {
      return StatusChip(status: '$status', label: displayValue(status));
    }
    return const Icon(Icons.chevron_left);
  }
}
