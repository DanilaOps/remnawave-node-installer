#!/usr/bin/env bash
#
# install-mirror.sh — nginx SNI-mirror для каскада Remnawave/Reality (две ноды).
#
# Зеркало (этот сервер) слушает :443, читает SNI из ClientHello через ssl_preread
# (TLS НЕ терминируется) и по SNI решает, куда отдать сырой TCP:
#   * СВОЙ секретный SNI → бэкенд-нода :443 (там ваш дефолтный Reality-конфиг);
#   * НЕИЗВЕСТНЫЙ/ПУСТОЙ SNI → reset на закрытый локальный порт (БЕЗОПАСНО по умолч.);
#   * прозрачный форвард чужого SNI на реально запрошенный хост (ОТКРЫТЫЙ РЕЛЕЙ) —
#     ТОЛЬКО по явному согласию (--allow-open-relay) и с egress-защитой от приватных
#     сетей; без неё функция не включается.
#
# Reality-ключи и SNI принадлежат БЭКЕНДУ; клиентам/в панель даётся адрес ЗЕРКАЛА.
# Сквозное шифрование остаётся у клиента и бэкенда — зеркало ключей не видит.
#
# Поддержка: Debian и Ubuntu (apt; протестированный основной путь) и Rocky/RHEL-
# подобные (dnf/microdnf). Root, tty.
# Конфиг nginx НЕ перезаписывается целиком: ставится отдельный stream-файл и
# подключается одной строкой include; применение транзакционное (validate → reload
# → откат на прошлую рабочую версию при ошибке).

set -Eeuo pipefail

# ── Константы (пути переопределяемы env-переменными — нужно для QA-харнесса,
#    который прогоняет функции против временного каталога, не трогая реальный
#    /etc/nginx, firewall и systemd) ──────────────────────────────────────────
LOG_FILE="${MIRROR_LOG_FILE:-install-mirror.log}"
NGINX_CONF="${MIRROR_NGINX_CONF:-/etc/nginx/nginx.conf}"
STREAM_DIR="${MIRROR_STREAM_DIR:-/etc/nginx/stream-enabled}"
STREAM_CONF="${STREAM_DIR}/remnawave-mirror.conf"
INCLUDE_LINE="include ${STREAM_DIR}/*.conf;"
INCLUDE_MARK="# remnawave SNI-mirror (managed by install-mirror.sh)"
BACKUP_DIR="${MIRROR_BACKUP_DIR:-/var/backups/remnawave-mirror}"
STREAM_LOG="${MIRROR_STREAM_LOG:-/var/log/nginx/remnawave-mirror.log}"
EGRESS_NFT="${MIRROR_EGRESS_NFT:-/etc/mirror-egress.nft}"
EGRESS_UNIT="${MIRROR_EGRESS_UNIT:-/etc/systemd/system/mirror-egress.service}"
PERIP_CONN_LIMIT="256"   # generous per-source cap: anti-exhaustion, НЕ rate-limit (NAT-safe)

# ── Globals, заполняются в ходе установки ────────────────────────────────────
ip1=""; ip2=""; backend_in=""; backend_host=""; backend_ip=""; sni=""
public_addr=""; nginx_user=""; pkg_mgr=""
RELAY_ENABLED=0
declare -a REFRESH_CMD=() INSTALL_CMD=()
declare -a PKGS=()

# ── Логирование + tty ────────────────────────────────────────────────────────
# MIRROR_LIB=1 — библиотечный режим для QA: только определить функции, без
# побочных эффектов (лог-редирект, проверки tty/root) и без запуска меню.
TTY=/dev/tty
if [[ "${MIRROR_LIB:-0}" != "1" ]]; then
    # Логируем весь вывод в файл, НЕ занимая stdin: process-substitution оставляет
    # tty свободным для read (скрипт переживает запуск и через пайп-обёртку).
    exec > >(tee "$LOG_FILE") 2>&1
    if [[ ! -e "$TTY" ]]; then
        echo "Ошибка: нет доступа к терминалу (/dev/tty) для интерактивного ввода."
        echo "  Запускай скрипт напрямую (bash install-mirror.sh), не через пайп."
        exit 1
    fi
    if [[ "$EUID" -ne 0 ]]; then
        echo "Ошибка: Скрипт должен запускаться от имени root или с sudo."
        exit 1
    fi
fi

# ── Вывод ────────────────────────────────────────────────────────────────────
c_red() { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn() { printf '\033[32m%s\033[0m\n' "$*"; }
c_ylw() { printf '\033[33m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }
ok()    { c_grn "  OK: $*"; }
warn()  { c_ylw "  ВНИМАНИЕ: $*"; }
die()   { c_red "Ошибка: $*"; exit 1; }

# ── Временные файлы: чистятся trap-ом ────────────────────────────────────────
TMP_ERR="$(mktemp)"
declare -a CLEANUP_FILES=("$TMP_ERR")
cleanup() { local f; for f in "${CLEANUP_FILES[@]}"; do [[ -n "$f" ]] && rm -f "$f"; done; }
# В lib-режиме trap не ставим, чтобы не трогать окружение тест-харнесса.
[[ "${MIRROR_LIB:-0}" != "1" ]] && trap cleanup EXIT INT TERM

# ── Ввод ─────────────────────────────────────────────────────────────────────
# trim <str> — срезать ведущие/хвостовые пробелы.
trim() {
    local v="$1"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    printf '%s' "$v"
}
# ask <prompt> — печатает приглашение на tty, тримленный ответ в REPLY_VALUE.
ask() {
    printf '%s' "$1" > "$TTY"
    IFS= read -r REPLY_VALUE < "$TTY"
    REPLY_VALUE="$(trim "$REPLY_VALUE")"
}

# ── Валидация ────────────────────────────────────────────────────────────────
is_ipv4() {
    local ip="$1" o
    [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    for o in "${BASH_REMATCH[@]:1}"; do (( 10#$o <= 255 )) || return 1; done
    return 0
}
# IPv6 (в т.ч. bracketed) — только чтобы ОТКЛОНИТЬ его понятным сообщением:
# полноценной IPv6-поддержки (src_toward, форматирование, firewall) тут нет.
looks_ipv6() {
    local s="$1"; s="${s#[}"; s="${s%]}"
    [[ "$s" == *:* && "$s" =~ ^[0-9A-Fa-f:.]+$ ]]
}
# Приватные/служебные IPv4: RFC1918, loopback, link-local(+metadata 169.254.169.254),
# CGNAT, multicast/reserved, this-net, documentation/test, benchmarking.
is_private_ipv4() {
    local ip="$1" a b c _
    is_ipv4 "$ip" || return 1
    IFS=. read -r a b c _ <<< "$ip"
    (( a == 0   )) && return 0
    (( a == 10  )) && return 0
    (( a == 127 )) && return 0
    (( a == 169 && b == 254 )) && return 0
    (( a == 172 && b >= 16 && b <= 31 )) && return 0
    (( a == 192 && b == 168 )) && return 0
    (( a == 100 && b >= 64 && b <= 127 )) && return 0
    (( a >= 224 )) && return 0
    (( a == 192 && b == 0 && c == 2 )) && return 0
    (( a == 198 && b == 51 && c == 100 )) && return 0
    (( a == 203 && b == 0 && c == 113 )) && return 0
    (( a == 198 && (b == 18 || b == 19) )) && return 0
    return 1
}
valid_hostname() {
    local h="$1"
    (( ${#h} <= 253 )) || return 1
    [[ "$h" == *.* ]] || return 1   # требуем хотя бы одну точку (реальный домен)
    [[ "$h" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$ ]]
}
# SNI — только DNS-hostname: без IP, порта, пробелов, управляющих символов и
# nginx/shell-конструкций (; { } $). valid_hostname уже ограничивает алфавит
# [A-Za-z0-9.-], что само по себе отсекает инъекции; проверка ниже — явный барьер.
valid_sni() {
    local s="$1"
    [[ -n "$s" ]] || return 1
    [[ "$s" =~ [[:cntrl:]] ]] && return 1
    case "$s" in *[' ;{}$']*|*'"'*|*"'"*) return 1 ;; esac
    is_ipv4 "$s" && return 1
    looks_ipv6 "$s" && return 1
    valid_hostname "$s"
}
is_local_ipv4() { ip -4 -o addr show 2>/dev/null | grep -qw "$1"; }

# ── Определение исходящего адреса (IPv4) ─────────────────────────────────────
# src_toward <host> — локальный source-IP, с которого сервер пойдёт на <host>.
# ЭТО НЕ публичный адрес за NAT — только для firewall-правила на бэкенде.
src_toward() {
    local host="$1" ip
    ip="$(getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1{print $1}')"
    [[ -z "$ip" ]] && ip="$host"
    is_ipv4 "$ip" || return 0
    ip -4 route get "$ip" 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}'
}
# Внешнее определение публичного IP: несколько источников, timeout. Пусто = не смогли.
detect_external_ip() {
    local u ip
    for u in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
        command -v curl >/dev/null 2>&1 || return 0
        ip="$(curl -fsS --connect-timeout 5 --max-time 8 "$u" 2>/dev/null | tr -d '[:space:]')"
        if is_ipv4 "$ip" && ! is_private_ipv4 "$ip"; then printf '%s' "$ip"; return 0; fi
    done
    return 0
}

# ── Сбор и валидация входных данных ──────────────────────────────────────────
collect_inputs() {
    echo ""
    while :; do
        ask "IP1 — входящий адрес (Enter = 0.0.0.0, слушать все IPv4): "
        ip1="${REPLY_VALUE:-0.0.0.0}"
        [[ "$ip1" == "0.0.0.0" ]] && break
        looks_ipv6 "$ip1" && { warn "IPv6 не поддерживается — укажите IPv4 или 0.0.0.0."; continue; }
        is_ipv4 "$ip1" || { warn "Не IPv4."; continue; }
        is_local_ipv4 "$ip1" || { warn "Адрес $ip1 не найден на интерфейсах — nginx не сможет его слушать."; continue; }
        break
    done

    while :; do
        ask "IP2 — исходящий адрес к бэкенду (Enter = авто/как IP1): "
        ip2="$REPLY_VALUE"
        [[ -z "$ip2" ]] && { ip2=""; break; }   # пусто = не задавать proxy_bind
        looks_ipv6 "$ip2" && { warn "IPv6 не поддерживается — укажите IPv4 или оставьте пустым."; continue; }
        is_ipv4 "$ip2" || { warn "Не IPv4."; continue; }
        is_local_ipv4 "$ip2" || { warn "Адрес $ip2 не найден на интерфейсах — нельзя использовать как source."; continue; }
        break
    done

    while :; do
        ask "Адрес БЭКЕНД-ноды (IPv4 или hostname): "
        backend_in="$REPLY_VALUE"
        [[ -z "$backend_in" ]] && { warn "Не может быть пустым."; continue; }
        # нормализуем host[:port] → host (бэкенд всегда достигается по :443)
        if [[ "$backend_in" =~ ^([^:]+):([0-9]+)$ ]]; then
            [[ "${BASH_REMATCH[2]}" != "443" ]] && info "Порт бэкенда всегда 443 — ${BASH_REMATCH[2]} проигнорирован."
            backend_in="${BASH_REMATCH[1]}"
        fi
        looks_ipv6 "$backend_in" && { warn "IPv6-бэкенд не поддерживается — укажите IPv4 или hostname."; continue; }
        if is_ipv4 "$backend_in"; then
            is_private_ipv4 "$backend_in" && { warn "Приватный/служебный адрес бэкенда запрещён."; continue; }
            backend_host="$backend_in"; backend_ip="$backend_in"; break
        elif valid_hostname "$backend_in"; then
            # Резолвим ОДИН раз на установке и запекаем IP (без runtime-resolver и
            # без DNS-rebinding). Отклоняем, если домен ведёт в приватную сеть.
            backend_ip="$(getent ahostsv4 "$backend_in" 2>/dev/null | awk 'NR==1{print $1}')"
            [[ -z "$backend_ip" ]] && { warn "Не удалось разрешить $backend_in в IPv4."; continue; }
            is_private_ipv4 "$backend_ip" && { warn "$backend_in → $backend_ip (приватный) — запрещено."; continue; }
            backend_host="$backend_in"
            info "$backend_in → $backend_ip"
            break
        else
            warn "Не похоже на IPv4 или корректный hostname."
        fi
    done

    while :; do
        if is_ipv4 "$backend_in"; then
            # Бэкенд задан IP → SNI по умолчанию из IP брать нельзя, спрашиваем домен.
            ask "Домен Reality (SNI, serverNames на бэкенде) — обязателен при IP-бэкенде: "
            sni="$REPLY_VALUE"
        else
            ask "Домен Reality (SNI), Enter = совпадает с адресом бэкенда (${backend_host}): "
            sni="${REPLY_VALUE:-$backend_host}"
        fi
        valid_sni "$sni" && break
        warn "SNI должен быть DNS-именем (без IP, порта, пробелов и символов ; { } \$ \" ')."
    done

    determine_public_addr
    decide_relay
}

# Публичный адрес зеркала (для панели) — ОТДЕЛЬНО от bind-адреса (ip1).
determine_public_addr() {
    if [[ "$ip1" != "0.0.0.0" ]] && is_ipv4 "$ip1" && ! is_private_ipv4 "$ip1"; then
        public_addr="$ip1"; return
    fi
    echo ""
    info "IP1 (${ip1}) не является публичным адресом (bind ≠ публичный за NAT)."
    ask "Публичный IP/домен ЗЕРКАЛА для панели (Enter — попробовать определить внешне): "
    local cand="$REPLY_VALUE"
    if [[ -z "$cand" ]]; then
        cand="$(detect_external_ip)"
        if [[ -n "$cand" ]]; then
            ask "Определён внешний IP ${cand}. Использовать? [y/N]: "
            case "$REPLY_VALUE" in [yYдД]|[yY][eE][sS]|да) ;; *) cand="" ;; esac
        else
            warn "Внешний IP определить не удалось."
        fi
    fi
    if [[ -z "$cand" ]]; then
        public_addr="<ПУБЛИЧНЫЙ_IP_ЗЕРКАЛА>"
        warn "Публичный адрес не задан — в инструкции будет плейсхолдер, впишите вручную."
    elif is_ipv4 "$cand"; then
        is_private_ipv4 "$cand" && warn "Указан приватный адрес — в панель как публичный он не годится."
        public_addr="$cand"
    elif valid_hostname "$cand"; then
        public_addr="$cand"
    else
        warn "Не похоже на IPv4/домен — записываю как есть, проверьте."
        public_addr="$cand"
    fi
}

# Открытый релей: по умолчанию ВЫКЛ. Включается только явно и только если можем
# поставить egress-защиту (nft) от приватных/служебных сетей и cloud-metadata.
decide_relay() {
    RELAY_ENABLED=0
    echo ""
    info "Политика для НЕИЗВЕСТНОГО SNI по умолчанию: reset (безопасно)."
    ask "Включить прозрачный форвард чужого SNI (ОТКРЫТЫЙ TLS-РЕЛЕЙ)? [y/N]: "
    case "$REPLY_VALUE" in [yYдД]|[yY][eE][sS]|да) ;; *) info "Релей выключен — оставляю reset."; return ;; esac

    if ! command -v nft >/dev/null 2>&1; then
        warn "nft не найден — egress-защиту от приватных сетей поставить нельзя."
        warn "Открытый релей БЕЗ egress-фильтра небезопасен (SSRF в 10.0.0.0/8, 169.254.169.254 и т.п.)."
        info "Релей НЕ включён. Поставьте nftables или оставьте reset."
        return
    fi
    echo ""
    c_ylw "  ┌─ ПРЕДУПРЕЖДЕНИЕ ────────────────────────────────────────────────┐"
    c_ylw "  │ Открытый TLS-релей форвардит ЛЮБОЙ чужой SNI на запрошенный хост.│"
    c_ylw "  │ Ваш сервер станет транзитом к произвольным TLS-хостам, исходящий │"
    c_ylw "  │ адрес — этого сервера. Возможны абьюз и жалобы хостеру.          │"
    c_ylw "  │ Будет установлен egress-фильтр nft: трафик nginx в приватные/    │"
    c_ylw "  │ служебные/metadata-сети блокируется (защита от SSRF/rebinding).  │"
    c_ylw "  └─────────────────────────────────────────────────────────────────┘"
    ask "  Осознаю риск, включить релей с egress-фильтром? Введите 'YES': "
    if [[ "$REPLY_VALUE" == "YES" ]]; then
        RELAY_ENABLED=1
        info "Релей будет включён с egress-защитой."
    else
        info "Не подтверждено — релей выключен, остаётся reset."
    fi
}

# ── Пакеты / stream-модуль ───────────────────────────────────────────────────
detect_pkg_mgr() {
    if command -v apt-get >/dev/null 2>&1; then
        pkg_mgr="apt"; nginx_user="www-data"
        PKGS=(nginx libnginx-mod-stream)
        REFRESH_CMD=(apt-get update -y)
        INSTALL_CMD=(env DEBIAN_FRONTEND=noninteractive apt-get install -y)
    elif command -v microdnf >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
        if command -v microdnf >/dev/null 2>&1; then pkg_mgr="microdnf"; else pkg_mgr="dnf"; fi
        nginx_user="nginx"
        PKGS=(nginx nginx-mod-stream)
        REFRESH_CMD=("$pkg_mgr" makecache)   # НЕ 'update' (обновил бы всю систему)
        INSTALL_CMD=("$pkg_mgr" install -y)
    else
        die "Не найден подходящий менеджер пакетов (apt/dnf/microdnf)."
    fi
}

install_packages() {
    step "Установка nginx + stream-модуля ($pkg_mgr)"
    if ! "${REFRESH_CMD[@]}"; then
        warn "Обновление индекса пакетов не удалось — продолжаю, install может подтянуть метаданные."
    fi
    "${INSTALL_CMD[@]}" "${PKGS[@]}" || die "Не удалось установить пакеты: ${PKGS[*]}"
    command -v nginx >/dev/null 2>&1 || die "nginx не установился."
}

# ── Транзакционное применение конфигурации ───────────────────────────────────
nginx_running() { systemctl is-active --quiet nginx 2>/dev/null || pgrep -x nginx >/dev/null 2>&1; }

# Убрать наш include-блок из nginx.conf (идемпотентно).
remove_include() {
    local tmp; tmp="$(mktemp)"; CLEANUP_FILES+=("$tmp")
    grep -vF -e "$INCLUDE_LINE" -e "$INCLUDE_MARK" "$NGINX_CONF" > "$tmp" && cat "$tmp" > "$NGINX_CONF"
}

# Записать наш include в nginx.conf на верхнем уровне (идемпотентно). Возвращает
# 0 если строку добавили в этот раз, 1 если уже была.
ensure_include() {
    if grep -qF "$INCLUDE_LINE" "$NGINX_CONF"; then return 1; fi
    # Отказ, если уже есть ЧУЖОЙ top-level stream{} — двух быть не должно.
    if awk 'BEGIN{d=0} /\{/{d++} /\}/{d--} /^[[:space:]]*stream[[:space:]]*\{/{ if(d==0) f=1 } END{exit !f}' "$NGINX_CONF"; then
        die "В $NGINX_CONF уже есть top-level stream{}. Второй добавлять нельзя — интегрируйте вручную."
    fi
    printf '\n%s\n%s\n' "$INCLUDE_MARK" "$INCLUDE_LINE" >> "$NGINX_CONF"
    return 0
}

# Сгенерировать stream-конфиг во временный файл (аргумент — путь).
generate_stream_conf() {
    local out="$1"
    local bind="$ip1"
    local proxy_bind_line=""
    if [[ -n "$ip2" && "$ip2" != "0.0.0.0" ]]; then
        proxy_bind_line="        proxy_bind ${ip2};"
    fi

    local default_target resolver_line=""
    if (( RELAY_ENABLED )); then
        # Релей: чужой SNI → реально запрошенный хост. Нужен resolver (динамическое
        # имя). Защита от приватных ответов/rebinding — на уровне egress-nft, не DNS.
        default_target='$ssl_preread_server_name:443'
        resolver_line=$'    resolver 1.1.1.1 8.8.8.8 valid=60s ipv6=off;\n    resolver_timeout 5s;'
    else
        default_target='127.0.0.1:1'   # reset: закрытый локальный порт → RST
    fi

    {
        echo "# remnawave SNI-mirror — генерируется install-mirror.sh. Правки перезапишутся."
        echo "stream {"
        echo "    # Небольшой security/abuse-лог вместо полного access_log off."
        echo "    # Ротация: пропишите $STREAM_LOG в /etc/logrotate.d/nginx (nginx его ротирует)."
        echo '    log_format  mirror  '"'"'$remote_addr [$time_local] sni=$ssl_preread_server_name -> $upstream_addr $status $bytes_sent'"'"';'
        echo "    access_log  ${STREAM_LOG}  mirror;"
        echo ""
        echo "    # Анти-исчерпание: щедрый лимит соединений на один source-IP (НЕ rate-limit,"
        echo "    # чтобы не бить пользователей за общим NAT). Общий потолок задаёт"
        echo "    # worker_connections в основном nginx.conf (events{})."
        echo "    limit_conn_zone \$binary_remote_addr zone=mirror_perip:10m;"
        [[ -n "$resolver_line" ]] && printf '%s\n' "$resolver_line"
        echo ""
        echo "    # Маршрутизация по SNI. Порядок nft/map: точные ключи → регэксп → default."
        echo "    map \$ssl_preread_server_name \$target_upstream {"
        echo "        ~^\$        127.0.0.1:1;      # пустой SNI → reset"
        echo "        ${sni}        ${backend_ip}:443;   # секретный SNI → бэкенд (${backend_host})"
        echo "        default    ${default_target};   # неизвестный SNI"
        echo "    }"
        echo ""
        echo "    server {"
        echo "        listen ${bind}:443;"
        echo "        ssl_preread on;"
        [[ -n "$proxy_bind_line" ]] && printf '%s\n' "$proxy_bind_line"
        echo "        proxy_pass \$target_upstream;"
        echo "        limit_conn mirror_perip ${PERIP_CONN_LIMIT};"
        echo "        # Idle-timeout щедрый: долгие Reality/XHTTP не рвём на паузах в трафике."
        echo "        proxy_timeout 300s;"
        echo "        proxy_connect_timeout 5s;"
        echo "    }"
        echo "}"
    } > "$out"
}

# Транзакция: backup → install → nginx -t → reload/rollback.
apply_nginx_config() {
    step "Применение конфигурации (транзакционно)"
    mkdir -p "$STREAM_DIR" "$BACKUP_DIR" "$(dirname "$STREAM_LOG")"

    # 1) backup ТЕКУЩЕГО рабочего состояния (не древнего) с таймстампом.
    local ts bkp; ts="$(date +%Y%m%d-%H%M%S)"; bkp="${BACKUP_DIR}/${ts}"
    mkdir -p "$bkp"
    cp -a "$NGINX_CONF" "${bkp}/nginx.conf"
    local had_stream=0
    if [[ -f "$STREAM_CONF" ]]; then cp -a "$STREAM_CONF" "${bkp}/remnawave-mirror.conf"; had_stream=1; fi
    info "Backup рабочей версии: $bkp"

    # 2) сгенерировать новый stream-конфиг во временный файл.
    local newconf; newconf="$(mktemp)"; CLEANUP_FILES+=("$newconf")
    generate_stream_conf "$newconf"

    # 3) подключить include (идемпотентно) и разложить новый файл, сохранив прошлый.
    local added_include=0
    ensure_include && added_include=1
    local prev=""
    if [[ "$had_stream" == 1 ]]; then prev="${STREAM_CONF}.prev"; cp -a "$STREAM_CONF" "$prev"; CLEANUP_FILES+=("$prev"); fi
    install -m 0644 "$newconf" "$STREAM_CONF"

    # 4) валидировать РЕАЛЬНЫЙ результирующий конфиг (модуль stream + наш блок).
    #    nginx -t не трогает работающий процесс, поэтому старый инстанс продолжает
    #    обслуживать клиентов, пока мы проверяем.
    if nginx -t 2>"$TMP_ERR"; then
        ok "nginx -t прошёл."
        # 5) применить: reload если работает, иначе start.
        if nginx_running; then
            if nginx -s reload 2>>"$TMP_ERR"; then
                ok "nginx reload — конфиг применён."
            else
                rollback_config "$added_include" "$prev" "$had_stream"
                die "reload не удался; восстановил прошлую рабочую версию. Детали:"$'\n'"$(cat "$TMP_ERR")"
            fi
        else
            systemctl enable nginx >/dev/null 2>&1 || warn "systemctl enable nginx не сработал."
            if systemctl start nginx 2>>"$TMP_ERR"; then
                ok "nginx запущен."
            else
                rollback_config "$added_include" "$prev" "$had_stream"
                die "start не удался; восстановил прошлую версию. Детали:"$'\n'"$(cat "$TMP_ERR")"
            fi
        fi
        [[ -n "$prev" ]] && rm -f "$prev"
    else
        rollback_config "$added_include" "$prev" "$had_stream"
        die "nginx -t не прошёл на новом конфиге; рабочий конфиг не изменён. Детали:"$'\n'"$(cat "$TMP_ERR")"
    fi
}

# Откат к ПРОШЛОЙ рабочей версии + гарантия, что nginx запущен.
rollback_config() {
    local added_include="$1" prev="$2" had_stream="$3"
    warn "Откат конфигурации…"
    if [[ "$had_stream" == 1 && -n "$prev" && -f "$prev" ]]; then
        install -m 0644 "$prev" "$STREAM_CONF"
    else
        rm -f "$STREAM_CONF"
    fi
    [[ "$added_include" == 1 ]] && remove_include
    # Убедиться, что nginx не остался лежать. Это уже путь восстановления, поэтому
    # неудачу reload/start НЕ глотаем молча, а сообщаем оператору с точной командой.
    if nginx -t >/dev/null 2>&1; then
        if nginx_running; then
            nginx -s reload >/dev/null 2>&1 || warn "Восстановленный конфиг не перечитался — выполните: nginx -s reload"
        else
            systemctl start nginx >/dev/null 2>&1 || warn "nginx не поднялся после отката — выполните: systemctl start nginx"
        fi
    else
        warn "Базовый конфиг тоже не проходит nginx -t — проверьте $NGINX_CONF вручную."
    fi
}

# ── Egress-защита для релея (nft, только при RELAY_ENABLED) ───────────────────
apply_relay_egress_guard() {
    (( RELAY_ENABLED )) || return 0
    step "Egress-фильтр релея (nft): блок nginx→приватные/metadata сети"
    local uid; uid="$(id -u "$nginx_user" 2>/dev/null || true)"
    [[ -z "$uid" ]] && { warn "Не нашёл uid пользователя nginx ($nginx_user) — egress-фильтр не поставлен; РЕЛЕЙ НЕБЕЗОПАСЕН."; RELAY_ENABLED=0; return 0; }

    local f="$EGRESS_NFT"
    cat > "$f" <<NFT
#!/usr/sbin/nft -f
# Managed by install-mirror.sh — блокирует исходящий трафик nginx (uid $uid) на
# :443 в приватные/служебные/metadata IPv4-сети. Защита открытого релея от SSRF
# и DNS-rebinding НЕЗАВИСИМО от ответа DNS-резолвера.
table inet mirror_egress
delete table inet mirror_egress
table inet mirror_egress {
    chain output {
        type filter hook output priority 0; policy accept;
        meta skuid $uid ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.0.2.0/24, 192.168.0.0/16, 198.18.0.0/15, 198.51.100.0/24, 203.0.113.0/24, 224.0.0.0/4 } tcp dport 443 drop
    }
}
NFT
    chmod 0644 "$f"
    if ! nft -f "$f"; then
        warn "Не удалось загрузить egress-фильтр — ОТКЛЮЧАЮ релей ради безопасности."
        RELAY_ENABLED=0
        return 0
    fi
    cat > "$EGRESS_UNIT" <<UNIT
[Unit]
Description=Egress guard for install-mirror open relay (block nginx -> private nets)
After=network-pre.target nftables.service
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/nft -f $f
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
    # Фильтр УЖЕ загружен через nft -f выше; enable влияет только на переживание
    # перезагрузки — неудачу отмечаем, но релей не отключаем (защита сейчас активна).
    systemctl daemon-reload >/dev/null 2>&1 || warn "systemctl daemon-reload не сработал."
    if systemctl enable mirror-egress.service >/dev/null 2>&1; then
        ok "Egress-фильтр активен и переживёт перезагрузку."
    else
        warn "Egress-фильтр активен, но автозапуск включить не удалось — после ребута выполните: nft -f $f"
    fi
}

# ── Файрвол зеркала: открыть :443 всему миру (сюда приходят клиенты) ──────────
open_mirror_443() {
    if [[ "$pkg_mgr" == "apt" ]] && command -v ufw >/dev/null 2>&1; then
        ufw allow 443/tcp >/dev/null 2>&1 && ok "ufw: 443/tcp открыт." || warn "ufw allow 443/tcp не применился."
    elif command -v firewall-cmd >/dev/null 2>&1; then
        local zone; zone="$(firewall-cmd --get-default-zone 2>/dev/null || echo public)"
        firewall-cmd --permanent --zone="$zone" --add-port=443/tcp >/dev/null 2>&1 \
            && firewall-cmd --reload >/dev/null 2>&1 \
            && ok "firewalld: 443/tcp открыт в зоне $zone." \
            || warn "firewalld: не удалось открыть 443/tcp."
    fi
}

# ── Финальная инструкция для панели/бэкенда ──────────────────────────────────
print_cascade_instructions() {
    local mirror_src steps_file="./cascade-next-steps.txt"
    if [[ -n "$ip2" && "$ip2" != "0.0.0.0" ]]; then
        mirror_src="$ip2"
    else
        mirror_src="$(src_toward "$backend_ip")"
    fi
    [[ -z "$mirror_src" ]] && mirror_src="<IP2_ЗЕРКАЛА>"

    local relay_note="выключен (неизвестный SNI → reset)"
    (( RELAY_ENABLED )) && relay_note="ВКЛючён (egress-фильтр nft активен)"

    local text
    text="$(cat <<EOF

════════════════════════════════════════════════════════════════════════════
  КАСКАД НАСТРОЕН. ДАЛЬНЕЙШИЕ ШАГИ (на БЭКЕНД-ноде и в панели)
════════════════════════════════════════════════════════════════════════════

Роли:
  • Зеркало (ЭТОТ сервер):  публичный вход ${public_addr}:443, исходящий ${mirror_src}
  • Бэкенд-нода:            ${backend_host} (${backend_ip}):443  — ваш дефолтный конфиг
  • Общий SNI (selfsteal):  ${sni}
  • Открытый релей:         ${relay_note}

1) БЭКЕНД-НОДА — дефолтный конфиг, проверьте лишь:
     • serverNames в Reality == ${sni}
     • публичный inbound принимает голый TLS (БЕЗ proxy_protocol/xver на :443)

2) ПАНЕЛЬ (Remnawave) → Host этого инбаунда. РАЗНЫЕ поля (не путать!):
     • Address     : ${public_addr}          (публичный адрес/домен ЗЕРКАЛА)
     • SNI         : ${sni}                   (== serverNames Reality на бэкенде)
     • publicKey   : <из инбаунда БЭКЕНДА>    (Reality public key бэкенда)
     • shortId     : <из инбаунда БЭКЕНДА>    (Reality shortId бэкенда)
   Клиенты коннектятся на Address (зеркало); ключи/SNI — бэкендовые.

3) БЭКЕНД — закрыть :443 на всех, кроме зеркала (${mirror_src}).

   РЕКОМЕНДУЕТСЯ (если бэкенд ставился нашим скриптом) — одной опцией:
       sudo bash remnawave-node.sh --front-ip ${mirror_src}
   (или ответить на запрос «behind an SNI-mirror front?» при установке).
   Ставит nft-таблицу mirror_gate автоматически и переживает перезагрузку.

   Проверенные РУЧНЫЕ альтернативы (на выбор, идемпотентны):

   • nftables — атомарный ruleset в отдельном файле (НЕ дописывать в nftables.conf):
       cat >/etc/mirror-gate.nft <<'NFT'
       #!/usr/sbin/nft -f
       table inet mirror_gate
       delete table inet mirror_gate
       table inet mirror_gate {
           chain input {
               type filter hook input priority -5; policy accept;
               iif lo accept
               ct state established,related accept
               tcp dport 443 ip saddr { ${mirror_src} } accept
               tcp dport 443 ct state new drop
           }
       }
       NFT
       nft -f /etc/mirror-gate.nft
       # персистентность: oneshot-юнит 'ExecStart=/usr/sbin/nft -f /etc/mirror-gate.nft'

   • firewalld — зона автоопределяется, приоритеты (accept раньше drop), идемпотентно:
       Z=\$(firewall-cmd --get-default-zone)
       firewall-cmd --permanent --zone="\$Z" --add-rich-rule='rule priority="100" family="ipv4" source address="${mirror_src}" port port="443" protocol="tcp" accept'
       firewall-cmd --permanent --zone="\$Z" --add-rich-rule='rule priority="200" family="ipv4" port port="443" protocol="tcp" drop'
       firewall-cmd --reload

   • ufw — allow ДО deny (порядок важен), повторный запуск не дублирует:
       ufw allow from ${mirror_src} to any port 443 proto tcp
       ufw deny 443/tcp

4) Control-порт панель→нода (напр. 2222) на бэкенде НЕ зеркалится и остаётся
   доступен ТОЛЬКО с IP панели — не ослабляйте это ограничение.

Проверка (с внешнего хоста):
     openssl s_client -connect ${public_addr}:443 -servername ${sni} </dev/null
     # прямой пробинг бэкенда должен таймаутиться после п.3:
     openssl s_client -connect ${backend_ip}:443 -servername ${sni} </dev/null
════════════════════════════════════════════════════════════════════════════
EOF
)"
    printf '%s\n' "$text"
    if printf '%s\n' "$text" > "$steps_file" 2>/dev/null; then
        echo "(инструкция сохранена в $(cd "$(dirname "$steps_file")" && pwd)/$(basename "$steps_file"))"
    fi
}

# ── Режим: SNI-зеркало (каскад для двух нод) ─────────────────────────────────
setup_cascade_mirror() {
    cat <<'EOF'

── Каскад для двух нод: SNI-зеркало ──────────────────────────────────────────
Этот сервер (зеркало) слушает :443 и по SNI отдаёт TLS-поток:
  • ваш секретный SNI → бэкенд-нода :443;
  • неизвестный SNI   → reset (по умолчанию, безопасно);
  • открытый релей чужого SNI → только по явному согласию.
Reality-ключи/SNI — на БЭКЕНДЕ; клиентам даётся адрес ЗЕРКАЛА.
EOF
    collect_inputs
    detect_pkg_mgr
    install_packages
    apply_relay_egress_guard   # до генерации конфига: если фильтр не встал, релей выключится
    apply_nginx_config
    open_mirror_443
    print_cascade_instructions
    step "Готово"
    ok "Зеркало настроено."
}

# ── Начальное меню ───────────────────────────────────────────────────────────
main_menu() {
    cat <<'EOF'

****************************
Скрипт для Debian/Ubuntu и Rocky/RHEL. nginx SNI-mirror для каскада Remnawave/Reality.
Все сообщения дублируются в лог: install-mirror.log
EOF
    echo ""
    echo "Выберите вариант установки:"
    echo "  1) Каскад для двух нод — SNI-зеркало"
    echo "     (этот сервер → бэкенд-нода; неизвестный SNI → reset по умолчанию)"
    echo "  0) Выход"
    ask "Ваш выбор [1]: "
    case "${REPLY_VALUE:-1}" in
        1) setup_cascade_mirror ;;
        0) echo "Выход."; exit 0 ;;
        *) die "Неизвестный вариант: ${REPLY_VALUE}" ;;
    esac
}

# Библиотечный режим (MIRROR_LIB=1) — не запускать меню, только определить функции.
if [[ "${MIRROR_LIB:-0}" != "1" ]]; then
    main_menu
fi
: # keep exit status 0 in lib mode when sourced
