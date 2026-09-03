import 'dart:convert';

import 'package:http/http.dart' as http;

import 'auth_store.dart';
import 'constants.dart';

/// خطأ عام قادم من الـ API مع رسالة قابلة للعرض للمستخدم.
class ApiException implements Exception {
  ApiException(this.message, {this.statusCode});
  final String message;
  final int? statusCode;

  bool get isUnauthorized => statusCode == 401;

  @override
  String toString() => message;
}

/// عميل HTTP رفيع حول package:http يتولّى:
///   • بناء الرابط من (baseUrl + apiPath + endpoint)
///   • إرفاق ترويسة Bearer تلقائياً
///   • تحديث الـ access token تلقائياً عند 401 ثم إعادة المحاولة مرة واحدة
///   • تحويل الأخطاء إلى ApiException بعربية مفهومة
class ApiClient {
  ApiClient({required this.store, http.Client? httpClient})
      : _http = httpClient ?? http.Client();

  final AuthStore store;
  final http.Client _http;

  Future<Uri> _uri(String endpoint, [Map<String, dynamic>? query]) async {
    final base = await store.getBaseUrl();
    final normalized = endpoint.startsWith('/') ? endpoint : '/$endpoint';
    final qp = query?.map((k, v) => MapEntry(k, '$v'));
    return Uri.parse('$base${ApiConfig.apiPath}$normalized')
        .replace(queryParameters: (qp == null || qp.isEmpty) ? null : qp);
  }

  Future<Map<String, String>> _headers({bool auth = true}) async {
    final headers = {'Content-Type': 'application/json', 'Accept': 'application/json'};
    if (auth) {
      final token = await store.accessToken;
      if (token != null) headers['Authorization'] = 'Bearer $token';
    }
    return headers;
  }

  Future<dynamic> get(String endpoint, {Map<String, dynamic>? query, bool auth = true}) {
    return _send(() async => _http.get(await _uri(endpoint, query), headers: await _headers(auth: auth)),
        auth: auth, retryOn401: auth);
  }

  Future<dynamic> post(String endpoint, {Object? body, bool auth = true}) {
    return _send(
      () async => _http.post(await _uri(endpoint),
          headers: await _headers(auth: auth), body: jsonEncode(body ?? {})),
      auth: auth,
      retryOn401: auth,
    );
  }

  Future<dynamic> _send(
    Future<http.Response> Function() run, {
    required bool auth,
    required bool retryOn401,
  }) async {
    http.Response resp;
    try {
      resp = await run();
    } catch (e) {
      throw ApiException('تعذّر الاتصال بالخادم. تأكد من الإنترنت ورابط الورشة.');
    }

    // تحديث التوكن تلقائياً ومحاولة واحدة إضافية.
    if (resp.statusCode == 401 && retryOn401) {
      final refreshed = await _tryRefresh();
      if (refreshed) {
        try {
          resp = await run();
        } catch (_) {
          throw ApiException('تعذّر الاتصال بالخادم.');
        }
      }
    }

    return _decode(resp);
  }

  Future<bool> _tryRefresh() async {
    final refresh = await store.refreshToken;
    if (refresh == null) return false;
    try {
      final resp = await _http.post(
        await _uri('/auth/refresh/'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'refresh': refresh}),
      );
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as Map<String, dynamic>;
        await store.saveAccess(data['access'] as String);
        return true;
      }
    } catch (_) {}
    return false;
  }

  dynamic _decode(http.Response resp) {
    final body = resp.body.isEmpty ? null : jsonDecode(utf8.decode(resp.bodyBytes));
    if (resp.statusCode >= 200 && resp.statusCode < 300) {
      return body;
    }
    throw ApiException(_extractError(body, resp.statusCode), statusCode: resp.statusCode);
  }

  String _extractError(dynamic body, int status) {
    if (status == 401) return 'انتهت الجلسة. من فضلك سجّل الدخول مجدداً.';
    if (body is Map) {
      for (final key in ['detail', 'message', 'error']) {
        if (body[key] is String) return body[key] as String;
      }
      // أخطاء التحقق الحقلية.
      final first = body.values.isNotEmpty ? body.values.first : null;
      if (first is List && first.isNotEmpty) return '${first.first}';
      if (first is String) return first;
    }
    return 'حدث خطأ ($status). حاول مرة أخرى.';
  }

  void close() => _http.close();
}
