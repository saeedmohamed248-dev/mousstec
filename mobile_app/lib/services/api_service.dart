import '../core/api_client.dart';
import '../models/models.dart';

/// طبقة الخدمات — تحوّل استدعاءات الـ API الخام إلى نماذج مكتوبة.
class ApiService {
  ApiService(this._api);
  final ApiClient _api;

  // ── المصادقة ─────────────────────────────────────────────────────
  Future<Map<String, dynamic>> login(String username, String password) async {
    final data =
        await _api.post('/auth/login/', body: {'username': username, 'password': password}, auth: false);
    return data as Map<String, dynamic>;
  }

  Future<AppUser> me() async {
    final data = await _api.get('/auth/me/');
    return AppUser.fromJson(data as Map<String, dynamic>);
  }

  // ── 🔔 أجهزة الإشعارات ─────────────────────────────────────────
  Future<void> registerDevice(
      {required String token, required String platform, String language = 'ar'}) async {
    await _api.post('/devices/', body: {'token': token, 'platform': platform, 'language': language});
  }

  Future<void> unregisterDevice(String token) async {
    await _api.delete('/devices/?token=${Uri.encodeQueryComponent(token)}');
  }

  // ── لوحة المعلومات ───────────────────────────────────────────────
  Future<DashboardSummary> dashboard() async {
    final data = await _api.get('/dashboard/');
    return DashboardSummary.fromJson(data as Map<String, dynamic>);
  }

  Future<Map<String, dynamic>> analytics() async {
    final data = await _api.get('/analytics/');
    return data as Map<String, dynamic>;
  }

  // ── أوامر الشغل ──────────────────────────────────────────────────
  Future<Paginated<WorkOrder>> workOrders({String? status, String? search}) async {
    final data = await _api.get('/work-orders/', query: {
      if (status != null) 'status': status,
      if (search != null && search.isNotEmpty) 'search': search,
    });
    return Paginated.fromJson(data as Map<String, dynamic>, WorkOrder.fromJson);
  }

  Future<WorkOrder> workOrder(int id) async {
    final data = await _api.get('/work-orders/$id/');
    return WorkOrder.fromJson(data as Map<String, dynamic>);
  }

  Future<WorkOrder> updateWorkOrderStatus(int id, String status) async {
    final data = await _api.post('/work-orders/$id/status/', body: {'status': status});
    return WorkOrder.fromJson(data as Map<String, dynamic>);
  }

  // ── المخزون ──────────────────────────────────────────────────────
  Future<Paginated<Product>> products({String? search}) async {
    final data = await _api.get('/products/', query: {
      if (search != null && search.isNotEmpty) 'search': search,
    });
    return Paginated.fromJson(data as Map<String, dynamic>, Product.fromJson);
  }

  Future<Paginated<Product>> lowStock() async {
    final data = await _api.get('/products/low-stock/');
    return Paginated.fromAny(data, Product.fromJson);
  }

  Future<Product> product(int id) async {
    final data = await _api.get('/products/$id/');
    return Product.fromJson(data as Map<String, dynamic>);
  }

  // ── العملاء ──────────────────────────────────────────────────────
  Future<Paginated<Customer>> customers({String? search}) async {
    final data = await _api.get('/customers/', query: {
      if (search != null && search.isNotEmpty) 'search': search,
    });
    return Paginated.fromJson(data as Map<String, dynamic>, Customer.fromJson);
  }

  Future<Customer> customer(int id) async {
    final data = await _api.get('/customers/$id/');
    return Customer.fromJson(data as Map<String, dynamic>);
  }

  // ── 🎨 المطابع ──────────────────────────────────────────────────
  Future<Map<String, dynamic>> printDashboard() async =>
      await _api.get('/print/dashboard/') as Map<String, dynamic>;

  Future<Map<String, dynamic>> printOrder(int id) async =>
      await _api.get('/print/orders/$id/') as Map<String, dynamic>;

  Future<Map<String, dynamic>> createPrintOrder(Map<String, dynamic> body) async =>
      await _api.post('/print/orders/', body: body) as Map<String, dynamic>;

  /// confirm / deliver / cancel
  Future<Map<String, dynamic>> printOrderAction(int id, String action) async =>
      await _api.post('/print/orders/$id/action/', body: {'action': action}) as Map<String, dynamic>;

  Future<Map<String, dynamic>> advancePrintJob(int id) async =>
      await _api.post('/print/jobs/$id/advance/') as Map<String, dynamic>;

  Future<List<Map<String, dynamic>>> printJobProofs(int jobId) async {
    final data = await _api.get('/print/jobs/$jobId/proofs/');
    return (data as List).map((e) => e as Map<String, dynamic>).toList();
  }

  Future<List<Map<String, dynamic>>> printTreasuries() async {
    final data = await _api.get('/print/treasuries/');
    return (data as List).map((e) => e as Map<String, dynamic>).toList();
  }

  // ── CRUD عام (لمحرك الموديولات) ───────────────────────────────────
  /// يُرجع صفحة DRF خام: {count, next, results:[...]} أو قائمة مباشرة.
  Future<Map<String, dynamic>> rawList(String endpoint, {Map<String, dynamic>? query}) async {
    final data = await _api.get(endpoint, query: query);
    if (data is List) {
      return {'count': data.length, 'next': null, 'results': data};
    }
    return data as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> rawGet(String endpoint, int id) async {
    final data = await _api.get('$endpoint$id/');
    return data as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> rawCreate(String endpoint, Map<String, dynamic> body) async {
    final data = await _api.post(endpoint, body: body);
    return data as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> rawUpdate(String endpoint, int id, Map<String, dynamic> body) async {
    final data = await _api.patch('$endpoint$id/', body: body);
    return data as Map<String, dynamic>;
  }

  Future<void> rawDelete(String endpoint, int id) async {
    await _api.delete('$endpoint$id/');
  }

  /// تنفيذ إجراء مخصّص على سجل، مثل الموافقة/الرفض.
  Future<Map<String, dynamic>> rawAction(
    String endpoint,
    int id,
    String slug, {
    Map<String, dynamic>? body,
  }) async {
    final data = await _api.post('$endpoint$id/$slug/', body: body ?? {});
    return data as Map<String, dynamic>;
  }

  /// تحميل خيارات علاقة (fk) — نجلب أول صفحة فقط لعرضها في القائمة المنسدلة.
  Future<List<Map<String, dynamic>>> fkOptions(String endpoint) async {
    final page = await rawList(endpoint, query: {'page_size': 100});
    final results = page['results'];
    return (results is List ? results : const []).map((e) => e as Map<String, dynamic>).toList();
  }
}
