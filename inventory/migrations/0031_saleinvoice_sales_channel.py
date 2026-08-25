from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0030_disassembly_templates'),
    ]

    operations = [
        migrations.AddField(
            model_name='saleinvoice',
            name='sales_channel',
            field=models.CharField(
                choices=[('in_store', 'بيع مباشر (المحل)'), ('website', 'الموقع الإلكتروني')],
                db_index=True, default='in_store',
                help_text='مباشر من المحل أم أوردر من الموقع الإلكتروني',
                max_length=20, verbose_name='قناة البيع'),
        ),
        migrations.RunSQL(
            # الفواتير القديمة اللي جت من الموقع (متعلّمة بماركر FixIt في الملاحظات)
            # نحدّثها لقناة website أثناء الترقية.
            sql="UPDATE inventory_saleinvoice SET sales_channel='website' WHERE notes LIKE '%[FixIt %';",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
