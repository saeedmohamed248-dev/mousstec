/// ثوابت التطبيق المشتركة.
class ApiConfig {
  ApiConfig._();

  /// مسار الـ Mobile API على الخادم (يُضاف بعد رابط الورشة).
  static const String apiPath = '/api/mobile/v1';

  /// رابط افتراضي للتجربة — يغيّره المستخدم من شاشة الإعدادات ليطابق نطاق ورشته.
  /// مثال: https://mytec.mousstec.com
  static const String defaultBaseUrl = 'https://demo.mousstec.com';

  /// النطاق الرئيسي — «كود الشركة» myshop ⇒ https://myshop.mousstec.com
  static const String rootDomain = 'mousstec.com';

  /// 🐛 [FIX]: المستخدم كان لازم يكتب رابط كامل بـ https:// في «إعدادات الخادم» المخفية
  /// وإلا الدخول يقع. دلوقتي يكتب كود شركته بس (myshop) أو أي رابط/دومين خاص.
  /// بيرجّع null لو المدخل مش صالح.
  static String? workspaceUrl(String input) {
    var v = input.trim().toLowerCase();
    if (v.isEmpty) return null;
    v = v.replaceAll(RegExp(r'/+$'), '');
    if (!v.contains('://')) {
      if (RegExp(r'^[a-z0-9][a-z0-9-]{0,62}$').hasMatch(v)) {
        v = 'https://$v.$rootDomain';
      } else {
        v = 'https://$v';
      }
    }
    final uri = Uri.tryParse(v);
    if (uri == null ||
        !(uri.scheme == 'https' || uri.scheme == 'http') ||
        uri.host.isEmpty ||
        !uri.host.contains('.') && uri.host != 'localhost') {
      return null;
    }
    return '${uri.scheme}://${uri.host}${uri.hasPort ? ':${uri.port}' : ''}';
  }

  /// العكس للعرض: https://myshop.mousstec.com ⇒ myshop
  static String workspaceCode(String url) {
    final host = Uri.tryParse(url)?.host ?? url;
    final suffix = '.$rootDomain';
    return host.endsWith(suffix) ? host.substring(0, host.length - suffix.length) : host;
  }

  // مفاتيح التخزين المحلي
  static const String kBaseUrl = 'base_url';
  static const String kAccessToken = 'access_token';
  static const String kRefreshToken = 'refresh_token';
}

/// حالات أمر الشغل كما يعرّفها الخادم (SaleInvoice.STATUS_CHOICES).
class WorkOrderStatus {
  WorkOrderStatus._();

  static const String quotation = 'quotation';
  static const String inProgress = 'in_progress';
  static const String qualityCheck = 'quality_check';
  static const String ready = 'ready';
  static const String posted = 'posted';

  /// الترتيب المنطقي لسير العمل داخل الورشة.
  static const List<String> flow = [
    quotation,
    inProgress,
    qualityCheck,
    ready,
    posted,
  ];

  static const Map<String, String> labels = {
    quotation: 'عرض سعر',
    inProgress: 'قيد العمل',
    qualityCheck: 'فحص الجودة',
    ready: 'جاهز للتسليم',
    posted: 'تم التسليم',
  };

  static String label(String status) => labels[status] ?? status;
}
