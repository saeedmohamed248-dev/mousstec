"""⚡ تأسيس الشركات الجديدة من قالب schema جاهز (بدل تشغيل كل المهاجرات).

تأسيس شركة كان بيشغّل كل مهاجرات التطبيقات (~٥٠ ثانية): التسجيل بيلف دقيقة
والعميل مستني (ولو المتصفح قفل في النص الشركة بتتعمل وهو مايعرفش)، وكل كلاس
اختبار بيستنى نفس الدقيقة — فالسويت الكاملة ساعة ونص ومحدش بيشغّلها في CI.

دلوقتي فيه schema «قالب» متهاجرة (مافيهاش غير اللي المهاجرات نفسها بتعمله)،
والشركة الجديدة = نسخة منها (~ثانيتين) + migrate حقيقي على النسخة:
  • لو القالب محدّث، الـ migrate مابيلاقيش حاجة يعملها.
  • لو القالب قديم (نشر جديد قبل ما يتحدّث)، الـ migrate بيكمّل الناقص —
    مش «fake» زي TENANT_CREATION_FAKES_MIGRATIONS في django-tenants، اللي
    مع قالب قديم بيعلّم مهاجرات إنها اتعملت وجداولها مش موجودة.
لو أي حاجة فشلت بنرجع للطريقة العادية (مافيش شركة بتتعمل نص مستوية).

القالب بيتعمل/يتحدّث بـ `manage.py refresh_tenant_template` (في سكريبت النشر)،
ولو مش موجود أول شركة بتعمله.
"""
import logging
import time

from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction
from django_tenants.utils import schema_exists

logger = logging.getLogger('mouss_tec_core')

# مفتاح pg_advisory_lock: بناء/تحديث القالب حصري، والنسخ منه مشترك (shared)
_LOCK_KEY = 804_197_701


def template_schema_name() -> str:
    """اسم القالب من الإعدادات — فاضي = الميزة مقفولة (التأسيس العادي)."""
    return (getattr(settings, 'TENANT_TEMPLATE_SCHEMA', '') or '').strip()


def _clone_function_exists() -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public' AND p.proname = 'clone_schema' LIMIT 1")
        return cur.fetchone() is not None


def ensure_template(verbosity=0) -> str:
    """يعمل القالب لو مش موجود ويطبّق عليه أي مهاجرات ناقصة. بيرجع اسمه ('' = مقفول)."""
    name = template_schema_name()
    if not name:
        return ''
    from django_tenants.clone import CloneSchema
    connection.set_schema_to_public()
    with connection.cursor() as cur:
        cur.execute('SELECT pg_advisory_lock(%s)', [_LOCK_KEY])
    try:
        if not schema_exists(name):
            with connection.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{name}"')
        call_command('migrate_schemas', tenant=True, schema_name=name,
                     interactive=False, verbosity=verbosity)
        connection.set_schema_to_public()
        # دالة النسخ (من django-tenants) بتتعمل هنا بس — تحت القفل الحصري — عشان
        # تسجيلين في نفس اللحظة مايعملوش CREATE OR REPLACE FUNCTION مع بعض.
        CloneSchema()._create_clone_schema_function()
    finally:
        connection.set_schema_to_public()
        with connection.cursor() as cur:
            cur.execute('SELECT pg_advisory_unlock(%s)', [_LOCK_KEY])
    return name


def create_from_template(schema_name, verbosity=0) -> bool:
    """يعمل schema الشركة من القالب. True = اتعملت؛ False = اعمل التأسيس العادي."""
    name = template_schema_name()
    if not name or name == schema_name:
        return False
    started = time.monotonic()
    try:
        if not schema_exists(name) or not _clone_function_exists():
            ensure_template(verbosity)
        connection.set_schema_to_public()
        with connection.cursor() as cur:
            cur.execute('SELECT pg_advisory_lock_shared(%s)', [_LOCK_KEY])
        try:
            # savepoint: لو النسخ أو المهاجرات فشلت، الـ schema بتترجع كأنها ماتعملتش
            with transaction.atomic():
                with connection.cursor() as cur:
                    # النسخ جملة SQL واحدة كبيرة — الحد العام (statement_timeout ٣٠ ث)
                    # كان بيقطعها على سيرفر مشغول فترجع للمهاجرات البطيئة.
                    cur.execute("SET LOCAL statement_timeout = '300s'")
                    cur.execute('SELECT clone_schema(%s, %s, %s)', [name, schema_name, 'DATA'])
                call_command('migrate_schemas', tenant=True, schema_name=schema_name,
                             interactive=False, verbosity=verbosity)
        finally:
            connection.set_schema_to_public()
            with connection.cursor() as cur:
                cur.execute('SELECT pg_advisory_unlock_shared(%s)', [_LOCK_KEY])
    except Exception:
        logger.exception("[TENANT TEMPLATE] cloning %s → %s failed — falling back to migrations",
                         name, schema_name)
        connection.set_schema_to_public()
        return False
    logger.info("[TENANT TEMPLATE] schema %s ready from template in %.1fs",
                schema_name, time.monotonic() - started)
    return True
