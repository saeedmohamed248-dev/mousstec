import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/i18n.dart';
import '../providers/auth_provider.dart';
import 'dashboard_screen.dart';
import 'inventory_screen.dart';
import 'modules_screen.dart';
import 'printing/print_dashboard_screen.dart';
import 'printing/print_orders_screen.dart';
import 'printing/production_board_screen.dart';
import 'work_orders_screen.dart';

/// الهيكل الرئيسي مع شريط تنقّل سفلي — حسب قطاع الشركة:
///   🚗 الورش:   الرئيسية • أوامر الشغل • المخزون • المزيد
///   🎨 المطابع: الرئيسية • الطلبات • الإنتاج • المزيد
class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _Tab {
  const _Tab(this.screen, this.icon, this.selectedIcon, this.label);
  final Widget screen;
  final IconData icon;
  final IconData selectedIcon;
  final String label;
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;

  List<_Tab> _tabs(AuthProvider auth) {
    if (auth.isPrinting) {
      final t = auth.tenant;
      return [
        if (t.can('view_printorder'))
          const _Tab(PrintDashboardScreen(), Icons.dashboard_outlined, Icons.dashboard, 'الرئيسية'),
        if (t.can('view_printorder'))
          const _Tab(PrintOrdersScreen(), Icons.receipt_long_outlined, Icons.receipt_long, 'الطلبات'),
        if (t.can('view_printjob'))
          const _Tab(ProductionBoardScreen(), Icons.precision_manufacturing_outlined,
              Icons.precision_manufacturing, 'الإنتاج'),
        _Tab(ModulesScreen(), Icons.apps_outlined, Icons.apps, 'المزيد'),
      ];
    }
    return [
      _Tab(DashboardScreen(), Icons.dashboard_outlined, Icons.dashboard, 'الرئيسية'),
      _Tab(WorkOrdersScreen(), Icons.build_outlined, Icons.build, 'أوامر الشغل'),
      _Tab(InventoryScreen(), Icons.inventory_2_outlined, Icons.inventory_2, 'المخزون'),
      _Tab(ModulesScreen(), Icons.apps_outlined, Icons.apps, 'المزيد'),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final tabs = _tabs(context.watch<AuthProvider>());
    if (_index >= tabs.length) _index = 0;
    return Scaffold(
      body: IndexedStack(index: _index, children: [for (final t in tabs) t.screen]),
      bottomNavigationBar: tabs.length < 2
          ? null
          : NavigationBar(
              selectedIndex: _index,
              onDestinationSelected: (i) => setState(() => _index = i),
              destinations: [
                for (final t in tabs)
                  NavigationDestination(
                      icon: Icon(t.icon), selectedIcon: Icon(t.selectedIcon), label: tr(t.label)),
              ],
            ),
    );
  }
}
