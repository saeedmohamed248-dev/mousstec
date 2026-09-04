import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../providers/auth_provider.dart';

class SettingsScreen extends StatelessWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final auth = context.watch<AuthProvider>();
    final user = auth.user;
    return Scaffold(
      appBar: AppBar(title: const Text('الإعدادات')),
      body: ListView(
        children: [
          const SizedBox(height: 12),
          Center(
            child: CircleAvatar(
              radius: 40,
              backgroundColor: Theme.of(context).colorScheme.primary.withOpacity(0.1),
              child: Text(
                (user?.fullName.isNotEmpty == true ? user!.fullName[0] : '?'),
                style: const TextStyle(fontSize: 32),
              ),
            ),
          ),
          const SizedBox(height: 8),
          Center(
            child: Text(user?.fullName ?? '',
                style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
          ),
          Center(child: Text('@${user?.username ?? ''}', style: const TextStyle(color: Colors.grey))),
          const Divider(height: 32),
          FutureBuilder<String>(
            future: auth.baseUrl,
            builder: (context, snap) => ListTile(
              leading: const Icon(Icons.link),
              title: const Text('رابط الورشة'),
              subtitle: Text(snap.data ?? '...', textDirection: TextDirection.ltr),
              trailing: const Icon(Icons.edit),
              onTap: () => _editUrl(context, auth, snap.data ?? ''),
            ),
          ),
          if (user?.email != null && user!.email!.isNotEmpty)
            ListTile(
              leading: const Icon(Icons.email_outlined),
              title: const Text('البريد الإلكتروني'),
              subtitle: Text(user.email!, textDirection: TextDirection.ltr),
            ),
          ListTile(
            leading: const Icon(Icons.badge_outlined),
            title: const Text('الصلاحية'),
            subtitle: Text(user?.isSuperuser == true
                ? 'مدير النظام'
                : user?.isStaff == true
                    ? 'موظف'
                    : 'مستخدم'),
          ),
          const Divider(height: 32),
          ListTile(
            leading: const Icon(Icons.logout, color: Colors.redAccent),
            title: const Text('تسجيل الخروج', style: TextStyle(color: Colors.redAccent)),
            onTap: () async {
              final confirm = await showDialog<bool>(
                context: context,
                builder: (_) => AlertDialog(
                  title: const Text('تسجيل الخروج'),
                  content: const Text('هل تريد تسجيل الخروج من التطبيق؟'),
                  actions: [
                    TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('إلغاء')),
                    TextButton(onPressed: () => Navigator.pop(context, true), child: const Text('خروج')),
                  ],
                ),
              );
              if (confirm == true) await auth.logout();
            },
          ),
          const SizedBox(height: 24),
          const Center(child: Text('Mouss Tec Mobile • v1.0.0', style: TextStyle(color: Colors.grey))),
        ],
      ),
    );
  }

  Future<void> _editUrl(BuildContext context, AuthProvider auth, String current) async {
    final ctrl = TextEditingController(text: current);
    final result = await showDialog<String>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('رابط الورشة'),
        content: TextField(
          controller: ctrl,
          keyboardType: TextInputType.url,
          textDirection: TextDirection.ltr,
          decoration: const InputDecoration(hintText: 'https://myshop.mousstec.com'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('إلغاء')),
          TextButton(onPressed: () => Navigator.pop(context, ctrl.text), child: const Text('حفظ')),
        ],
      ),
    );
    if (result != null && result.trim().isNotEmpty) {
      await auth.setBaseUrl(result);
    }
  }
}
