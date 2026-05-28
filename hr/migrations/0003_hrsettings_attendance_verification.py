"""
Add attendance verification settings to HRSettings:
- require_face_verification
- require_location
- face_match_threshold
"""

from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hr', '0002_add_ai_design_subscription'),
    ]

    operations = [
        migrations.AddField(
            model_name='hrsettings',
            name='require_face_verification',
            field=models.BooleanField(
                default=False,
                help_text='إذا مُفعّل: لن يتمكن الموظف من تسجيل الحضور بدون التحقق من وجهه بالكاميرا',
                verbose_name='إلزام بصمة الوجه',
            ),
        ),
        migrations.AddField(
            model_name='hrsettings',
            name='require_location',
            field=models.BooleanField(
                default=False,
                help_text='إذا مُفعّل: لن يتمكن الموظف من تسجيل الحضور بدون تفعيل الموقع الجغرافي',
                verbose_name='إلزام تحديد الموقع (GPS)',
            ),
        ),
        migrations.AddField(
            model_name='hrsettings',
            name='face_match_threshold',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0.45'),
                help_text='المسافة الأقصى للمطابقة (أقل = أدق). الافتراضي 0.45 — قيم بين 0.3 و 0.6',
                max_digits=4,
                verbose_name='حد مطابقة الوجه',
            ),
        ),
    ]
