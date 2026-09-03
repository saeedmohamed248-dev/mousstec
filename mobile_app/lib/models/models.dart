/// نماذج البيانات — تحويل من/إلى JSON القادم من Mobile API.
///
/// كلها ثابتة (immutable) لتسهيل الاختبار وتجنّب التعديل العرضي.

double _toDouble(dynamic v) {
  if (v == null) return 0;
  if (v is num) return v.toDouble();
  return double.tryParse('$v') ?? 0;
}

int _toInt(dynamic v) {
  if (v == null) return 0;
  if (v is num) return v.toInt();
  return int.tryParse('$v') ?? 0;
}

class AppUser {
  const AppUser({
    required this.id,
    required this.username,
    required this.fullName,
    this.email,
    this.isStaff = false,
    this.isSuperuser = false,
  });

  final int id;
  final String username;
  final String fullName;
  final String? email;
  final bool isStaff;
  final bool isSuperuser;

  factory AppUser.fromJson(Map<String, dynamic> j) => AppUser(
        id: _toInt(j['id']),
        username: j['username'] as String? ?? '',
        fullName: j['full_name'] as String? ?? j['username'] as String? ?? '',
        email: j['email'] as String?,
        isStaff: j['is_staff'] as bool? ?? false,
        isSuperuser: j['is_superuser'] as bool? ?? false,
      );
}

class DashboardSummary {
  const DashboardSummary({
    required this.openWorkOrders,
    required this.readyForDelivery,
    required this.inProgress,
    required this.qualityCheck,
    required this.quotations,
    required this.revenueToday,
    required this.lowStockAlerts,
    required this.totalCustomers,
    required this.totalProducts,
  });

  final int openWorkOrders;
  final int readyForDelivery;
  final int inProgress;
  final int qualityCheck;
  final int quotations;
  final double revenueToday;
  final int lowStockAlerts;
  final int totalCustomers;
  final int totalProducts;

  factory DashboardSummary.fromJson(Map<String, dynamic> j) => DashboardSummary(
        openWorkOrders: _toInt(j['open_work_orders']),
        readyForDelivery: _toInt(j['ready_for_delivery']),
        inProgress: _toInt(j['in_progress']),
        qualityCheck: _toInt(j['quality_check']),
        quotations: _toInt(j['quotations']),
        revenueToday: _toDouble(j['revenue_today']),
        lowStockAlerts: _toInt(j['low_stock_alerts']),
        totalCustomers: _toInt(j['total_customers']),
        totalProducts: _toInt(j['total_products']),
      );
}

class WorkOrder {
  const WorkOrder({
    required this.id,
    required this.status,
    required this.statusDisplay,
    required this.customerName,
    required this.customerPhone,
    this.vehiclePlate,
    this.branchName,
    required this.totalAmount,
    required this.paidAmount,
    required this.dueAmount,
    this.dateCreated,
    this.mileage,
    this.notes,
    this.items = const [],
    this.services = const [],
  });

  final int id;
  final String status;
  final String statusDisplay;
  final String customerName;
  final String customerPhone;
  final String? vehiclePlate;
  final String? branchName;
  final double totalAmount;
  final double paidAmount;
  final double dueAmount;
  final String? dateCreated;
  final int? mileage;
  final String? notes;
  final List<WorkOrderItem> items;
  final List<WorkOrderService> services;

  factory WorkOrder.fromJson(Map<String, dynamic> j) => WorkOrder(
        id: _toInt(j['id']),
        status: j['status'] as String? ?? '',
        statusDisplay: j['status_display'] as String? ?? '',
        customerName: j['customer_name'] as String? ?? '',
        customerPhone: j['customer_phone'] as String? ?? '',
        vehiclePlate: j['vehicle_plate'] as String?,
        branchName: j['branch_name'] as String?,
        totalAmount: _toDouble(j['total_amount']),
        paidAmount: _toDouble(j['paid_amount']),
        dueAmount: _toDouble(j['due_amount']),
        dateCreated: j['date_created'] as String?,
        mileage: j['mileage'] == null ? null : _toInt(j['mileage']),
        notes: j['notes'] as String?,
        items: (j['items'] as List? ?? [])
            .map((e) => WorkOrderItem.fromJson(e as Map<String, dynamic>))
            .toList(),
        services: (j['service_items'] as List? ?? [])
            .map((e) => WorkOrderService.fromJson(e as Map<String, dynamic>))
            .toList(),
      );
}

class WorkOrderItem {
  const WorkOrderItem({
    required this.productName,
    required this.partNumber,
    required this.quantity,
    required this.unitPrice,
    required this.totalPrice,
  });

  final String productName;
  final String partNumber;
  final int quantity;
  final double unitPrice;
  final double totalPrice;

  factory WorkOrderItem.fromJson(Map<String, dynamic> j) => WorkOrderItem(
        productName: j['product_name'] as String? ?? '',
        partNumber: j['part_number'] as String? ?? '',
        quantity: _toInt(j['quantity']),
        unitPrice: _toDouble(j['unit_price']),
        totalPrice: _toDouble(j['total_price']),
      );
}

class WorkOrderService {
  const WorkOrderService({
    required this.serviceName,
    required this.price,
    required this.hours,
  });

  final String serviceName;
  final double price;
  final double hours;

  factory WorkOrderService.fromJson(Map<String, dynamic> j) => WorkOrderService(
        serviceName: j['service_name'] as String? ?? '',
        price: _toDouble(j['price']),
        hours: _toDouble(j['actual_hours']),
      );
}

class Product {
  const Product({
    required this.id,
    required this.name,
    required this.partNumber,
    required this.brand,
    this.condition,
    this.carModel,
    required this.retailPrice,
    required this.totalQuantity,
    required this.minStockLevel,
    required this.isLowStock,
    this.stockByBranch = const [],
    this.warrantyMonths,
    this.barcode,
  });

  final int id;
  final String name;
  final String partNumber;
  final String brand;
  final String? condition;
  final String? carModel;
  final double retailPrice;
  final int totalQuantity;
  final int minStockLevel;
  final bool isLowStock;
  final List<StockLocation> stockByBranch;
  final int? warrantyMonths;
  final String? barcode;

  factory Product.fromJson(Map<String, dynamic> j) => Product(
        id: _toInt(j['id']),
        name: j['name'] as String? ?? '',
        partNumber: j['part_number'] as String? ?? '',
        brand: j['brand'] as String? ?? '',
        condition: j['condition'] as String?,
        carModel: j['car_model'] as String?,
        retailPrice: _toDouble(j['retail_price']),
        totalQuantity: _toInt(j['total_quantity']),
        minStockLevel: _toInt(j['min_stock_level']),
        isLowStock: j['is_low_stock'] as bool? ?? false,
        warrantyMonths: j['warranty_months'] == null ? null : _toInt(j['warranty_months']),
        barcode: j['barcode'] as String?,
        stockByBranch: (j['stock_by_branch'] as List? ?? [])
            .map((e) => StockLocation.fromJson(e as Map<String, dynamic>))
            .toList(),
      );
}

class StockLocation {
  const StockLocation({required this.branchName, required this.quantity, this.shelf});
  final String branchName;
  final int quantity;
  final String? shelf;

  factory StockLocation.fromJson(Map<String, dynamic> j) => StockLocation(
        branchName: j['branch_name'] as String? ?? '',
        quantity: _toInt(j['quantity']),
        shelf: j['shelf_location'] as String?,
      );
}

class Customer {
  const Customer({
    required this.id,
    required this.name,
    required this.phone,
    required this.isB2b,
    required this.balance,
    required this.loyaltyPoints,
    this.vipTier,
    this.vehicles = const [],
  });

  final int id;
  final String name;
  final String phone;
  final bool isB2b;
  final double balance;
  final int loyaltyPoints;
  final String? vipTier;
  final List<Vehicle> vehicles;

  factory Customer.fromJson(Map<String, dynamic> j) => Customer(
        id: _toInt(j['id']),
        name: j['name'] as String? ?? '',
        phone: j['phone'] as String? ?? '',
        isB2b: j['is_b2b_company'] as bool? ?? false,
        balance: _toDouble(j['balance']),
        loyaltyPoints: _toInt(j['loyalty_points']),
        vipTier: j['vip_tier'] as String?,
        vehicles: (j['vehicles'] as List? ?? [])
            .map((e) => Vehicle.fromJson(e as Map<String, dynamic>))
            .toList(),
      );
}

class Vehicle {
  const Vehicle({
    required this.plate,
    required this.brand,
    this.modelName,
    this.chassis,
    this.mileage,
    this.healthScore,
  });

  final String? plate;
  final String brand;
  final String? modelName;
  final String? chassis;
  final int? mileage;
  final int? healthScore;

  factory Vehicle.fromJson(Map<String, dynamic> j) => Vehicle(
        plate: j['car_plate'] as String?,
        brand: j['brand'] as String? ?? '',
        modelName: j['model_name'] as String?,
        chassis: j['chassis_number'] as String?,
        mileage: j['last_mileage'] == null ? null : _toInt(j['last_mileage']),
        healthScore: j['ai_health_score'] == null ? null : _toInt(j['ai_health_score']),
      );
}

/// نتيجة مُقسّمة على صفحات من DRF (count/next/previous/results).
class Paginated<T> {
  const Paginated({required this.count, required this.results, this.hasNext = false});
  final int count;
  final List<T> results;
  final bool hasNext;

  factory Paginated.fromJson(Map<String, dynamic> j, T Function(Map<String, dynamic>) parse) {
    final raw = j['results'];
    // بعض المسارات قد تُعيد قائمة مباشرة دون ترقيم.
    final list = raw is List ? raw : (j is List ? j : const []);
    return Paginated(
      count: _toInt(j['count'] ?? list.length),
      hasNext: j['next'] != null,
      results: list.map((e) => parse(e as Map<String, dynamic>)).toList(),
    );
  }
}
