import 'package:flutter/material.dart';

import 'field_spec.dart';

/// تعريف موديول كامل — يقود القوائم والتفاصيل والنماذج تلقائياً.
class ModuleDef {
  const ModuleDef({
    required this.key,
    required this.title,
    required this.icon,
    required this.group,
    required this.endpoint,
    required this.titleKey,
    this.subtitleKeys = const [],
    this.trailingKey,
    this.trailingMoney = false,
    this.statusKey,
    this.searchable = false,
    this.canCreate = false,
    this.canEdit = false,
    this.canDelete = false,
    this.fields = const [],
    this.color = const Color(0xFF1565C0),
  });

  final String key;
  final String title;
  final IconData icon;
  final String group;
  final String endpoint;

  final String titleKey;
  final List<String> subtitleKeys;
  final String? trailingKey;
  final bool trailingMoney;
  final String? statusKey;

  final bool searchable;
  final bool canCreate;
  final bool canEdit;
  final bool canDelete;
  final List<FieldSpec> fields;
  final Color color;

  bool get isEditable => canCreate || canEdit || canDelete;
}

// خيارات ثابتة مشتركة
const _txTypes = [Choice('in', 'قبض / إيراد'), Choice('out', 'صرف / مصروف')];
const _transmission = [Choice('Auto', 'أوتوماتيك'), Choice('Manual', 'مانيوال')];
const _condition = [Choice('new', 'جديد'), Choice('used', 'استيراد/تقطيع'), Choice('core', 'تالف للتجديد')];
const _leaveTypes = [
  Choice('annual', 'سنوية'), Choice('sick', 'مرضية'), Choice('personal', 'شخصية'),
  Choice('unpaid', 'بدون راتب'), Choice('emergency', 'طارئة'),
];

/// خريطة تسميات عربية لمفاتيح الحقول — تُستخدم في شاشة التفاصيل العامة.
const Map<String, String> fieldLabels = {
  'name': 'الاسم', 'phone': 'الهاتف', 'balance': 'الرصيد', 'tax_id': 'الرقم الضريبي',
  'is_b2b_company': 'حساب شركة', 'loyalty_points': 'نقاط الولاء', 'vip_tier': 'الفئة',
  'part_number': 'رقم القطعة', 'brand': 'الماركة', 'condition': 'الحالة', 'car_model': 'الموديل',
  'car_year': 'سنة الصنع', 'barcode': 'الباركود', 'retail_price': 'سعر البيع',
  'purchase_price': 'سعر الشراء', 'b2b_wholesale_price': 'سعر الجملة', 'min_stock_level': 'حد الأمان',
  'total_quantity': 'المتاح', 'warranty_months': 'الضمان (شهور)', 'is_active': 'نشط',
  'car_plate': 'اللوحة', 'chassis_number': 'الشاسيه', 'model_name': 'الموديل', 'color': 'اللون',
  'transmission': 'الفتيس', 'last_mileage': 'العداد', 'ai_health_score': 'صحة المركبة',
  'customer_name': 'العميل', 'vendor_name': 'المورد', 'branch_name': 'الفرع', 'type': 'النوع',
  'total_amount': 'الإجمالي', 'paid_amount': 'المدفوع', 'due_amount': 'المتبقّي',
  'status_display': 'الحالة', 'date_created': 'التاريخ', 'amount': 'المبلغ',
  'transaction_type': 'النوع', 'type_display': 'النوع', 'description': 'البيان',
  'treasury_name': 'الخزنة', 'category_name': 'الفئة', 'date': 'التاريخ',
  'contract_code': 'كود العقد', 'start_date': 'من', 'end_date': 'إلى', 'total_value': 'القيمة',
  'labor_price': 'سعر المصنعية', 'estimated_hours': 'الساعات', 'tech_commission_percent': 'عمولة الفني %',
  'employee_id': 'كود الموظف', 'full_name': 'الاسم', 'job_title': 'الوظيفة', 'department': 'القسم',
  'contract_type': 'نوع التعاقد', 'hire_date': 'تاريخ التعيين', 'base_salary': 'الراتب الأساسي',
  'daily_rate': 'اليومية', 'leave_type_display': 'نوع الإجازة', 'from_date': 'من', 'to_date': 'إلى',
  'reason': 'السبب', 'installments_count': 'عدد الأقساط', 'remaining_amount': 'المتبقّي',
  'worked_hours': 'ساعات العمل', 'late_minutes': 'دقائق التأخير', 'net_salary': 'صافي الراتب',
  'dtc_code': 'كود العطل', 'severity': 'الخطورة', 'vehicle_plate': 'المركبة', 'detected_at': 'وقت الرصد',
  'scan_type': 'نوع الفحص', 'scanned_at': 'وقت الفحص', 'ai_summary': 'ملخّص الذكاء',
  'from_branch_name': 'من فرع', 'to_branch_name': 'إلى فرع', 'quantity': 'الكمية',
  'quantity_change': 'التغيّر', 'quantity_after': 'الرصيد بعد', 'task_title': 'المهمة',
  'technician_name': 'الفني', 'tech_notes': 'ملاحظات', 'rule_name': 'القاعدة', 'due_at': 'مستحق في',
  'urgency': 'الأولوية', 'rating': 'التقييم', 'comment': 'التعليق', 'location': 'الموقع',
  'system_key': 'المفتاح', 'company_details': 'بيانات الشركة', 'period_month': 'الشهر',
  'period_year': 'السنة', 'total_net': 'صافي الإجمالي', 'total_employees': 'عدد الموظفين',
  'notes': 'ملاحظات', 'mileage': 'العداد',
};

/// كل موديولات التطبيق مُجمّعة بالأقسام.
final List<ModuleDef> moduleRegistry = [
  // ─────────────── 🔧 الورشة ───────────────
  ModuleDef(
    key: 'repair-logs', title: 'سجلات الإصلاح', icon: Icons.handyman, group: 'الورشة',
    endpoint: '/repair-logs/', titleKey: 'task_title',
    subtitleKeys: ['technician_name'], statusKey: 'status', color: Colors.orange,
  ),
  ModuleDef(
    key: 'diagnostic-reports', title: 'تقارير التشخيص', icon: Icons.description, group: 'الورشة',
    endpoint: '/diagnostic-reports/', titleKey: 'scan_type',
    subtitleKeys: ['vin_snapshot', 'scanned_at'], color: Colors.deepOrange,
  ),
  ModuleDef(
    key: 'services', title: 'كتالوج الخدمات', icon: Icons.build, group: 'الورشة',
    endpoint: '/services/', titleKey: 'name', subtitleKeys: ['estimated_hours'],
    trailingKey: 'labor_price', trailingMoney: true, searchable: true,
    canCreate: true, canEdit: true, canDelete: true, color: Colors.teal,
    fields: [
      FieldSpec('name', 'اسم الخدمة', required: true),
      FieldSpec('labor_price', 'سعر المصنعية', type: FieldType.decimal, required: true),
      FieldSpec('estimated_hours', 'الساعات التقديرية', type: FieldType.decimal),
      FieldSpec('tech_commission_percent', 'عمولة الفني %', type: FieldType.decimal),
    ],
  ),

  // ─────────────── 📦 المخزون والمشتريات ───────────────
  ModuleDef(
    key: 'products', title: 'قطع الغيار', icon: Icons.inventory_2, group: 'المخزون والمشتريات',
    endpoint: '/products/', titleKey: 'name', subtitleKeys: ['part_number', 'brand'],
    trailingKey: 'retail_price', trailingMoney: true, searchable: true,
    canCreate: true, canEdit: true, canDelete: true, color: Colors.blueGrey,
    fields: [
      FieldSpec('name', 'اسم القطعة', required: true),
      FieldSpec('part_number', 'رقم القطعة', required: true),
      FieldSpec('brand', 'الماركة'),
      FieldSpec('condition', 'الحالة', type: FieldType.choice, choices: _condition),
      FieldSpec('car_model', 'الموديلات المتوافقة'),
      FieldSpec('car_year', 'سنة الصنع'),
      FieldSpec('barcode', 'الباركود'),
      FieldSpec('retail_price', 'سعر البيع', type: FieldType.decimal),
      FieldSpec('purchase_price', 'سعر الشراء', type: FieldType.decimal),
      FieldSpec('min_stock_level', 'حد الأمان', type: FieldType.integer),
      FieldSpec('warranty_months', 'الضمان (شهور)', type: FieldType.integer),
      FieldSpec('is_active', 'نشط', type: FieldType.boolean),
    ],
  ),
  ModuleDef(
    key: 'stock-alerts', title: 'تنبيهات المخزون', icon: Icons.warning_amber, group: 'المخزون والمشتريات',
    endpoint: '/stock-alerts/', titleKey: 'product_name',
    subtitleKeys: ['branch_name', 'current_quantity'], statusKey: 'alert_type', color: Colors.redAccent,
  ),
  ModuleDef(
    key: 'stock-transfers', title: 'تحويلات المخزون', icon: Icons.swap_horiz, group: 'المخزون والمشتريات',
    endpoint: '/stock-transfers/', titleKey: 'product_name',
    subtitleKeys: ['from_branch_name', 'to_branch_name', 'quantity'], statusKey: 'status',
    canCreate: true, color: Colors.indigo,
    fields: [
      FieldSpec('product', 'القطعة', type: FieldType.fk, fkEndpoint: '/products/', required: true),
      FieldSpec('from_branch', 'من فرع', type: FieldType.fk, fkEndpoint: '/branches/', required: true),
      FieldSpec('to_branch', 'إلى فرع', type: FieldType.fk, fkEndpoint: '/branches/', required: true),
      FieldSpec('quantity', 'الكمية', type: FieldType.integer, required: true),
    ],
  ),
  ModuleDef(
    key: 'inventory-movements', title: 'حركات المخزون', icon: Icons.compare_arrows, group: 'المخزون والمشتريات',
    endpoint: '/inventory-movements/', titleKey: 'product_name',
    subtitleKeys: ['reason', 'quantity_change'], trailingKey: 'quantity_after', color: Colors.blueGrey,
  ),
  ModuleDef(
    key: 'vendors', title: 'الموردون', icon: Icons.local_shipping, group: 'المخزون والمشتريات',
    endpoint: '/vendors/', titleKey: 'name', subtitleKeys: ['phone'],
    trailingKey: 'balance', trailingMoney: true, searchable: true,
    canCreate: true, canEdit: true, canDelete: true, color: Colors.brown,
    fields: [
      FieldSpec('name', 'اسم المورد', required: true),
      FieldSpec('phone', 'الهاتف'),
      FieldSpec('tax_id', 'الرقم الضريبي'),
      FieldSpec('company_details', 'بيانات الشركة', type: FieldType.multiline),
    ],
  ),
  ModuleDef(
    key: 'purchase-invoices', title: 'فواتير الشراء', icon: Icons.receipt_long, group: 'المخزون والمشتريات',
    endpoint: '/purchase-invoices/', titleKey: 'vendor_name',
    subtitleKeys: ['status_display', 'date_created'], trailingKey: 'total_amount', trailingMoney: true,
    searchable: true, color: Colors.deepPurple,
  ),
  ModuleDef(
    key: 'scrap-jobs', title: 'عمليات التفكيك', icon: Icons.car_crash, group: 'المخزون والمشتريات',
    endpoint: '/scrap-jobs/', titleKey: 'job_ref',
    subtitleKeys: ['car_model', 'chassis_number'], trailingKey: 'total_purchase_cost', trailingMoney: true,
    color: Colors.blueGrey,
  ),

  // ─────────────── 👥 العملاء والمركبات ───────────────
  ModuleDef(
    key: 'customers', title: 'العملاء', icon: Icons.people, group: 'العملاء والمركبات',
    endpoint: '/customers/', titleKey: 'name', subtitleKeys: ['phone', 'vip_tier'],
    trailingKey: 'balance', trailingMoney: true, searchable: true,
    canCreate: true, canEdit: true, canDelete: true, color: Colors.indigo,
    fields: [
      FieldSpec('name', 'اسم العميل', required: true),
      FieldSpec('phone', 'رقم الهاتف', required: true),
      FieldSpec('is_b2b_company', 'حساب شركة (B2B)', type: FieldType.boolean),
      FieldSpec('tax_id', 'الرقم الضريبي'),
    ],
  ),
  ModuleDef(
    key: 'vehicles', title: 'المركبات', icon: Icons.directions_car, group: 'العملاء والمركبات',
    endpoint: '/vehicles/', titleKey: 'car_plate', subtitleKeys: ['brand', 'model_name', 'customer_name'],
    searchable: true, canCreate: true, canEdit: true, canDelete: true, color: Colors.blue,
    fields: [
      FieldSpec('customer', 'المالك', type: FieldType.fk, fkEndpoint: '/customers/', required: true),
      FieldSpec('chassis_number', 'رقم الشاسيه (VIN)', required: true),
      FieldSpec('car_plate', 'رقم اللوحة'),
      FieldSpec('brand', 'الماركة'),
      FieldSpec('model_name', 'الموديل'),
      FieldSpec('color', 'اللون'),
      FieldSpec('transmission', 'الفتيس', type: FieldType.choice, choices: _transmission),
      FieldSpec('last_mileage', 'قراءة العداد', type: FieldType.integer),
    ],
  ),
  ModuleDef(
    key: 'maintenance-contracts', title: 'عقود الصيانة', icon: Icons.assignment, group: 'العملاء والمركبات',
    endpoint: '/maintenance-contracts/', titleKey: 'contract_code',
    subtitleKeys: ['customer_name', 'end_date'], trailingKey: 'total_value', trailingMoney: true,
    canCreate: true, canEdit: true, canDelete: true, color: Colors.cyan,
    fields: [
      FieldSpec('customer', 'العميل', type: FieldType.fk, fkEndpoint: '/customers/', required: true),
      FieldSpec('contract_code', 'كود العقد', required: true),
      FieldSpec('start_date', 'تاريخ البداية', type: FieldType.date),
      FieldSpec('end_date', 'تاريخ النهاية', type: FieldType.date),
      FieldSpec('total_value', 'قيمة العقد', type: FieldType.decimal),
      FieldSpec('is_active', 'نشط', type: FieldType.boolean),
    ],
  ),
  ModuleDef(
    key: 'service-nudges', title: 'تذكيرات الصيانة', icon: Icons.notifications_active, group: 'العملاء والمركبات',
    endpoint: '/service-nudges/', titleKey: 'rule_name',
    subtitleKeys: ['vehicle_plate', 'due_at'], statusKey: 'urgency', color: Colors.amber,
  ),
  ModuleDef(
    key: 'customer-feedback', title: 'تقييمات العملاء', icon: Icons.star_rate, group: 'العملاء والمركبات',
    endpoint: '/customer-feedback/', titleKey: 'rating', subtitleKeys: ['comment'], color: Colors.amber,
  ),

  // ─────────────── 💰 الحسابات ───────────────
  ModuleDef(
    key: 'treasuries', title: 'الخزائن', icon: Icons.account_balance_wallet, group: 'الحسابات',
    endpoint: '/treasuries/', titleKey: 'name', subtitleKeys: ['branch_name', 'type'],
    trailingKey: 'balance', trailingMoney: true,
    canCreate: true, canEdit: true, canDelete: true, color: Colors.green,
    fields: [
      FieldSpec('name', 'اسم الخزنة', required: true),
      FieldSpec('branch', 'الفرع', type: FieldType.fk, fkEndpoint: '/branches/', required: true),
      FieldSpec('type', 'النوع'),
      FieldSpec('balance', 'الرصيد الافتتاحي', type: FieldType.decimal),
      FieldSpec('is_active', 'نشط', type: FieldType.boolean),
    ],
  ),
  ModuleDef(
    key: 'transactions', title: 'الحركات المالية', icon: Icons.receipt, group: 'الحسابات',
    endpoint: '/transactions/', titleKey: 'description',
    subtitleKeys: ['type_display', 'treasury_name', 'date'], trailingKey: 'amount', trailingMoney: true,
    canCreate: true, color: Colors.teal,
    fields: [
      FieldSpec('treasury', 'الخزنة', type: FieldType.fk, fkEndpoint: '/treasuries/', required: true),
      FieldSpec('transaction_type', 'النوع', type: FieldType.choice, choices: _txTypes, required: true),
      FieldSpec('amount', 'المبلغ', type: FieldType.decimal, required: true),
      FieldSpec('category', 'الفئة', type: FieldType.fk, fkEndpoint: '/expense-categories/'),
      FieldSpec('description', 'البيان', type: FieldType.multiline),
    ],
  ),
  ModuleDef(
    key: 'expense-categories', title: 'فئات المصروفات', icon: Icons.category, group: 'الحسابات',
    endpoint: '/expense-categories/', titleKey: 'name',
    canCreate: true, canEdit: true, canDelete: true, color: Colors.blueGrey,
    fields: [FieldSpec('name', 'اسم الفئة', required: true)],
  ),
  ModuleDef(
    key: 'branches', title: 'الفروع', icon: Icons.store, group: 'الحسابات',
    endpoint: '/branches/', titleKey: 'name', subtitleKeys: ['location', 'phone'],
    canCreate: true, canEdit: true, canDelete: true, color: Colors.indigo,
    fields: [
      FieldSpec('name', 'اسم الفرع', required: true),
      FieldSpec('location', 'الموقع'),
      FieldSpec('phone', 'الهاتف'),
    ],
  ),

  // ─────────────── 👷 الموارد البشرية ───────────────
  ModuleDef(
    key: 'employees', title: 'الموظفون', icon: Icons.badge, group: 'الموارد البشرية',
    endpoint: '/employees/', titleKey: 'full_name', subtitleKeys: ['job_title', 'department'],
    searchable: true, color: Colors.indigo,
  ),
  ModuleDef(
    key: 'attendance', title: 'الحضور والانصراف', icon: Icons.fingerprint, group: 'الموارد البشرية',
    endpoint: '/attendance/', titleKey: 'employee_name',
    subtitleKeys: ['date', 'status_display'], trailingKey: 'worked_hours', color: Colors.teal,
  ),
  ModuleDef(
    key: 'leave-requests', title: 'طلبات الإجازة', icon: Icons.event_busy, group: 'الموارد البشرية',
    endpoint: '/leave-requests/', titleKey: 'employee_name',
    subtitleKeys: ['leave_type_display', 'from_date', 'to_date'], statusKey: 'status',
    canCreate: true, color: Colors.orange,
    fields: [
      FieldSpec('employee', 'الموظف', type: FieldType.fk, fkEndpoint: '/employees/', fkLabelKey: 'full_name', required: true),
      FieldSpec('leave_type', 'نوع الإجازة', type: FieldType.choice, choices: _leaveTypes, required: true),
      FieldSpec('from_date', 'من تاريخ', type: FieldType.date, required: true),
      FieldSpec('to_date', 'إلى تاريخ', type: FieldType.date, required: true),
      FieldSpec('reason', 'السبب', type: FieldType.multiline),
    ],
  ),
  ModuleDef(
    key: 'advances', title: 'السلف', icon: Icons.savings, group: 'الموارد البشرية',
    endpoint: '/advances/', titleKey: 'employee_name', subtitleKeys: ['reason'],
    trailingKey: 'amount', trailingMoney: true, statusKey: 'status',
    canCreate: true, color: Colors.deepPurple,
    fields: [
      FieldSpec('employee', 'الموظف', type: FieldType.fk, fkEndpoint: '/employees/', fkLabelKey: 'full_name', required: true),
      FieldSpec('amount', 'المبلغ', type: FieldType.decimal, required: true),
      FieldSpec('installments_count', 'عدد الأقساط', type: FieldType.integer),
      FieldSpec('reason', 'السبب', type: FieldType.multiline),
    ],
  ),
  ModuleDef(
    key: 'payroll-runs', title: 'مسيّرات الرواتب', icon: Icons.payments, group: 'الموارد البشرية',
    endpoint: '/payroll-runs/', titleKey: 'period_month',
    subtitleKeys: ['period_year', 'status_display'], trailingKey: 'total_net', trailingMoney: true,
    color: Colors.green,
  ),

  // ─────────────── 🚗 التشخيص الذكي ───────────────
  ModuleDef(
    key: 'diag-scans', title: 'الفحوصات', icon: Icons.wifi_tethering, group: 'التشخيص الذكي',
    endpoint: '/diag-scans/', titleKey: 'vehicle_plate',
    subtitleKeys: ['status_display', 'started_at'], color: Colors.purple,
  ),
  ModuleDef(
    key: 'fault-logs', title: 'سجل الأعطال', icon: Icons.error, group: 'التشخيص الذكي',
    endpoint: '/fault-logs/', titleKey: 'dtc_code',
    subtitleKeys: ['vehicle_plate', 'detected_at'], statusKey: 'severity', color: Colors.red,
  ),
  ModuleDef(
    key: 'diag-devices', title: 'أجهزة الفحص', icon: Icons.memory, group: 'التشخيص الذكي',
    endpoint: '/diag-devices/', titleKey: 'hardware_id',
    subtitleKeys: ['vehicle_plate'], color: Colors.purple,
  ),
];

/// أقسام مرتّبة كما تظهر في شاشة «المزيد».
const List<String> moduleGroups = [
  'الورشة',
  'المخزون والمشتريات',
  'العملاء والمركبات',
  'الحسابات',
  'الموارد البشرية',
  'التشخيص الذكي',
];

List<ModuleDef> modulesInGroup(String group) =>
    moduleRegistry.where((m) => m.group == group).toList();
