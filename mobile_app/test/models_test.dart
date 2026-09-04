import 'package:flutter_test/flutter_test.dart';
import 'package:mousstec_mobile/models/models.dart';

void main() {
  group('DashboardSummary', () {
    test('parses all fields including string decimals', () {
      final d = DashboardSummary.fromJson({
        'open_work_orders': 5,
        'ready_for_delivery': 2,
        'in_progress': 1,
        'quality_check': 1,
        'quotations': 1,
        'revenue_today': '1250.50',
        'low_stock_alerts': 3,
        'total_customers': 40,
        'total_products': 120,
      });
      expect(d.openWorkOrders, 5);
      expect(d.revenueToday, 1250.50);
      expect(d.lowStockAlerts, 3);
    });
  });

  group('WorkOrder', () {
    test('parses nested items and services', () {
      final o = WorkOrder.fromJson({
        'id': 10,
        'status': 'in_progress',
        'status_display': 'قيد العمل',
        'customer_name': 'أحمد',
        'customer_phone': '0100',
        'vehicle_plate': 'ن ص ر 1',
        'total_amount': '500.00',
        'paid_amount': '200.00',
        'due_amount': '300.00',
        'items': [
          {'product_name': 'فلتر', 'part_number': 'F1', 'quantity': 2, 'unit_price': '50.00', 'total_price': '100.00'}
        ],
        'service_items': [
          {'service_name': 'تغيير زيت', 'price': '150.00', 'actual_hours': '1.5'}
        ],
      });
      expect(o.id, 10);
      expect(o.items.length, 1);
      expect(o.items.first.totalPrice, 100.0);
      expect(o.services.first.hours, 1.5);
      expect(o.dueAmount, 300.0);
    });

    test('handles missing optional fields gracefully', () {
      final o = WorkOrder.fromJson({
        'id': 1,
        'status': 'quotation',
        'status_display': 'عرض سعر',
        'customer_name': 'x',
        'customer_phone': 'y',
        'total_amount': 0,
        'paid_amount': 0,
        'due_amount': 0,
      });
      expect(o.vehiclePlate, isNull);
      expect(o.items, isEmpty);
    });
  });

  group('Product', () {
    test('parses stock locations and low stock flag', () {
      final p = Product.fromJson({
        'id': 3,
        'name': 'طلمبة',
        'part_number': 'WP-1',
        'brand': 'BMW',
        'retail_price': '900.00',
        'total_quantity': 1,
        'min_stock_level': 3,
        'is_low_stock': true,
        'stock_by_branch': [
          {'branch_name': 'الرئيسي', 'quantity': 1, 'shelf_location': 'A3'}
        ],
      });
      expect(p.isLowStock, true);
      expect(p.stockByBranch.single.quantity, 1);
      expect(p.stockByBranch.single.shelf, 'A3');
    });
  });

  group('Paginated', () {
    test('parses DRF page envelope', () {
      final page = Paginated.fromJson({
        'count': 2,
        'next': 'http://x/?page=2',
        'results': [
          {'id': 1, 'name': 'a', 'part_number': 'P1', 'brand': 'BMW', 'retail_price': '1', 'total_quantity': 1, 'min_stock_level': 1, 'is_low_stock': false},
          {'id': 2, 'name': 'b', 'part_number': 'P2', 'brand': 'BMW', 'retail_price': '1', 'total_quantity': 1, 'min_stock_level': 1, 'is_low_stock': false},
        ],
      }, Product.fromJson);
      expect(page.count, 2);
      expect(page.hasNext, true);
      expect(page.results.length, 2);
    });
  });
}
