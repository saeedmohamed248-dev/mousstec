# 🛠️ أوامر الشغل اليومي — `make help` يعرضها كلها.
# الأوامر بتفترض venv متفعّل (أو PYTHON=/path/to/python) و Postgres + Redis شغالين.
# المتغيرات من .env (manage.py بيقراه) — شوف README.md «التشغيل محلياً».

PYTHON ?= python
MANAGE := $(PYTHON) manage.py
SHARDS ?= 4
APP ?= inventory
E2E_PASSWORD ?= Demo12345

.DEFAULT_GOAL := help
.PHONY: help setup services migrate seed run test test-app test-shard lint check css css-check e2e-server e2e messages

help:  ## الأوامر المتاحة
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36mmake %-12s\033[0m %s\n", $$1, $$2}'

setup:  ## تثبيت الاعتماديات (بايثون + Playwright)
	$(PYTHON) -m pip install -r requirements.txt
	cd e2e && npm ci && npx playwright install chromium

services:  ## تشغيل Postgres و Redis (Linux/devcontainer)
	service postgresql start
	redis-server --daemonize yes

migrate:  ## المهاجرات (public + كل الشركات) وتحديث قالب الشركات الجديدة
	$(MANAGE) migrate_schemas --shared
	$(MANAGE) migrate_schemas --tenant
	$(MANAGE) refresh_tenant_template

seed:  ## شركة تجريبية ببيانات: http://demo.localhost:8000 (admin / cashier / tech / accountant)
	$(MANAGE) seed_demo --sub demo --password $(E2E_PASSWORD)

run:  ## سيرفر التطوير على :8000
	$(MANAGE) runserver 0.0.0.0:8000

test:  ## السويت كاملة (بطيئة — CI بيقسّمها على 4 أجهزة)
	$(MANAGE) test --noinput

test-app:  ## اختبارات تطبيق واحد: make test-app APP=hr
	$(MANAGE) test $(APP) --noinput

test-shard:  ## shard واحد زي CI: make test-shard SHARD=2
	$(MANAGE) test $$($(PYTHON) scripts/test_shards.py --shards $(SHARDS) --index $(SHARD)) --noinput

lint:  ## سينتاكس + لينت القوالب + ruff (نفس CI)
	$(PYTHON) -m compileall -q -x '(^|/)(node_modules|staticfiles|venv)/' .
	$(PYTHON) scripts/lint_templates.py .
	ruff check --select F821,F822,F823,F501,F502,F503,F504,F505,F506,F507,F508,F509,F521,F522,F523,F524,F525,F601,F602,F631,F632,F633,B002,B006,B011,B014,PLE .

check:  ## فحوصات Django: مهاجرات ناقصة + القوالب الحرجة
	$(MANAGE) makemigrations --check --dry-run
	$(MANAGE) check_templates

css:  ## بناء CSS بتاع Tailwind بعد ما تضيف classes في القوالب (static/vendor/css/tailwind.css)
	scripts/build_css.sh

css-check:  ## الـ CSS متحدّث مع القوالب؟ (نفس CI)
	scripts/build_css.sh --check

e2e-server:  ## سيرفر اختبارات المتصفح (زي الإنتاج) على :8000
	E2E_PASSWORD=$(E2E_PASSWORD) scripts/e2e_server.sh

e2e:  ## اختبارات المتصفح عربي + إنجليزي (السيرفر لازم يكون شغال)
	cd e2e && E2E_PASSWORD=$(E2E_PASSWORD) npx playwright test

messages:  ## تجميع ملفات الترجمة (.po ⇒ .mo) بعد إضافة نصوص
	msgfmt --check -o locale/en/LC_MESSAGES/django.mo locale/en/LC_MESSAGES/django.po
	msgfmt --check -o locale/ar/LC_MESSAGES/django.mo locale/ar/LC_MESSAGES/django.po
