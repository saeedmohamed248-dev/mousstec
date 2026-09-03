import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'constants.dart';

/// تخزين آمن للتوكنات + تخزين عادي لرابط الخادم.
///
/// التوكنات تُحفظ في flutter_secure_storage (Keychain على iOS / Keystore على
/// أندرويد)، بينما رابط الخادم — وهو غير حسّاس — يُحفظ في SharedPreferences.
class AuthStore {
  AuthStore({FlutterSecureStorage? secure}) : _secure = secure ?? const FlutterSecureStorage();

  final FlutterSecureStorage _secure;

  Future<String> getBaseUrl() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString(ApiConfig.kBaseUrl) ?? ApiConfig.defaultBaseUrl;
  }

  Future<void> setBaseUrl(String url) async {
    final prefs = await SharedPreferences.getInstance();
    // نزيل أي "/" زائدة في النهاية لتفادي مسارات مكرّرة.
    final clean = url.trim().replaceAll(RegExp(r'/+$'), '');
    await prefs.setString(ApiConfig.kBaseUrl, clean);
  }

  Future<String?> get accessToken => _secure.read(key: ApiConfig.kAccessToken);
  Future<String?> get refreshToken => _secure.read(key: ApiConfig.kRefreshToken);

  Future<void> saveTokens({required String access, required String refresh}) async {
    await _secure.write(key: ApiConfig.kAccessToken, value: access);
    await _secure.write(key: ApiConfig.kRefreshToken, value: refresh);
  }

  Future<void> saveAccess(String access) async {
    await _secure.write(key: ApiConfig.kAccessToken, value: access);
  }

  Future<void> clear() async {
    await _secure.delete(key: ApiConfig.kAccessToken);
    await _secure.delete(key: ApiConfig.kRefreshToken);
  }
}
