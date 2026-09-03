import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../core/theme.dart';

final _money = NumberFormat.currency(locale: 'ar_EG', symbol: 'ج.م ', decimalDigits: 2);
String formatMoney(num v) => _money.format(v);

/// بطاقة مؤشر رقمي في لوحة المعلومات.
class MetricCard extends StatelessWidget {
  const MetricCard({
    super.key,
    required this.title,
    required this.value,
    required this.icon,
    required this.color,
    this.onTap,
  });

  final String title;
  final String value;
  final IconData icon;
  final Color color;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(16),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Container(
                    padding: const EdgeInsets.all(8),
                    decoration: BoxDecoration(
                      color: color.withOpacity(0.12),
                      borderRadius: BorderRadius.circular(10),
                    ),
                    child: Icon(icon, color: color, size: 22),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              Text(value,
                  style: const TextStyle(fontSize: 24, fontWeight: FontWeight.bold),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis),
              const SizedBox(height: 4),
              Text(title, style: TextStyle(color: Colors.grey.shade600, fontSize: 13)),
            ],
          ),
        ),
      ),
    );
  }
}

/// شارة ملوّنة لحالة أمر الشغل.
class StatusChip extends StatelessWidget {
  const StatusChip({super.key, required this.status, required this.label});
  final String status;
  final String label;

  @override
  Widget build(BuildContext context) {
    final color = AppTheme.statusColor(status);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: color.withOpacity(0.14),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: color.withOpacity(0.4)),
      ),
      child: Text(label,
          style: TextStyle(color: color, fontWeight: FontWeight.w600, fontSize: 12)),
    );
  }
}

/// حالة تحميل/خطأ/فارغ موحّدة.
class StateView extends StatelessWidget {
  const StateView.loading({super.key})
      : type = _StateType.loading,
        message = null,
        onRetry = null;
  const StateView.error(this.message, {super.key, this.onRetry}) : type = _StateType.error;
  const StateView.empty(this.message, {super.key})
      : type = _StateType.empty,
        onRetry = null;

  final _StateType type;
  final String? message;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    switch (type) {
      case _StateType.loading:
        return const Center(child: CircularProgressIndicator());
      case _StateType.empty:
        return Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.inbox_outlined, size: 56, color: Colors.grey.shade400),
              const SizedBox(height: 12),
              Text(message ?? 'لا توجد بيانات', style: TextStyle(color: Colors.grey.shade600)),
            ],
          ),
        );
      case _StateType.error:
        return Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.error_outline, size: 56, color: Colors.redAccent),
                const SizedBox(height: 12),
                Text(message ?? 'حدث خطأ', textAlign: TextAlign.center),
                if (onRetry != null) ...[
                  const SizedBox(height: 16),
                  OutlinedButton.icon(
                    onPressed: onRetry,
                    icon: const Icon(Icons.refresh),
                    label: const Text('إعادة المحاولة'),
                  ),
                ],
              ],
            ),
          ),
        );
    }
  }
}

enum _StateType { loading, error, empty }
