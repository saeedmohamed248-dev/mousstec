"""
🔧 Repair Atlas — Models

أربع كيانات أساسية:
    1. RepairSession  — جلسة شغل واحدة لفني (ممكن تضم أسئلة كتير على نفس العربية)
    2. RepairQuery    — السؤال (نص + موديل + ماركة + وضع: تفكيك/تركيب/ضفائر)
    3. RepairAnswer   — رد البوت (نص + ميتاداتا + علم "Verified" من SuperAdmin)
    4. TechPhoto      — صور الفني (كل صورة مربوطة بسؤال + رد Vision من البوت)

+ كيان معرفة "محفوظة" مستقل:
    5. VerifiedKnowledge — إجابة تم اعتمادها من SuperAdmin → تتسحب من DB مباشرة
       قبل أي استدعاء LLM في المرات الجاية على نفس النوع من السؤال.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------
class RepairMode(models.TextChoices):
    DISASSEMBLY = 'disassembly', 'تفكيك'
    INSTALL = 'install', 'تركيب'
    WIRING = 'wiring', 'ضفيرة / Wiring'
    LOCATE = 'locate', 'تحديد مكان قطعة'


class AnswerSource(models.TextChoices):
    LLM = 'llm', 'AI مولّد'
    VERIFIED = 'verified', 'معتمد (SuperAdmin)'
    LLM_VERIFIED = 'llm_verified', 'AI + معتمد لاحقاً'
    AI_AUTO_VERIFIED = 'ai_auto_verified', 'AI ذاتي التحقق (Verifier ≥85)'


class ConfidenceTier(models.TextChoices):
    HIGH = 'high', '🟢 موثوق'
    MEDIUM = 'medium', '🟡 محتمل'
    LOW = 'low', '🔴 غير مؤكد'
    UNKNOWN = 'unknown', '⚪ لم يفحص'


class VerificationStatus(models.TextChoices):
    PENDING = 'pending', 'بانتظار المراجعة'
    APPROVED = 'approved', 'معتمد'
    CORRECTED = 'corrected', 'تم تصحيحه'
    REJECTED = 'rejected', 'مرفوض'


# ---------------------------------------------------------------------------
# Session — تجميع أسئلة الفني على نفس العربية في "جلسة"
# ---------------------------------------------------------------------------
class RepairSession(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='repair_sessions',
        verbose_name='الفني',
    )
    brand = models.CharField(max_length=40, blank=True, db_index=True,
                             verbose_name='الماركة')
    model_name = models.CharField(max_length=80, blank=True,
                                   verbose_name='الموديل')
    year = models.PositiveSmallIntegerField(null=True, blank=True,
                                             verbose_name='السنة')
    engine_code = models.CharField(max_length=40, blank=True,
                                    verbose_name='كود المحرك')
    vin = models.CharField(max_length=20, blank=True, db_index=True,
                            verbose_name='VIN')

    title = models.CharField(max_length=200, blank=True,
                              help_text='ملخص قصير للجلسة (auto-filled من أول سؤال)')
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'جلسة إصلاح'
        verbose_name_plural = '🔧 جلسات الإصلاح'
        ordering = ['-created_at']

    def __str__(self) -> str:
        vehicle = ' '.join(p for p in [self.brand, self.model_name,
                                        str(self.year) if self.year else '']
                            if p).strip()
        return f'{self.user.username} — {vehicle or "جلسة"}'


# ---------------------------------------------------------------------------
# Query + Answer
# ---------------------------------------------------------------------------
class RepairQuery(models.Model):
    session = models.ForeignKey(RepairSession, on_delete=models.CASCADE,
                                 related_name='queries')
    mode = models.CharField(max_length=20, choices=RepairMode.choices,
                             default=RepairMode.DISASSEMBLY, db_index=True,
                             verbose_name='الوضع')
    part_or_system = models.CharField(max_length=160, blank=True, db_index=True,
                                       verbose_name='القطعة / النظام',
                                       help_text='مستخرج من السؤال — ييخدم البحث في الـ Verified KB')
    question_text = models.TextField(verbose_name='السؤال')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # كاش الإجابة الأخيرة (لو الفني سأل نفس السؤال جوه نفس الجلسة)
    last_answer = models.ForeignKey(
        'RepairAnswer', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )

    class Meta:
        verbose_name = 'سؤال إصلاح'
        verbose_name_plural = '❓ أسئلة الإصلاح'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'[{self.get_mode_display()}] {self.question_text[:80]}'


class RepairAnswer(models.Model):
    query = models.ForeignKey(RepairQuery, on_delete=models.CASCADE,
                               related_name='answers')
    body_markdown = models.TextField(verbose_name='الإجابة')
    source = models.CharField(max_length=20, choices=AnswerSource.choices,
                               default=AnswerSource.LLM, verbose_name='المصدر')

    # Verified KB lookup result — لو الإجابة جت من معرفة محفوظة، نخزن المرجع
    verified_kb = models.ForeignKey(
        'VerifiedKnowledge', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='served_answers',
    )

    # SuperAdmin review trail
    review_status = models.CharField(
        max_length=20, choices=VerificationStatus.choices,
        default=VerificationStatus.PENDING, db_index=True,
        verbose_name='حالة المراجعة',
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, verbose_name='ملاحظة المراجع')

    # Cost telemetry — to match diagnostic spend tracking elsewhere
    llm_model_used = models.CharField(max_length=80, blank=True)
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    cost_cents = models.PositiveIntegerField(default=0)

    # 🔎 Self-Verifier — البوت بيراجع نفسه
    confidence_score = models.PositiveSmallIntegerField(
        default=0,
        help_text='0-100 من الـ Verifier LLM. ≥85 → auto-promoted to KB.',
    )
    confidence_tier = models.CharField(
        max_length=10, choices=ConfidenceTier.choices,
        default=ConfidenceTier.UNKNOWN, db_index=True,
        verbose_name='شارة الثقة',
    )
    verifier_doubts = models.JSONField(
        default=list, blank=True,
        help_text='قائمة الشكوك اللي طلعها الـ Verifier',
    )
    auto_promoted = models.BooleanField(
        default=False, db_index=True,
        help_text='True لو الإجابة دي اتـ promote تلقائياً لـ VerifiedKnowledge',
    )
    revision_count = models.PositiveSmallIntegerField(
        default=0,
        help_text='كام مرة الـ Generator أعاد بعد ملاحظات الـ Verifier',
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'إجابة'
        verbose_name_plural = '💬 الإجابات'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.get_source_display()} — {self.body_markdown[:60]}'


# ---------------------------------------------------------------------------
# Photo loop — الفني يصوّر، البوت يرد على الصورة
# ---------------------------------------------------------------------------
def _photo_upload_path(instance: 'TechPhoto', filename: str) -> str:
    return f'repair_atlas/photos/{instance.query.session_id}/{filename}'


class TechPhoto(models.Model):
    query = models.ForeignKey(RepairQuery, on_delete=models.CASCADE,
                               related_name='photos')
    image = models.ImageField(upload_to=_photo_upload_path,
                               verbose_name='الصورة')
    caption = models.CharField(max_length=200, blank=True,
                                verbose_name='تعليق الفني')

    # Vision response — رد البوت بعد ما حلّل الصورة
    ai_feedback = models.TextField(blank=True, verbose_name='تعليق البوت')
    ai_verdict = models.CharField(
        max_length=20, blank=True,
        choices=[
            ('correct', 'صح كمل'),
            ('warn', 'انتبه'),
            ('wrong', 'غلط ارجع'),
            ('unclear', 'الصورة مش واضحة'),
        ],
        verbose_name='تقييم البوت',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'صورة فني'
        verbose_name_plural = '📸 صور الفنيين'
        ordering = ['-created_at']


# ---------------------------------------------------------------------------
# Verified Knowledge — المعرفة اللي SuperAdmin اعتمدها
# ---------------------------------------------------------------------------
class VerifiedKnowledge(models.Model):
    """
    إجابة معتمدة. لما الفني يسأل سؤال شبه السؤال ده على نفس الموديل،
    بنرجّع المعرفة دي مباشرة بدون استدعاء LLM (أسرع + أرخص + أدق).

    البحث بيتم على tuple = (brand_norm, model_norm, mode, part_or_system_norm)
    + similarity على question_pattern.
    """
    brand_norm = models.CharField(max_length=40, db_index=True,
                                   verbose_name='ماركة (normalized)')
    model_norm = models.CharField(max_length=80, blank=True, db_index=True,
                                   verbose_name='موديل (normalized)')
    year_from = models.PositiveSmallIntegerField(null=True, blank=True)
    year_to = models.PositiveSmallIntegerField(null=True, blank=True)
    mode = models.CharField(max_length=20, choices=RepairMode.choices,
                             db_index=True)
    part_or_system_norm = models.CharField(max_length=160, db_index=True,
                                            verbose_name='القطعة / النظام')

    question_pattern = models.TextField(
        help_text='صياغة نموذجية للسؤال — للبحث الدلالي',
    )
    answer_markdown = models.TextField(verbose_name='الإجابة المعتمدة')

    # المصدر — منين جت المعرفة دي
    oem_source = models.CharField(max_length=200, blank=True,
                                   verbose_name='مصدر OEM',
                                   help_text='Workshop Manual / TSB / Forum thread...')
    image_refs = models.JSONField(default=list, blank=True,
                                   help_text='قائمة روابط/معرّفات صور OEM')

    # Audit
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='+',
    )
    times_served = models.PositiveIntegerField(default=0)
    last_served_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'معرفة معتمدة'
        verbose_name_plural = '✅ المعرفة المعتمدة'
        ordering = ['-times_served', '-updated_at']
        indexes = [
            models.Index(fields=['brand_norm', 'model_norm', 'mode']),
        ]

    def __str__(self) -> str:
        return f'[{self.brand_norm}/{self.model_norm}] {self.part_or_system_norm}'
