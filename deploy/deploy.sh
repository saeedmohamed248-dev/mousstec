#!/usr/bin/env bash
# 🚀 نشر من غير ما الموقع يقف (rolling) + رجوع تلقائي لو النسخة الجديدة بايظة.
#
#   bash deploy/deploy.sh              # git pull + بناء + مهاجرات + تبديل نسخ الويب واحدة واحدة
#   bash deploy/deploy.sh --no-pull    # من غير git pull (الكود اللي على السيرفر زي ما هو)
#   bash deploy/deploy.sh --rollback   # ارجع لنسخة الويب اللي قبل آخر نشر
#
# إزاي الموقع مابيقفش:
#   1. الصورة الجديدة بتتبني والنسخ القديمة لسه شغالة وبتخدم الناس.
#   2. المهاجرات بتتعمل مرة واحدة (docker compose run --rm migrate).
#   3. لكل نسخة قديمة: بنشغّل نسخة جديدة جنبها ← نستنى /healthz/ يقول «سليم» ← نقفل القديمة.
#      Caddy بيوزّع على النسخ الشغالة بس، وبيعيد أي طلب على نسخة تانية لو واحدة وقفت.
#   4. لو نسخة جديدة مابقتش سليمة خلال HEALTH_TIMEOUT ثانية ⇒ بتتشال والقديمة بتفضل شغالة،
#      والسكربت بيقف ويقولك (والموقع شغال بالنسخة القديمة).
set -euo pipefail

cd "${PROJECT_DIR:-$(dirname "$0")/..}"
ENV_FILE="${ENV_FILE:-.env}"
DC=(docker compose --env-file "$ENV_FILE")
IMAGE="${WEB_IMAGE:-mousstec-web}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"
REPLICAS="${WEB_REPLICAS:-$( (grep -E '^WEB_REPLICAS=' "$ENV_FILE" 2>/dev/null || true) | tail -1 | cut -d= -f2)}"
REPLICAS="${REPLICAS:-2}"

log()  { echo "$(date '+%H:%M:%S') $*"; }
fail() { echo "$(date '+%H:%M:%S') ❌ $*" >&2; exit 1; }

health_of() {
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null || echo gone
}

wait_healthy() {   # $1 = container id
  local waited=0 s
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    s="$(health_of "$1")"
    case "$s" in
      healthy) return 0 ;;
      unhealthy|exited|dead|gone) return 1 ;;
    esac
    sleep 5; waited=$((waited + 5))
  done
  return 1
}

web_ids() { "${DC[@]}" ps -q web 2>/dev/null | sort; }

# نسخة ويب جديدة جنب الموجودين (بالصورة الحالية لـ $IMAGE:latest) ⇒ يطبع الـ id بتاعها
add_web_replica() {
  local before after count new
  before="$(web_ids)"
  count=$(( $(printf '%s\n' "$before" | grep -c . || true) + 1 ))
  "${DC[@]}" up -d --no-deps --no-recreate --scale web="$count" web >&2
  after="$(web_ids)"
  new="$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | head -1)"
  [ -n "$new" ] || fail "مقدرتش أشغّل نسخة ويب جديدة."
  echo "$new"
}

remove_container() {   # قفل هادي: Caddy بيلاحظ في ثواني ويعيد أي طلب على نسخة تانية
  docker stop -t 30 "$1" >/dev/null && docker rm "$1" >/dev/null
}

rolling_replace_web() {
  local old new
  old="$(web_ids)"
  for o in $old; do
    log "🟡 نسخة جديدة جنب $(docker inspect -f '{{.Name}}' "$o" | tr -d /)…"
    new="$(add_web_replica)"
    if ! wait_healthy "$new"; then
      log "🧯 النسخة الجديدة مابقتش سليمة — آخر سطور من اللوج:"
      docker logs --tail 40 "$new" 2>&1 | sed 's/^/    /' >&2 || true
      remove_container "$new" || true
      if docker image inspect "$IMAGE:previous" >/dev/null 2>&1; then
        docker image tag "$IMAGE:previous" "$IMAGE:latest"
      fi
      fail "النشر اتلغى — الموقع لسه شغال بالنسخة القديمة. صلّح المشكلة وشغّل السكربت تاني."
    fi
    log "🟢 سليمة. بنقفل القديمة…"
    sleep 6   # Caddy بيحدّث قايمة النسخ كل ٥ ثواني
    remove_container "$o"
  done
  # العدد المطلوب (مثلاً أول مرة من نسخة واحدة لنسختين)
  while [ "$(web_ids | grep -c . || true)" -lt "$REPLICAS" ]; do
    log "➕ نسخة ويب إضافية…"
    new="$(add_web_replica)"
    wait_healthy "$new" || fail "النسخة الإضافية مابقتش سليمة (الباقيين شغالين)."
  done
  while [ "$(web_ids | grep -c . || true)" -gt "$REPLICAS" ]; do
    remove_container "$(web_ids | head -1)"
  done
}

reload_caddy() {   # reload بيفحص الملف الأول — لو فيه غلط Caddy بيفضل بالإعدادات القديمة
  if [ -n "$("${DC[@]}" ps -q caddy 2>/dev/null)" ]; then
    "${DC[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile \
      && log "🔁 Caddy اتحدّث." || log "⚠️ Caddyfile فيه مشكلة — Caddy مكمّل بالإعدادات القديمة."
  else
    "${DC[@]}" up -d --no-deps caddy
  fi
}

if [ "${1:-}" = "--rollback" ]; then
  docker image inspect "$IMAGE:previous" >/dev/null 2>&1 || fail "مفيش نسخة سابقة محفوظة ($IMAGE:previous)."
  log "⏪ رجوع لنسخة الويب السابقة…"
  docker image tag "$IMAGE:previous" "$IMAGE:latest"
  rolling_replace_web
  log "✅ الويب رجع للنسخة السابقة. (المهاجرات اللي اتعملت مابترجعش — لو محتاج تلغيها كلّم المطوّر.)"
  exit 0
fi

[ "${1:-}" = "--no-pull" ] || { log "⬇️ git pull…"; git pull --ff-only; }

# قاعدة البيانات/Redis: لو إعداداتهم اتغيّرت بس (مرة واحدة) — من غير كده مفيش حاجة بتحصل
"${DC[@]}" up -d --no-deps --wait db redis

if docker image inspect "$IMAGE:latest" >/dev/null 2>&1; then
  docker image tag "$IMAGE:latest" "$IMAGE:previous"
  log "💾 النسخة الحالية محفوظة للرجوع ($IMAGE:previous)."
fi

log "🔨 بناء الصورة الجديدة (الموقع شغال عادي)…"
"${DC[@]}" build web worker beat

log "🔄 مهاجرات + ستاتيك…"
"${DC[@]}" run --rm migrate || fail "المهاجرات فشلت — مافيش ولا نسخة ويب اتغيّرت والموقع شغال زي ما هو."

rolling_replace_web
log "⚙️ الخدمات الخلفية (worker/beat/autoheal)…"
"${DC[@]}" up -d --no-deps worker beat autoheal
reload_caddy

log "✅ النشر خلص — $(web_ids | grep -c .) نسخ ويب شغالين وسليمين."
"${DC[@]}" ps
