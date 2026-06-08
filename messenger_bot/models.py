from django.db import models


class SystemUpdate(models.Model):
    """
    Knowledge-base entry surfaced to the Messenger bot. A post_save signal
    re-vectorises this row whenever it changes, so the RAG layer is always
    consistent with what's stored here.
    """

    KIND_CHOICES = [
        ("update", "Product Update"),
        ("faq", "FAQ"),
        ("pricing", "Pricing Plan"),
        ("docs", "Documentation"),
        ("announcement", "Announcement"),
    ]

    title = models.CharField(max_length=255)
    content = models.TextField()
    kind = models.CharField(max_length=32, choices=KIND_CHOICES, default="update")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"[{self.kind}] {self.title}"

    @property
    def embedding_text(self) -> str:
        return f"{self.title}\n\n{self.content}".strip()


class ConversationLog(models.Model):
    """Lightweight audit trail — useful for tuning RAG and debugging Meta delivery."""

    sender_id = models.CharField(max_length=64, db_index=True)
    user_message = models.TextField()
    bot_response = models.TextField(blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
