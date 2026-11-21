import 'package:flutter_test/flutter_test.dart';
import 'package:mousstec_mobile/core/constants.dart';
import 'package:mousstec_mobile/core/i18n.dart';
import 'package:mousstec_mobile/core/l10n_en.dart';
import 'package:mousstec_mobile/core/modules.dart';
import 'package:mousstec_mobile/models/models.dart';
import 'package:mousstec_mobile/widgets/common.dart';

void main() {
  group('workspaceUrl', () {
    test('company code becomes its subdomain', () {
      expect(ApiConfig.workspaceUrl('MyShop'), 'https://myshop.mousstec.com');
      expect(ApiConfig.workspaceUrl(' print-house '), 'https://print-house.mousstec.com');
    });
    test('full urls and custom domains are kept (path stripped)', () {
      expect(ApiConfig.workspaceUrl('https://myshop.mousstec.com/'), 'https://myshop.mousstec.com');
      expect(ApiConfig.workspaceUrl('erp.example.com'), 'https://erp.example.com');
      expect(ApiConfig.workspaceUrl('http://localhost:8000/admin'), 'http://localhost:8000');
    });
    test('garbage is rejected', () {
      expect(ApiConfig.workspaceUrl(''), isNull);
      expect(ApiConfig.workspaceUrl('bad code!'), isNull);
      expect(ApiConfig.workspaceUrl('ftp://x.com'), isNull);
    });
    test('workspaceCode reverses it', () {
      expect(ApiConfig.workspaceCode('https://myshop.mousstec.com'), 'myshop');
      expect(ApiConfig.workspaceCode('https://erp.example.com'), 'erp.example.com');
    });
  });

  group('i18n', () {
    tearDown(() => AppLanguage.instance.setForTest('ar'));

    test('arabic is the default and returned as-is', () {
      expect(tr('الإعدادات'), 'الإعدادات');
    });
    test('english returns the translation, unknown text falls back to arabic', () {
      AppLanguage.instance.setForTest('en');
      expect(tr('الإعدادات'), 'Settings');
      expect(tr('نص مش مترجم'), 'نص مش مترجم');
      expect(trf('أهلاً، {name} 👋', {'name': 'Saeed'}), 'Hi, Saeed 👋');
    });
    test('every module title, group and field label has an english translation', () {
      final missing = <String>{
        for (final m in moduleRegistry) ...[
          m.title,
          m.group,
          for (final f in m.fields) f.label,
          for (final f in m.fields)
            for (final c in f.choices) c.label,
          for (final a in m.actions) a.label,
        ],
        for (final g in moduleGroupsByIndustry.values.expand((x) => x)) g,
        ...fieldLabels.values,
      }.where((s) => !kEnglish.containsKey(s)).toList();
      expect(missing, isEmpty, reason: 'ناقص ترجمة: $missing');
    });
  });

  group('sector', () {
    test('printing tenant sees printing modules only, filtered by permission', () {
      const t = TenantInfo(name: 'x', industry: 'printing', permissions: {'view_printcustomer'});
      final visible = moduleRegistry.where((m) => m.visibleFor(t)).map((m) => m.key).toList();
      expect(visible, ['print-customers']);
    });
    test('workshop tenant never sees printing modules', () {
      final visible = moduleRegistry.where((m) => m.visibleFor(TenantInfo.automotive));
      expect(visible.any((m) => m.industry == 'printing'), isFalse);
      expect(visible.any((m) => m.key == 'customers'), isTrue);
    });
  });

  test('money uses the tenant currency', () {
    setAppCurrency('SAR');
    expect(formatMoney(10), contains('ر.س'));
    setAppCurrency('EGP');
    expect(formatMoney(10), contains('ج.م'));
  });
}
