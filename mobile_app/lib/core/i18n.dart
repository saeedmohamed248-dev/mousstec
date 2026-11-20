import 'package:flutter/widgets.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'l10n_en.dart';

/// 🌐 لغة التطبيق (عربي/إنجليزي) — قابلة للتغيير من الإعدادات وبتتحفظ على الجهاز.
///
/// النصوص مكتوبة بالعربي في الكود (زي ما هي)، و`tr()` بترجّع ترجمتها الإنجليزي
/// من [kEnglish] لو اللغة إنجليزي. أي نص مالوش ترجمة بيظهر عربي (مفيش كراش).
class AppLanguage extends ChangeNotifier {
  AppLanguage._();
  static final AppLanguage instance = AppLanguage._();

  static const _key = 'app_language';
  String _code = 'ar';

  String get code => _code;
  bool get isEnglish => _code == 'en';
  Locale get locale => Locale(_code);
  TextDirection get direction => isEnglish ? TextDirection.ltr : TextDirection.rtl;

  Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString(_key);
    if (saved == 'ar' || saved == 'en') _code = saved!;
  }

  Future<void> set(String code) async {
    if (code != 'ar' && code != 'en') return;
    _code = code;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_key, code);
    notifyListeners();
  }

  /// لاختبارات الوحدة.
  @visibleForTesting
  void setForTest(String code) {
    _code = code;
    notifyListeners();
  }
}

/// ترجمة نص عربي للغة الحالية.
String tr(String ar) => AppLanguage.instance.isEnglish ? (kEnglish[ar] ?? ar) : ar;

/// ترجمة مع متغيرات: `trf('أهلاً، {name}', {'name': 'سعيد'})`.
String trf(String ar, Map<String, Object?> args) {
  var out = tr(ar);
  args.forEach((k, v) => out = out.replaceAll('{$k}', '${v ?? ''}'));
  return out;
}
