"""
🔑 جداول توكنات JWT جوه schema كل شركة.

token_blacklist كان في SHARED_APPS بس — فجدول outstandingtoken كان في public
ومربوط بـ public.auth_user، بينما مستخدمين الشركة في الـ schema بتاعها ⇒ أي
تسجيل دخول من تطبيق الموبايل كان بيقع بـ ForeignKeyViolation.

ضفنا التطبيق لـ TENANT_APPS، بس الشركات الموجودة متسجّل عندها إن مهاجرات
token_blacklist اتطبّقت (كانت no-op) فمش هتتعمل تاني — المهاجرة دي بتعمل
الجدولين لو مش موجودين في الـ schema الحالي (والشركات الجديدة بتاخدهم من
مهاجرات token_blacklist نفسها فالمهاجرة دي بتعدّي).
"""
from django.db import migrations

TABLES = ('token_blacklist_outstandingtoken', 'token_blacklist_blacklistedtoken')


def create_missing_jwt_tables(apps, schema_editor):
    conn = schema_editor.connection
    schema = getattr(conn, 'schema_name', 'public')
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name IN %s",
            [schema, TABLES])
        have = {row[0] for row in cur.fetchall()}
    if 'token_blacklist_outstandingtoken' not in have:
        schema_editor.create_model(apps.get_model('token_blacklist', 'OutstandingToken'))
    if 'token_blacklist_blacklistedtoken' not in have:
        schema_editor.create_model(apps.get_model('token_blacklist', 'BlacklistedToken'))


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('token_blacklist', '0013_alter_blacklistedtoken_options_and_more'),
    ]

    operations = [
        migrations.RunPython(create_missing_jwt_tables, migrations.RunPython.noop),
    ]
