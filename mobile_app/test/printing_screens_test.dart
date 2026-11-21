import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mousstec_mobile/core/api_client.dart';
import 'package:mousstec_mobile/core/auth_store.dart';
import 'package:mousstec_mobile/providers/auth_provider.dart';
import 'package:mousstec_mobile/screens/home_shell.dart';
import 'package:mousstec_mobile/screens/printing/print_order_detail_screen.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

http.Response _json(Object body, [int status = 200]) => http.Response.bytes(
      utf8.encode(jsonEncode(body)),
      status,
      headers: {'content-type': 'application/json; charset=utf-8'},
    );

void _mockSecureStorage() {
  final store = <String, String?>{};
  const channel = MethodChannel('plugins.it_nomads.com/flutter_secure_storage');
  TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(channel, (
    call,
  ) async {
    final args = (call.arguments as Map?) ?? {};
    final key = args['key'] as String?;
    if (call.method == 'write') store[key!] = args['value'] as String?;
    if (call.method == 'read') return store[key];
    if (call.method == 'delete') store.remove(key);
    return null;
  });
}

final _order = {
  'id': 7,
  'order_number': 'PO-1',
  'customer_name': 'شركة الندى',
  'customer_phone': '0101',
  'status': 'confirmed',
  'status_display': 'مؤكد',
  'net_total': '300.00',
  'paid_amount': '0.00',
  'remaining': '300.00',
  'discount': '0.00',
  'allowed_actions': ['deliver', 'cancel'],
  'jobs': [
    {
      'id': 3,
      'description': 'كروت بزنس',
      'quantity': 2,
      'unit_price': '150.00',
      'total_price': '300.00',
      'stage': 'proof',
      'stage_display': 'بروفة',
      'next_stage': 'printing',
      'blocked': true,
    },
  ],
};

Future<AuthProvider> _printingProvider(List<String> perms, List<String> calls) async {
  final store = AuthStore();
  final mock = MockClient((req) async {
    calls.add('${req.method} ${req.url.path}');
    final path = req.url.path.replaceFirst('/api/mobile/v1', '');
    if (path == '/auth/login/') {
      return _json({
        'access': 'A',
        'refresh': 'R',
        'user': {'id': 1, 'username': 'p', 'full_name': 'سعيد'},
        'tenant': {'name': 'مطبعة النور', 'industry': 'printing', 'currency': 'EGP', 'permissions': perms},
      });
    }
    if (path == '/print/dashboard/') {
      return _json({'open_orders': 4, 'ready': 1, 'jobs_by_stage': {'design': 2}, 'sales_today': null});
    }
    if (path == '/print/orders/7/') return _json(_order);
    if (path.startsWith('/print/')) return _json({'count': 0, 'next': null, 'results': []});
    return _json({'detail': 'not found'}, 404);
  });
  final p = AuthProvider(store: store, api: ApiClient(store: store, httpClient: mock));
  await store.setBaseUrl('https://nour.mousstec.com');
  await p.login('p', 'x');
  return p;
}

Widget _app(AuthProvider p, Widget home) =>
    ChangeNotifierProvider.value(value: p, child: MaterialApp(home: home));

void main() {
  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
    _mockSecureStorage();
    SharedPreferences.setMockInitialValues({});
  });

  testWidgets('print shop gets printing tabs and dashboard (no workshop calls)', (tester) async {
    final calls = <String>[];
    final p = await _printingProvider(['view_printorder', 'view_printjob', 'add_printorder'], calls);
    await tester.pumpWidget(_app(p, const HomeShell()));
    await tester.pumpAndSettle();
    expect(find.text('الطلبات'), findsOneWidget);
    expect(find.text('الإنتاج'), findsWidgets);
    expect(find.text('أوامر الشغل'), findsNothing);
    expect(find.text('مطبعة النور'), findsOneWidget);
    expect(calls.any((c) => c.contains('/dashboard/') && !c.contains('/print/')), isFalse);
  });

  testWidgets('order detail hides stage button while proof is pending, shows deliver', (tester) async {
    final calls = <String>[];
    final p = await _printingProvider(['view_printorder', 'change_printorder', 'change_printjob'], calls);
    await tester.pumpWidget(_app(p, const PrintOrderDetailScreen(orderId: 7)));
    await tester.pumpAndSettle();
    expect(find.text('شركة الندى'), findsOneWidget);
    expect(find.text('⏳ مستني موافقة العميل على البروفة'), findsOneWidget);
    expect(find.textContaining('لـ «'), findsNothing);
    expect(find.text('اتسلّم'), findsOneWidget);
    expect(find.text('إلغاء الطلب'), findsOneWidget);
  });

  testWidgets('order actions hidden without change permission', (tester) async {
    final calls = <String>[];
    final p = await _printingProvider(['view_printorder'], calls);
    await tester.pumpWidget(_app(p, const PrintOrderDetailScreen(orderId: 7)));
    await tester.pumpAndSettle();
    expect(find.text('اتسلّم'), findsNothing);
    expect(find.text('البروفات'), findsNothing);
  });
}
