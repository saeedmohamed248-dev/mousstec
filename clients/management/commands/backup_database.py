"""
🛟 Disaster Recovery — full-database backup via pg_dump.

نظام multi-tenant بيمسك دفاتر مالية وescrow — نسخة احتياطية يومية مش رفاهية.
الأمر ده بيعمل pg_dump للـ database كلها (كل الـ schemas في نفس الـ cluster)
ويرفعها للتخزين المضبوط (S3 لو USE_S3، وإلا مجلد محلي)، ويشيل النسخ الأقدم
من فترة الاحتفاظ.

الاستخدام:
    python manage.py backup_database
    python manage.py backup_database --retention-days 14 --output /var/backups

الاسترجاع (يدوي — عمداً):
    gunzip -c mousstec_backup_YYYYmmdd_HHMMSS.sql.gz | psql "$DATABASE_URL"
"""
from __future__ import annotations

import gzip
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger('mouss_tec_core')


class Command(BaseCommand):
    help = 'نسخة احتياطية كاملة لقاعدة البيانات (pg_dump) + رفعها للتخزين'

    def add_arguments(self, parser):
        parser.add_argument('--retention-days', type=int, default=14,
                            help='عدد الأيام للاحتفاظ بالنسخ المحلية (افتراضي 14)')
        parser.add_argument('--output', default='',
                            help='مجلد الحفظ المحلي (افتراضي BASE_DIR/backups)')

    def handle(self, *args, **opts):
        db = settings.DATABASES['default']
        name = db.get('NAME')
        user = db.get('USER')
        host = db.get('HOST') or 'localhost'
        port = str(db.get('PORT') or '5432')
        password = db.get('PASSWORD') or ''

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        fname = f'mousstec_backup_{ts}.sql.gz'

        out_dir = opts['output'] or os.path.join(str(settings.BASE_DIR), 'backups')
        os.makedirs(out_dir, exist_ok=True)
        local_path = os.path.join(out_dir, fname)

        env = os.environ.copy()
        if password:
            env['PGPASSWORD'] = password

        cmd = ['pg_dump', '-h', host, '-p', port, '-U', user or '', '--no-owner',
               '--no-privileges', '-d', name]

        self.stdout.write(f'⏳ pg_dump {name}@{host}:{port} → {fname}')
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.sql') as tmp:
                proc = subprocess.run(cmd, stdout=tmp, stderr=subprocess.PIPE,
                                      env=env, timeout=1800)
            if proc.returncode != 0:
                err = proc.stderr.decode('utf-8', 'ignore')[:500]
                logger.error('[BACKUP] pg_dump failed: %s', err)
                self.stderr.write(self.style.ERROR(f'❌ pg_dump فشل: {err}'))
                os.unlink(tmp.name)
                return
            # gzip
            with open(tmp.name, 'rb') as raw, gzip.open(local_path, 'wb') as gz:
                gz.writelines(raw)
            os.unlink(tmp.name)
        except FileNotFoundError:
            self.stderr.write(self.style.ERROR(
                '❌ pg_dump مش متثبّت على السيرفر (نصّب postgresql-client).'))
            return
        except subprocess.TimeoutExpired:
            self.stderr.write(self.style.ERROR('❌ pg_dump تجاوز المهلة (30 دقيقة).'))
            return

        size_mb = os.path.getsize(local_path) / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(f'✅ نسخة محلية: {local_path} ({size_mb:.1f} MB)'))

        # رفع للـ S3 لو مفعّل
        if getattr(settings, 'USE_S3', False):
            self._upload_s3(local_path, fname)

        self._prune_old(out_dir, opts['retention_days'])

    def _upload_s3(self, local_path, fname):
        try:
            from django.core.files.storage import default_storage
            with open(local_path, 'rb') as f:
                key = f'backups/{fname}'
                default_storage.save(key, f)
            self.stdout.write(self.style.SUCCESS(f'☁️  رُفعت للتخزين السحابي: {key}'))
        except Exception as exc:
            logger.error('[BACKUP] S3 upload failed: %s', exc)
            self.stderr.write(self.style.WARNING(f'⚠️ فشل الرفع السحابي: {exc}'))

    def _prune_old(self, out_dir, retention_days):
        cutoff = datetime.now() - timedelta(days=retention_days)
        removed = 0
        for f in os.listdir(out_dir):
            if not f.startswith('mousstec_backup_') or not f.endswith('.sql.gz'):
                continue
            path = os.path.join(out_dir, f)
            if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        if removed:
            self.stdout.write(f'🧹 حذف {removed} نسخة أقدم من {retention_days} يوم')
