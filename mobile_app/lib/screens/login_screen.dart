import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/constants.dart';
import '../core/i18n.dart';
import '../core/theme.dart';
import '../providers/auth_provider.dart';

/// 🔐 الدخول: كود الشركة (أو رابطها) + اسم المستخدم + كلمة المرور.
///
/// 🐛 [FIX]: رابط الشركة كان مخفي في «إعدادات الخادم» ولازم يتكتب كامل بـ https://
/// (وافتراضياً demo.mousstec.com) — أغلب الناس كانوا بيدخلوا على الديمو ويفشلوا.
class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _userCtrl = TextEditingController();
  final _passCtrl = TextEditingController();
  final _workspaceCtrl = TextEditingController();
  bool _obscure = true;

  @override
  void initState() {
    super.initState();
    context.read<AuthProvider>().baseUrl.then((v) {
      if (mounted && v != ApiConfig.defaultBaseUrl) _workspaceCtrl.text = ApiConfig.workspaceCode(v);
    });
  }

  @override
  void dispose() {
    _userCtrl.dispose();
    _passCtrl.dispose();
    _workspaceCtrl.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    final auth = context.read<AuthProvider>();
    await auth.setWorkspace(_workspaceCtrl.text);
    final ok = await auth.login(_userCtrl.text.trim(), _passCtrl.text);
    if (!ok && mounted && auth.error != null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(auth.error!), backgroundColor: Colors.redAccent),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final auth = context.watch<AuthProvider>();
    final lang = AppLanguage.instance;
    return Scaffold(
      body: SafeArea(
        child: Stack(
          children: [
            Align(
              alignment: AlignmentDirectional.topEnd,
              child: TextButton.icon(
                onPressed: () => lang.set(lang.isEnglish ? 'ar' : 'en'),
                icon: const Icon(Icons.language),
                label: Text(lang.isEnglish ? 'العربية' : 'English'),
              ),
            ),
            Center(
              child: SingleChildScrollView(
                padding: const EdgeInsets.all(24),
                child: Form(
                  key: _formKey,
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      ClipRRect(
                        borderRadius: BorderRadius.circular(24),
                        child: Image.asset('assets/icon/icon.png',
                            width: 88,
                            height: 88,
                            errorBuilder: (_, __, ___) =>
                                const Icon(Icons.business, size: 72, color: AppTheme.primary)),
                      ),
                      const SizedBox(height: 12),
                      const Text('Mouss Tec',
                          style:
                              TextStyle(fontSize: 30, fontWeight: FontWeight.bold, color: AppTheme.primary)),
                      Text(tr('إدارة الورش والمطابع من موبايلك'), style: const TextStyle(color: Colors.grey)),
                      const SizedBox(height: 32),
                      TextFormField(
                        controller: _workspaceCtrl,
                        keyboardType: TextInputType.url,
                        textDirection: TextDirection.ltr,
                        autocorrect: false,
                        textInputAction: TextInputAction.next,
                        decoration: InputDecoration(
                          labelText: tr('كود الشركة'),
                          hintText: 'myshop',
                          helperText: tr('اللي قبل .mousstec.com في رابط شركتك — أو الرابط كامل'),
                          prefixIcon: const Icon(Icons.business),
                        ),
                        validator: (v) {
                          if (v == null || v.trim().isEmpty) return tr('اكتب كود الشركة');
                          if (ApiConfig.workspaceUrl(v) == null) return tr('كود أو رابط غير صالح');
                          return null;
                        },
                      ),
                      const SizedBox(height: 16),
                      TextFormField(
                        controller: _userCtrl,
                        textInputAction: TextInputAction.next,
                        autocorrect: false,
                        decoration: InputDecoration(
                          labelText: tr('اسم المستخدم'),
                          prefixIcon: const Icon(Icons.person_outline),
                        ),
                        validator: (v) => (v == null || v.trim().isEmpty) ? tr('أدخل اسم المستخدم') : null,
                      ),
                      const SizedBox(height: 16),
                      TextFormField(
                        controller: _passCtrl,
                        obscureText: _obscure,
                        decoration: InputDecoration(
                          labelText: tr('كلمة المرور'),
                          prefixIcon: const Icon(Icons.lock_outline),
                          suffixIcon: IconButton(
                            icon: Icon(_obscure ? Icons.visibility : Icons.visibility_off),
                            onPressed: () => setState(() => _obscure = !_obscure),
                          ),
                        ),
                        validator: (v) => (v == null || v.isEmpty) ? tr('أدخل كلمة المرور') : null,
                        onFieldSubmitted: (_) => _submit(),
                      ),
                      const SizedBox(height: 24),
                      ElevatedButton(
                        onPressed: auth.busy ? null : _submit,
                        child: auth.busy
                            ? const SizedBox(
                                height: 22,
                                width: 22,
                                child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                            : Text(tr('تسجيل الدخول'), style: const TextStyle(fontSize: 16)),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
