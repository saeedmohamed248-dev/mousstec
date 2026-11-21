import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/constants.dart';
import '../core/i18n.dart';
import '../providers/auth_provider.dart';

class SettingsScreen extends StatelessWidget {
  const SettingsScreen({super.key});

  static const appVersion = '1.1.0';

  @override
  Widget build(BuildContext context) {
    final auth = context.watch<AuthProvider>();
    final user = auth.user;
    final tenant = auth.tenant;
    final lang = AppLanguage.instance;
    return Scaffold(
      appBar: AppBar(title: Text(tr('الإعدادات'))),
      body: ListView(
        children: [
          const SizedBox(height: 12),
          Center(
            child: CircleAvatar(
              radius: 40,
              backgroundColor: Theme.of(context).colorScheme.primary.withValues(alpha: 0.1),
              child: Text(
                (user?.fullName.isNotEmpty == true ? user!.fullName[0] : '?'),
                style: const TextStyle(fontSize: 32),
              ),
            ),
          ),
          const SizedBox(height: 8),
          Center(
            child:
                Text(user?.fullName ?? '', style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
          ),
          Center(child: Text('@${user?.username ?? ''}', style: const TextStyle(color: Colors.grey))),
          const Divider(height: 32),
          if (tenant.name.isNotEmpty)
            ListTile(
              leading: Icon(tenant.isPrinting ? Icons.print : Icons.car_repair),
              title: Text(tenant.name),
              subtitle: Text(tenant.isPrinting ? tr('مطبعة / تصميم') : tr('ورشة / قطع غيار')),
            ),
          FutureBuilder<String>(
            future: auth.baseUrl,
            builder: (context, snap) => ListTile(
              leading: const Icon(Icons.business),
              title: Text(tr('كود الشركة')),
              subtitle: Text(ApiConfig.workspaceCode(snap.data ?? '...'), textDirection: TextDirection.ltr),
              trailing: const Icon(Icons.edit),
              onTap: () => _editWorkspace(context, auth, snap.data ?? ''),
            ),
          ),
          ListTile(
            leading: const Icon(Icons.language),
            title: Text(tr('اللغة')),
            trailing: SegmentedButton<String>(
              segments: const [
                ButtonSegment(value: 'ar', label: Text('عربي')),
                ButtonSegment(value: 'en', label: Text('English')),
              ],
              selected: {lang.code},
              onSelectionChanged: (s) => lang.set(s.first),
            ),
          ),
          if (user?.email != null && user!.email!.isNotEmpty)
            ListTile(
              leading: const Icon(Icons.email_outlined),
              title: Text(tr('البريد الإلكتروني')),
              subtitle: Text(user.email!, textDirection: TextDirection.ltr),
            ),
          ListTile(
            leading: const Icon(Icons.badge_outlined),
            title: Text(tr('الصلاحية')),
            subtitle: Text(user?.isSuperuser == true
                ? tr('مدير النظام')
                : user?.isStaff == true
                    ? tr('موظف')
                    : tr('مستخدم')),
          ),
          const Divider(height: 32),
          ListTile(
            leading: const Icon(Icons.logout, color: Colors.redAccent),
            title: Text(tr('تسجيل الخروج'), style: const TextStyle(color: Colors.redAccent)),
            onTap: () async {
              final confirm = await showDialog<bool>(
                context: context,
                builder: (ctx) => AlertDialog(
                  title: Text(tr('تسجيل الخروج')),
                  content: Text(tr('هل تريد تسجيل الخروج من التطبيق؟')),
                  actions: [
                    TextButton(onPressed: () => Navigator.pop(ctx, false), child: Text(tr('إلغاء'))),
                    TextButton(onPressed: () => Navigator.pop(ctx, true), child: Text(tr('خروج'))),
                  ],
                ),
              );
              if (confirm == true) {
                await auth.logout();
                if (context.mounted) Navigator.of(context).popUntil((r) => r.isFirst);
              }
            },
          ),
          const SizedBox(height: 24),
          const Center(child: Text('Mouss Tec Mobile • v$appVersion', style: TextStyle(color: Colors.grey))),
          const SizedBox(height: 24),
        ],
      ),
    );
  }

  Future<void> _editWorkspace(BuildContext context, AuthProvider auth, String current) async {
    final ctrl = TextEditingController(text: ApiConfig.workspaceCode(current));
    final result = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(tr('كود الشركة')),
        content: TextField(
          controller: ctrl,
          keyboardType: TextInputType.url,
          textDirection: TextDirection.ltr,
          decoration: const InputDecoration(hintText: 'myshop'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: Text(tr('إلغاء'))),
          TextButton(onPressed: () => Navigator.pop(ctx, ctrl.text), child: Text(tr('حفظ'))),
        ],
      ),
    );
    if (result == null || result.trim().isEmpty) return;
    final url = ApiConfig.workspaceUrl(result);
    if (url == null) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text(tr('كود أو رابط غير صالح')), backgroundColor: Colors.redAccent));
      }
      return;
    }
    if (url == current) return;
    // شركة تانية = حساب تاني: التوكن بتاع الشركة القديمة مايشتغلش هناك
    await auth.logout();
    await auth.setWorkspace(result);
    if (context.mounted) Navigator.of(context).popUntil((r) => r.isFirst);
  }
}
