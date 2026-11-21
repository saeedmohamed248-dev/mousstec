import 'package:flutter/material.dart';

/// هوية Mouss Tec البصرية — أزرق تقني داكن مع لمسة برتقالية.
class AppTheme {
  AppTheme._();

  static const Color primary = Color(0xFF1565C0);
  static const Color accent = Color(0xFFFF6D00);
  static const Color surface = Color(0xFFF5F7FA);

  static ThemeData light() {
    final scheme = ColorScheme.fromSeed(
      seedColor: primary,
      primary: primary,
      secondary: accent,
    );
    return ThemeData(
      useMaterial3: true,
      colorScheme: scheme,
      scaffoldBackgroundColor: surface,
      fontFamily: 'Cairo',
      appBarTheme: const AppBarTheme(
        backgroundColor: primary,
        foregroundColor: Colors.white,
        elevation: 0,
        centerTitle: true,
      ),
      cardTheme: CardThemeData(
        elevation: 1.5,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
        margin: const EdgeInsets.symmetric(vertical: 6, horizontal: 2),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: Colors.white,
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: primary,
          foregroundColor: Colors.white,
          minimumSize: const Size.fromHeight(50),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
      ),
    );
  }

  /// لون تمييزي لكل حالة أمر شغل.
  static Color statusColor(String status) {
    switch (status) {
      case 'quotation':
        return Colors.blueGrey;
      case 'in_progress':
        return Colors.orange;
      case 'quality_check':
        return Colors.purple;
      case 'ready':
        return Colors.green;
      case 'posted':
      case 'delivered':
      case 'done':
      case 'approved':
      case 'accepted':
        return Colors.teal;
      // 🎨 المطابع
      case 'draft':
      case 'design':
        return Colors.blueGrey;
      case 'confirmed':
      case 'sent':
        return Colors.blue;
      case 'proof':
      case 'pending':
        return Colors.amber.shade800;
      case 'printing':
        return Colors.orange;
      case 'finishing':
        return Colors.purple;
      case 'cancelled':
      case 'rejected':
      case 'changes_requested':
      case 'expired':
        return Colors.redAccent;
      default:
        return Colors.grey;
    }
  }
}
