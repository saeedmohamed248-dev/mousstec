"""
Test data factories for Mouss Tec ERP.
Centralized helpers to create test objects with sensible defaults.
"""
from decimal import Decimal
from django.contrib.auth.models import User
from django.utils import timezone

from inventory.models import (
    Branch, Product, Inventory, Customer, Vendor, Treasury,
    PurchaseInvoice, PurchaseInvoiceItem, SaleInvoice, SaleInvoiceItem,
    FinancialTransaction, StockTransfer, ExpenseCategory, EmployeeProfile,
)


def make_branch(name='فرع الاختبار', **kwargs):
    return Branch.objects.create(name=name, **kwargs)


def make_product(part_number=None, name='قطعة اختبار', retail_price='100.00',
                 purchase_price='60.00', average_cost='60.00', **kwargs):
    if part_number is None:
        part_number = f'TEST-{Product.objects.count() + 1:04d}'
    defaults = dict(
        name=name,
        part_number=part_number,
        brand='BMW',
        car_model='E90',
        car_year='2010',
        retail_price=Decimal(retail_price),
        purchase_price=Decimal(purchase_price),
        average_cost=Decimal(average_cost),
    )
    defaults.update(kwargs)
    return Product.objects.create(**defaults)


def make_inventory(product, branch, quantity=10):
    inv, _ = Inventory.objects.get_or_create(
        product=product, branch=branch,
        defaults={'quantity': quantity},
    )
    if inv.quantity != quantity:
        inv.quantity = quantity
        inv.save(update_fields=['quantity'])
    return inv


def make_customer(name='عميل اختبار', **kwargs):
    defaults = dict(name=name, phone='01000000000')
    defaults.update(kwargs)
    return Customer.objects.create(**defaults)


def make_vendor(name='مورد اختبار', **kwargs):
    defaults = dict(name=name, phone='01100000000')
    defaults.update(kwargs)
    return Vendor.objects.create(**defaults)


def make_treasury(branch, name='خزنة اختبار', balance='10000.00', **kwargs):
    defaults = dict(name=name, branch=branch, balance=Decimal(balance), is_active=True)
    defaults.update(kwargs)
    return Treasury.objects.create(**defaults)


def make_expense_category(name='مصاريف عامة'):
    cat, _ = ExpenseCategory.objects.get_or_create(name=name)
    return cat


def make_purchase_invoice(vendor, branch, treasury=None, items=None,
                          paid_amount='0.00', status='draft'):
    """
    Create a purchase invoice with items.
    items: list of (product, quantity, cost_price) tuples
    """
    pi = PurchaseInvoice.objects.create(
        vendor=vendor, branch=branch, treasury=treasury,
        paid_amount=Decimal(paid_amount), status=status,
    )
    if items:
        for product, qty, cost in items:
            PurchaseInvoiceItem.objects.create(
                invoice=pi, product=product,
                quantity=qty, cost_price=Decimal(str(cost)),
            )
        pi.update_total()
    return pi


def make_sale_invoice(customer, branch, treasury=None, items=None,
                      paid_amount=None, status='quotation',
                      invoice_type='sale'):
    """
    Create a sale invoice with items.
    items: list of (product, quantity, unit_price) tuples
    """
    si = SaleInvoice.objects.create(
        invoice_type=invoice_type,
        customer=customer, branch=branch, treasury=treasury,
        paid_amount=Decimal(paid_amount or '0.00'), status=status,
    )
    if items:
        for product, qty, price in items:
            si._skip_update_total = False
            SaleInvoiceItem.objects.create(
                invoice=si, product=product,
                quantity=qty, unit_price=Decimal(str(price)),
                cost_at_sale=product.average_cost,
            )
        si.update_total()
    return si


def make_financial_transaction(treasury, amount, txn_type='in', **kwargs):
    defaults = dict(
        treasury=treasury,
        transaction_type=txn_type,
        amount=Decimal(str(amount)),
        description='حركة اختبار',
    )
    defaults.update(kwargs)
    return FinancialTransaction.objects.create(**defaults)


def make_user(username='testuser', **kwargs):
    defaults = dict(username=username, password='testpass123')
    defaults.update(kwargs)
    return User.objects.create_user(**defaults)


def make_employee(username='testemp', role='cashier', branch=None,
                  commission_balance=None, **user_kwargs):
    """Create User + configure its auto-created EmployeeProfile.

    The signal `create_employee_profile` auto-creates a profile with role='cashier'
    on User.save() inside any tenant schema. This helper just updates that profile
    to the role/branch the test needs.

    🐛 [test-coverage FIX]: After updating the profile we MUST re-fetch the user
    with select_related('employee_profile'), otherwise the cached reverse-OneToOne
    on the original user instance still holds the original (role='cashier') profile
    — and any view that reads `request.user.employee_profile.role` sees stale data
    → role gate denies a freshly-promoted manager → test fails with 403.
    """
    user = make_user(username=username, **user_kwargs)
    profile = EmployeeProfile.objects.get(user=user)
    profile.role = role
    if branch is not None:
        profile.branch = branch
    if commission_balance is not None:
        profile.commission_balance = Decimal(str(commission_balance))
    profile.save()
    # Refresh user with the now-updated profile pre-loaded into its cache.
    user = User.objects.select_related('employee_profile').get(pk=user.pk)
    return user, profile
