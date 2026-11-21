import 'package:flutter/material.dart';

import 'package:provider/provider.dart';

import '../core/i18n.dart';
import '../core/modules.dart';
import '../providers/auth_provider.dart';
import 'generic_list_screen.dart';
import 'settings_screen.dart';

/// شاشة «المزيد» — كل الموديولات مُجمّعة بالأقسام + الإعدادات.
class ModulesScreen extends StatelessWidget {
  const ModulesScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final tenant = context.watch<AuthProvider>().tenant;
    final groups = (moduleGroupsByIndustry[tenant.industry] ?? moduleGroups)
        .where((g) => modulesInGroup(g, tenant).isNotEmpty)
        .toList();
    return Scaffold(
      appBar: AppBar(
        title: Text(tr('كل الموديولات')),
        actions: [
          IconButton(
            icon: const Icon(Icons.settings),
            tooltip: tr('الإعدادات'),
            onPressed: () => Navigator.push(
              context,
              MaterialPageRoute(builder: (_) => const SettingsScreen()),
            ),
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          if (groups.isEmpty)
            Padding(
              padding: const EdgeInsets.all(32),
              child: Text(tr('مفيش أقسام متاحة لصلاحياتك — كلّم صاحب الشركة.'), textAlign: TextAlign.center),
            ),
          for (final group in groups) ...[
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 6),
              child: Text(tr(group),
                  style: TextStyle(fontWeight: FontWeight.bold, fontSize: 15, color: Colors.grey.shade700)),
            ),
            GridView.count(
              crossAxisCount: 2,
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              childAspectRatio: 2.6,
              padding: const EdgeInsets.symmetric(horizontal: 8),
              children: modulesInGroup(group, tenant).map((mod) => _ModuleTile(module: mod)).toList(),
            ),
          ],
          const SizedBox(height: 24),
        ],
      ),
    );
  }
}

class _ModuleTile extends StatelessWidget {
  const _ModuleTile({required this.module});
  final ModuleDef module;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(16),
        onTap: () => Navigator.push(
          context,
          MaterialPageRoute(builder: (_) => GenericListScreen(module: module)),
        ),
        child: Padding(
          padding: const EdgeInsets.all(10),
          child: Row(
            children: [
              Container(
                padding: const EdgeInsets.all(8),
                decoration: BoxDecoration(
                  color: module.color.withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Icon(module.icon, color: module.color, size: 20),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Text(tr(module.title),
                    style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13),
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
