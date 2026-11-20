import 'dart:async';
import 'dart:io' show Platform;

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../core/i18n.dart';
import 'api_service.dart';

/// 🔔 إشعارات Firebase (FCM).
///
/// بتشتغل بس لو ملفات Firebase موجودة في المشروع (google-services.json لأندرويد،
/// GoogleService-Info.plist لـ iOS — راجع README). من غيرها التطبيق شغال عادي
/// والإشعارات بس هي اللي مقفولة — مفيش أي كراش.
class PushService {
  PushService._();
  static final PushService instance = PushService._();

  /// لعرض الإشعار جوه التطبيق وهو مفتوح.
  final GlobalKey<ScaffoldMessengerState> messengerKey = GlobalKey<ScaffoldMessengerState>();

  bool _ready = false;
  String? _token;
  StreamSubscription<String>? _refreshSub;

  bool get isReady => _ready;

  Future<void> init() async {
    if (kIsWeb) return;
    try {
      await Firebase.initializeApp();
      FirebaseMessaging.onMessage.listen(_showInApp);
      _ready = true;
    } catch (e) {
      debugPrint('[PUSH] Firebase مش متظبط — الإشعارات مقفولة: $e');
      _ready = false;
    }
  }

  /// بعد الدخول: إذن الإشعارات + تسجيل الجهاز على السيرفر.
  Future<void> registerDevice(ApiService api) async {
    if (!_ready) return;
    try {
      final messaging = FirebaseMessaging.instance;
      final perm = await messaging.requestPermission();
      if (perm.authorizationStatus == AuthorizationStatus.denied) return;
      _token = await messaging.getToken();
      if (_token != null) await _send(api, _token!);
      await _refreshSub?.cancel();
      _refreshSub = messaging.onTokenRefresh.listen((t) {
        _token = t;
        _send(api, t);
      });
    } catch (e) {
      debugPrint('[PUSH] فشل تسجيل الجهاز: $e');
    }
  }

  Future<void> _send(ApiService api, String token) => api.registerDevice(
        token: token,
        platform: Platform.isIOS ? 'ios' : 'android',
        language: AppLanguage.instance.code,
      );

  /// عند الخروج: الجهاز مايستقبلش إشعارات الحساب ده تاني.
  Future<void> unregister(ApiService api) async {
    await _refreshSub?.cancel();
    _refreshSub = null;
    final t = _token;
    _token = null;
    if (t == null) return;
    try {
      await api.unregisterDevice(t);
    } catch (_) {}
  }

  void _showInApp(RemoteMessage message) {
    final n = message.notification;
    if (n == null) return;
    messengerKey.currentState?.showSnackBar(SnackBar(
      content: Text([n.title, n.body].whereType<String>().join('\n')),
      duration: const Duration(seconds: 5),
    ));
  }
}
