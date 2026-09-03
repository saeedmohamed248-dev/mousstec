import 'package:flutter/foundation.dart';

import '../core/api_client.dart';
import '../core/auth_store.dart';
import '../models/models.dart';
import '../services/api_service.dart';

enum AuthStatus { unknown, authenticated, unauthenticated }

/// حالة المصادقة على مستوى التطبيق.
class AuthProvider extends ChangeNotifier {
  AuthProvider({AuthStore? store, ApiClient? api})
      : store = store ?? AuthStore() {
    _api = api ?? ApiClient(store: this.store);
    service = ApiService(_api);
  }

  final AuthStore store;
  late final ApiClient _api;
  late final ApiService service;

  AuthStatus status = AuthStatus.unknown;
  AppUser? user;
  String? error;
  bool busy = false;

  ApiService get apiService => service;

  /// يُستدعى عند إقلاع التطبيق: يحاول استعادة الجلسة من التوكن المخزّن.
  Future<void> bootstrap() async {
    final token = await store.accessToken;
    if (token == null) {
      status = AuthStatus.unauthenticated;
      notifyListeners();
      return;
    }
    try {
      user = await service.me();
      status = AuthStatus.authenticated;
    } catch (_) {
      // التوكن غير صالح أو الخادم غير متاح — نطلب تسجيل دخول جديد.
      await store.clear();
      status = AuthStatus.unauthenticated;
    }
    notifyListeners();
  }

  Future<bool> login(String username, String password) async {
    busy = true;
    error = null;
    notifyListeners();
    try {
      final data = await service.login(username, password);
      await store.saveTokens(
        access: data['access'] as String,
        refresh: data['refresh'] as String,
      );
      user = data['user'] != null
          ? AppUser.fromJson(data['user'] as Map<String, dynamic>)
          : await service.me();
      status = AuthStatus.authenticated;
      busy = false;
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      error = e.message;
      busy = false;
      notifyListeners();
      return false;
    } catch (_) {
      error = 'حدث خطأ غير متوقع. حاول مرة أخرى.';
      busy = false;
      notifyListeners();
      return false;
    }
  }

  Future<void> logout() async {
    await store.clear();
    user = null;
    status = AuthStatus.unauthenticated;
    notifyListeners();
  }

  Future<String> get baseUrl => store.getBaseUrl();

  Future<void> setBaseUrl(String url) async {
    await store.setBaseUrl(url);
    notifyListeners();
  }
}
