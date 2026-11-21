/// 🎨 ثوابت المطابع — نفس حالات/مراحل السيرفر (printing.models).

/// حالات طلب الطباعة (PrintOrder.STATUS_CHOICES).
const Map<String, String> printOrderStatuses = {
  'draft': 'مسودة',
  'confirmed': 'مؤكد',
  'in_progress': 'قيد التنفيذ',
  'ready': 'جاهز للتسليم',
  'delivered': 'تم التسليم',
  'cancelled': 'ملغي',
};

/// مراحل الإنتاج (PrintJob.STAGE_CHOICES).
const Map<String, String> productionStages = {
  'design': 'تصميم',
  'proof': 'بروفة',
  'printing': 'طباعة',
  'finishing': 'تشطيب',
  'done': 'خلصت',
};

/// إجراءات الطلب (/print/orders/{id}/action/).
const Map<String, String> printOrderActions = {
  'confirm': 'تأكيد الطلب',
  'deliver': 'اتسلّم',
  'cancel': 'إلغاء الطلب',
};
