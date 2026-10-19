#!/usr/bin/env python3
"""🧪 تقسيم سويت الاختبارات على أكتر من job في CI (shards) بالتساوي تقريباً.

السويت كاملة (~٢٣٠٠ اختبار) على job واحد بتاخد وقت كبير؛ فبنقسّمها على N
jobs بتشتغل مع بعض، كل واحد بقاعدة Postgres وRedis خاصين بيه.

  python scripts/test_shards.py --shards 4 --index 1     # موديولات الـ shard رقم ١
  python scripts/test_shards.py --shards 4 --plan        # خطة التقسيم والأوزان

• الاكتشاف أوتوماتيك: أي ملف test*.py جوه <pkg>/tests/ أو <pkg>/tests.py لأي
  باكدج في جذر المشروع (زي ما `manage.py test` بيلاقيها) بيدخل لوحده —
  مافيش قائمة يدوية تتنسي.
• الوزن تقديري: الكلاس اللي بيعمل شركة (tenant) هو الأغلى (نسخ القالب + بيانات)،
  وبعده كلاسات قاعدة البيانات، والاختبار نفسه رخيص.
• التقسيم ثابت (deterministic): نفس الملفات ⇒ نفس الـ shards في كل تشغيل.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# الأوزان بالثواني (تقريبية — متظبطة على أوقات السويت الفعلية أكتوبر ٢٠٢٦)
TENANT_CLASS_COST = 12.0
DB_CLASS_COST = 1.2
TEST_COST = 0.15

_CLASS_RE = re.compile(r'^class\s+\w+\s*\(([^)]*)\)', re.M)
_TEST_RE = re.compile(r'^\s+def\s+test_', re.M)
_TENANT_HINT = re.compile(r'Tenant|auto_create_schema|tenant_context|schema_context')
_DB_BASES = re.compile(r'\b(TestCase|TransactionTestCase|TenantTestCase|FastTenantTestCase)\b')


def discover():
    """كل موديولات الاختبار كـ labels منقّطة (inventory.tests.test_x)."""
    labels = []
    for app in sorted(p for p in ROOT.iterdir() if (p / '__init__.py').is_file()):
        pkg = app / 'tests'
        if (pkg / '__init__.py').is_file():
            labels += [f'{app.name}.tests.{f.stem}' for f in sorted(pkg.glob('test*.py'))]
        elif (app / 'tests.py').is_file():
            labels.append(f'{app.name}.tests')
    return labels


def weight(label):
    path = ROOT.joinpath(*label.split('.')).with_suffix('.py')
    src = path.read_text(encoding='utf-8')
    tests = len(_TEST_RE.findall(src))
    bases = _CLASS_RE.findall(src)
    uses_tenant = bool(_TENANT_HINT.search(src))
    cost = TEST_COST * tests
    for base in bases:
        if uses_tenant and ('Tenant' in base or 'Base' in base or base.startswith('_')):
            cost += TENANT_CLASS_COST
        elif _DB_BASES.search(base):
            cost += DB_CLASS_COST
    return max(cost, 0.1)


def plan(shards):
    """أكبر موديول الأول على أخف shard (LPT) — توزيع متوازن وثابت."""
    buckets = [[0.0, []] for _ in range(shards)]
    for label, w in sorted(((lbl, weight(lbl)) for lbl in discover()), key=lambda x: (-x[1], x[0])):
        bucket = min(buckets, key=lambda b: b[0])
        bucket[0] += w
        bucket[1].append(label)
    return buckets


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--shards', type=int, required=True)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--index', type=int, help='رقم الـ shard (من ١)')
    group.add_argument('--plan', action='store_true', help='اطبع خطة التقسيم')
    args = ap.parse_args()
    buckets = plan(args.shards)
    if args.plan:
        for i, (total, labels) in enumerate(buckets, 1):
            print(f'shard {i}: ~{total:.0f}s, {len(labels)} modules')
        print(f'total: {sum(len(b[1]) for b in buckets)} modules')
        return 0
    if not 1 <= args.index <= args.shards:
        ap.error('--index لازم يكون بين 1 و --shards')
    print(' '.join(sorted(buckets[args.index - 1][1])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
