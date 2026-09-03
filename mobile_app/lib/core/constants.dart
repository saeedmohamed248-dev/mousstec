/// ثوابت التطبيق المشتركة.
class ApiConfig {
  ApiConfig._();

  /// مسار الـ Mobile API على الخادم (يُضاف بعد رابط الورشة).
  static const String apiPath = '/api/mobile/v1';

  /// رابط افتراضي للتجربة — يغيّره المستخدم من شاشة الإعدادات ليطابق نطاق ورشته.
  /// مثال: https://mytec.mousstec.com
  static const String defaultBaseUrl = 'https://demo.mousstec.com';

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
