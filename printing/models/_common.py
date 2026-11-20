"""🧰 المطابع — مساعدات مشتركة بين موديولات الموديلات."""
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def _save_with_number(instance, field, prefix, save, *args, **kwargs):
    """
    Save ``instance`` with the next ``<prefix>YYMMDD-NNN`` number in ``field``.

    Two saves in the same instant read the same last number — the second one
    hits the unique constraint, so we retry inside a savepoint.
    """
    from django.db import IntegrityError, transaction
    model = type(instance)
    day_prefix = f"{prefix}{timezone.localdate():%y%m%d}-"
    for _attempt in range(5):
        last = (model.objects.filter(**{f'{field}__startswith': day_prefix})
                .order_by(f'-{field}').values_list(field, flat=True).first())
        try:
            seq = int(last.split('-')[-1]) + 1 if last else 1
        except (ValueError, IndexError):
            seq = 1
        setattr(instance, field, f'{day_prefix}{seq:03d}')
        try:
            with transaction.atomic():
                return save(*args, **kwargs)
        except IntegrityError:
            setattr(instance, field, '')
    raise ValidationError(_("تعذّر توليد رقم فريد — حاول مرة أخرى."))
