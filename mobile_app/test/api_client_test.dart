import 'dart:convert';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mousstec_mobile/core/api_client.dart';
import 'package:mousstec_mobile/core/auth_store.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// يهيّئ قنوات المنصّة (secure storage) في الذاكرة حتى تعمل الاختبارات بدون جهاز.
void _mockSecureStorage() {
  final store = <String, String?>{};
  const channel = MethodChannel('plugins.it_nomads.com/flutter_secure_storage');
  TestWidgetsFlutterBinding.ensureInitialized();
  TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
      .setMockMethodCallHandler(channel, (call) async {
    final args = (call.arguments as Map?) ?? {};
    final key = args['key'] as String?;
    switch (call.method) {
      case 'write':
        store[key!] = args['value'] as String?;
        return null;
      case 'read':
        return store[key];
      case 'delete':
        store.remove(key);
        return null;
      case 'readAll':
        return Map<String, String>.from(store.map((k, v) => MapEntry(k, v ?? '')));
      case 'deleteAll':
        store.clear();
        return null;
      case 'containsKey':
        return store.containsKey(key);
    }
    return null;
  });
}

void main() {
  setUp(() {
    _mockSecureStorage();
    SharedPreferences.setMockInitialValues({'base_url': 'https://shop.test'});
  });

  test('attaches Bearer token and builds correct URL', () async {
    final store = AuthStore();
    await store.saveTokens(access: 'ACCESS1', refresh: 'REFRESH1');

    late http.Request captured;
    final mock = MockClient((req) async {
      captured = req;
      return http.Response(jsonEncode({'ok': true}), 200);
    });

    final api = ApiClient(store: store, httpClient: mock);
    final data = await api.get('/dashboard/');

    expect((data as Map)['ok'], true);
    expect(captured.url.toString(), 'https://shop.test/api/mobile/v1/dashboard/');
    expect(captured.headers['Authorization'], 'Bearer ACCESS1');
  });

  test('refreshes access token on 401 then retries once', () async {
    final store = AuthStore();
    await store.saveTokens(access: 'OLD', refresh: 'REFRESH1');

    var call = 0;
    final mock = MockClient((req) async {
      if (req.url.path.endsWith('/auth/refresh/')) {
        return http.Response(jsonEncode({'access': 'NEW'}), 200);
      }
      call++;
      if (call == 1) return http.Response('unauthorized', 401);
      // بعد التحديث يجب أن تحمل المحاولة الثانية التوكن الجديد.
      expect(req.headers['Authorization'], 'Bearer NEW');
      return http.Response(jsonEncode({'ok': true}), 200);
    });

    final api = ApiClient(store: store, httpClient: mock);
    final data = await api.get('/dashboard/');
    expect((data as Map)['ok'], true);
    expect(await store.accessToken, 'NEW');
  });

  test('throws ApiException with server detail message', () async {
    final store = AuthStore();
    await store.saveTokens(access: 'A', refresh: 'R');
    final mock = MockClient((req) async {
      return http.Response(jsonEncode({'detail': 'رصيد غير كافٍ'}), 400);
    });
    final api = ApiClient(store: store, httpClient: mock);
    expect(
      () => api.get('/x/'),
      throwsA(isA<ApiException>().having((e) => e.message, 'message', 'رصيد غير كافٍ')),
    );
  });
}
