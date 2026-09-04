import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:http/http.dart' as http;
import 'package:mousstec_mobile/core/api_client.dart';
import 'package:mousstec_mobile/core/auth_store.dart';
import 'package:mousstec_mobile/providers/auth_provider.dart';
import 'package:mousstec_mobile/screens/login_screen.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

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
      case 'deleteAll':
        store.clear();
        return null;
      case 'readAll':
        return <String, String>{};
      case 'containsKey':
        return store.containsKey(key);
    }
    return null;
  });
}

Widget _wrap(AuthProvider provider) => ChangeNotifierProvider.value(
      value: provider,
      child: const Directionality(
        textDirection: TextDirection.rtl,
        child: MaterialApp(home: LoginScreen()),
      ),
    );

void main() {
  setUp(() {
    _mockSecureStorage();
    SharedPreferences.setMockInitialValues({});
  });

  testWidgets('shows validation errors when fields empty', (tester) async {
    final provider = AuthProvider();
    await tester.pumpWidget(_wrap(provider));
    await tester.pumpAndSettle();

    await tester.tap(find.text('تسجيل الدخول'));
    await tester.pump();

    expect(find.text('أدخل اسم المستخدم'), findsOneWidget);
    expect(find.text('أدخل كلمة المرور'), findsOneWidget);
  });

  testWidgets('successful login flips status to authenticated', (tester) async {
    final store = AuthStore();
    final mock = MockClient((req) async {
      return http.Response(
        jsonEncode({
          'access': 'A',
          'refresh': 'R',
          'user': {'id': 1, 'username': 'tech1', 'full_name': 'الفني'},
        }),
        200,
      );
    });
    final provider = AuthProvider(store: store, api: ApiClient(store: store, httpClient: mock));
    await tester.pumpWidget(_wrap(provider));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextFormField).at(0), 'tech1');
    await tester.enterText(find.byType(TextFormField).at(1), 'pass12345');
    await tester.tap(find.text('تسجيل الدخول'));
    await tester.pumpAndSettle();

    expect(provider.status, AuthStatus.authenticated);
    expect(provider.user?.username, 'tech1');
  });
}
