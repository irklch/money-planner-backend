#!/usr/bin/env bash
# Резервное копирование и восстановление PostgreSQL для deploy/portable.
#
#   backup.sh once               снять копию сейчас
#   backup.sh loop               копия каждые BACKUP_INTERVAL_HOURS (сервис backup в compose)
#   backup.sh health             0, если последняя успешная копия свежее 2 × интервала
#   backup.sh list               копии в BACKUP_REMOTE
#   backup.sh restore <name|latest>
#                                скачать, проверить, расшифровать и восстановить в пустую БД;
#                                нужен приватный ключ age в /run/age.key (монтирует оператор)
#
# Формат: <BACKUP_REMOTE>/<INSTANCE_NAME>/money-<UTC>.dump.age + .sha256 + .manifest.json.
# Шифрование — age на публичный ключ BACKUP_AGE_RECIPIENT: сервер не может расшифровать свои копии.
# BACKUP_REMOTE — путь rclone; удалённое хранилище описывается переменными RCLONE_CONFIG_<NAME>_*.
set -euo pipefail


: "${BACKUP_INTERVAL_HOURS:=6}"
: "${BACKUP_KEEP_LOCAL_DAYS:=3}"
: "${BACKUP_KEEP_REMOTE_DAYS:=35}"
: "${INSTANCE_NAME:?INSTANCE_NAME is required}"
LOCAL=/backups
STAMP_FILE=$LOCAL/last_success

log() { printf '%s backup: %s\n' "$(date -u +%FT%TZ)" "$*"; }

# Дополнительная проверка, как app/jobs/fence_check.py: копию снимает сервер, на который указывает домен,
# чтобы старая VM после переезда не выкладывала копии устаревшей БД. Не гарантия: при ошибке DNS копия снимается.
fence() {
	[ "${FENCE_MODE:-enforce}" = off ] && return 0
	: "${FENCE_DOMAIN:?}" "${FENCE_EXPECTED_IP:?}"
	ips=$(dig +short +time=3 +tries=2 A "$FENCE_DOMAIN" 2>/dev/null | grep -E '^[0-9.]+$' || true)
	[ -z "$ips" ] && { log "dns_unavailable, continuing"; return 0; }
	echo "$ips" | grep -qx "$FENCE_EXPECTED_IP" && return 0
	log "FENCED: $FENCE_DOMAIN -> $(echo $ips), this server is $FENCE_EXPECTED_IP"
	return 1
}

remote_dir() { echo "${BACKUP_REMOTE:?BACKUP_REMOTE is required}/$INSTANCE_NAME"; }

counts() {
	psql -X -At -v ON_ERROR_STOP=1 -c "
		select json_build_object(
			'alembic', (select version_num from alembic_version),
			'users', (select count(*) from users),
			'refresh_tokens', (select count(*) from refresh_tokens),
			'categories', (select count(*) from categories),
			'expenses', (select count(*) from expenses),
			'free_days', (select count(*) from free_days),
			'expense_imports', (select count(*) from expense_imports))"
}

once() {
	fence || return 0
	: "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT (age public key) is required}"
	ts=$(date -u +%Y%m%dT%H%M%SZ)
	name=money-$ts
	tmp=$LOCAL/.$name
	# Счётчики до дампа: при восстановлении сверяются с фактом (записи после них допустимы только в дампе).
	counts >"$tmp.manifest.json"
	pg_dump --format=custom --no-owner --no-privileges | age -r "$BACKUP_AGE_RECIPIENT" -o "$tmp.dump.age"
	[ -s "$tmp.dump.age" ] || { log "empty dump"; exit 1; }
	(cd $LOCAL && sha256sum ".$name.dump.age" | sed "s/ \.$name/ $name/" >"$tmp.sha256")
	for ext in dump.age sha256 manifest.json; do mv "$tmp.$ext" "$LOCAL/$name.$ext"; done
	if [ -n "${BACKUP_REMOTE:-}" ]; then
		rclone copy --quiet --include "$name.*" "$LOCAL" "$(remote_dir)"
		rclone check --quiet --one-way --include "$name.*" "$LOCAL" "$(remote_dir)"
		rclone delete --quiet --min-age "${BACKUP_KEEP_REMOTE_DAYS}d" "$(remote_dir)"
	else
		log "BACKUP_REMOTE is empty: copy stays on this server only"
	fi
	find $LOCAL -maxdepth 1 -name 'money-*' -mtime +"$BACKUP_KEEP_LOCAL_DAYS" -delete
	date -u +%s >"$STAMP_FILE"
	log "ok $name $(wc -c <"$LOCAL/$name.dump.age") bytes $(cat "$LOCAL/$name.manifest.json")"
}

health() {
	[ -f "$STAMP_FILE" ] || exit 1
	age_s=$(($(date -u +%s) - $(cat "$STAMP_FILE")))
	[ "$age_s" -lt $((BACKUP_INTERVAL_HOURS * 2 * 3600)) ]
}

list() { rclone lsl "$(remote_dir)" --include 'money-*.dump.age' | sort -k2,3; }

restore() {
	key=/run/age.key
	[ -r "$key" ] || { log "mount the age private key to $key"; exit 1; }
	want=${1:?restore <money-YYYYmmddTHHMMSSZ|latest>}
	work=$(mktemp -d)
	trap 'rm -rf "$work"' EXIT
	if [ "$want" = latest ]; then
		want=$(rclone lsf "$(remote_dir)" --include 'money-*.dump.age' | sort | tail -1)
		want=${want%.dump.age}
		[ -n "$want" ] || { log "no backups in $(remote_dir)"; exit 1; }
	fi
	rclone copy --quiet --include "$want.*" "$(remote_dir)" "$work"
	(cd "$work" && sha256sum -c "$want.sha256")
	existing=$(psql -X -At -c "select count(*) from information_schema.tables where table_schema = 'public'")
	[ "$existing" = 0 ] || [ "${RESTORE_FORCE:-}" = yes ] || {
		log "target database is not empty ($existing tables); set RESTORE_FORCE=yes to overwrite"
		exit 1
	}
	age -d -i "$key" "$work/$want.dump.age" |
		pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error -d "$PGDATABASE"
	actual=$(counts)
	log "restored $want"
	log "manifest: $(cat "$work/$want.manifest.json")"
	log "actual:   $actual"
	[ "$actual" = "$(cat "$work/$want.manifest.json")" ] || log "WARNING: counts differ from manifest (rows written during dump?)"
}

cmd=${1:-once}
case "$cmd" in
once) once ;;
loop)
	while true; do
		# Отдельный процесс: внутри `f || …` bash игнорирует set -e.
		"$0" once || log "FAILED"
		sleep $((BACKUP_INTERVAL_HOURS * 3600))
	done
	;;
health) health ;;
list) list ;;
restore) shift; restore "$@" ;;
*) echo "usage: backup.sh once|loop|health|list|restore <name|latest>" >&2; exit 2 ;;
esac
