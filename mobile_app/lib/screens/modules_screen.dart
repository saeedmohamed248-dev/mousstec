import 'package:flutter/material.dart';

import '../core/modules.dart';
import 'generic_list_screen.dart';
import 'settings_screen.dart';

/// شاشة «المزيد» — كل الموديولات مُجمّعة بالأقسام + الإعدادات.
class ModulesScreen extends StatelessWidget {
  const ModulesScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('كل الموديولات'),
        actions: [
          IconButton(
            icon: const Icon(Icons.settings),
            tooltip: 'الإعدادات',
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
          for (final group in moduleGroups) ...[
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 6),
              child: Text(group,
                  style: TextStyle(
                      fontWeight: FontWeight.bold, fontSize: 15, color: Colors.grey.shade700)),
            ),
            GridView.count(
              crossAxisCount: 2,
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              childAspectRatio: 2.6,
              padding: const EdgeInsets.symmetric(horizontal: 8),
              children: modulesInGroup(group).map((mod) => _ModuleTile(module: mod)).toList(),
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
                  color: module.color.withOpacity(0.12),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Icon(module.icon, color: module.color, size: 20),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Text(module.title,
                    style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13),
                    maxLines: 2, overflow: TextOverflow.ellipsis),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
