import 'package:flutter/foundation.dart';

import '../core/api_client.dart';
import '../core/auth_store.dart';
import '../core/constants.dart';
import '../core/i18n.dart';
import '../models/models.dart';
import '../services/api_service.dart';
import '../services/push_service.dart';
import '../widgets/common.dart' show setAppCurrency;

enum AuthStatus { unknown, authenticated, unauthenticated }

/// حالة المصادقة على مستوى التطبيق.
class AuthProvider extends ChangeNotifier {
  AuthProvider({AuthStore? store, ApiClient? api}) : store = store ?? AuthStore() {
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

  /// القطاع والعملة وصلاحيات المطابع — من `tenant` في رد الدخول/me.
  TenantInfo get tenant => user?.tenant ?? TenantInfo.automotive;
  bool get isPrinting => tenant.isPrinting;

  void _onUser(AppUser u) {
    user = u;
    setAppCurrency(u.tenant?.currency ?? 'EGP');
    // 🔔 بعد الدخول: سجّل الجهاز للإشعارات (من غير ما نستنى)
    PushService.instance.registerDevice(service);
  }

  /// يُستدعى عند إقلاع التطبيق: يحاول استعادة الجلسة من التوكن المخزّن.
  Future<void> bootstrap() async {
    final token = await store.accessToken;
    if (token == null) {
      status = AuthStatus.unauthenticated;
      notifyListeners();
      return;
    }
    try {
      _onUser(await service.me());
      status = AuthStatus.authenticated;
    } on ApiException catch (e) {
      // 🐛 [FIX]: أي خطأ (حتى انقطاع النت لحظة فتح التطبيق) كان بيمسح التوكن ويطلّع
      //    المستخدم برا. نخرج بس لو السيرفر قال صراحةً إن الجلسة مش صالحة.
      if (e.isUnauthorized) await store.clear();
      error = e.isUnauthorized ? null : e.message;
      status = AuthStatus.unauthenticated;
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
      if (data['user'] != null) {
        final u = Map<String, dynamic>.from(data['user'] as Map);
        if (data['tenant'] != null) u['tenant'] = data['tenant'];
        _onUser(AppUser.fromJson(u));
      } else {
        _onUser(await service.me());
      }
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
      error = tr('حدث خطأ غير متوقع. حاول مرة أخرى.');
      busy = false;
      notifyListeners();
      return false;
    }
  }

  Future<void> logout() async {
    await PushService.instance.unregister(service);
    await store.clear();
    user = null;
    status = AuthStatus.unauthenticated;
    notifyListeners();
  }

  Future<String> get baseUrl => store.getBaseUrl();

  /// كود الشركة أو رابطها ⇒ رابط كامل. بيرجّع false لو المدخل مش صالح.
  Future<bool> setWorkspace(String input) async {
    final url = ApiConfig.workspaceUrl(input);
    if (url == null) return false;
    await store.setBaseUrl(url);
    notifyListeners();
    return true;
  }

  Future<void> setBaseUrl(String url) async {
    await store.setBaseUrl(url);
    notifyListeners();
  }
}
