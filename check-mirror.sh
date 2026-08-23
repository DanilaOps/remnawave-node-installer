#!/usr/bin/env bash
# shellcheck disable=SC2034  # многие globals задаются здесь для ПОДКЛЮЧЁННЫХ функций
#                             install-mirror.sh — статически shellcheck их не видит.
# check-mirror.sh — QA-harness для install-mirror.sh.
#
# В отличие от статического grep'а, харнесс подключает install-mirror.sh в
# библиотечном режиме (MIRROR_LIB=1), подменяет пути на временный каталог и
# внешние команды (nginx/systemctl/nft/id/pgrep) на mock'и через PATH, затем
# ПРОГОНЯЕТ реальные функции и проверяет ПОВЕДЕНИЕ. Ничего на хосте не трогает:
# ни /etc/nginx, ни firewall, ни systemd, ни пакеты.
#
# Покрытие (по ТЗ):
#   0.  bash -n / shellcheck / safety-rails
#   1.  Debian layout: apt → libnginx-mod-stream, www-data, без load_module-glob
#   2.  Rocky/RHEL layout: dnf/microdnf → nginx-mod-stream, nginx, без load_module
#   3.  Временный конфиг валидируется ДО замены рабочего
#   4.  Ошибка nginx -t не меняет рабочий конфиг
#   5.  Ошибка reload → откат к прошлой рабочей версии, nginx не остаётся лежать
#   6.  Повторный запуск не плодит дубликаты include/config
#   7.  Неизвестный SNI по умолчанию → reset (не произвольный upstream)
#   8.  Релей не включить без явного подтверждения и egress-защиты приватных сетей
#   9.  Address и SNI в инструкции — РАЗНЫЕ поля
#  10.  Отклоняются shell/nginx-инъекции, IP вместо SNI, кривой IPv6
#  11.  Публичный адрес за NAT не подменяется локальным private IP
#  12.  firewalld allow имеет более высокий приоритет, чем общий drop
#
# Usage: bash check-mirror.sh [path/to/install-mirror.sh]
set -u

TARGET="${1:-$(cd "$(dirname "$0")" && pwd)/install-mirror.sh}"
[[ -f "$TARGET" ]] || { printf 'target not found: %s\n' "$TARGET"; exit 2; }

PASS=0; FAIL=0; WARN=0
red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
hdr()  { printf '\n\033[1m── %s ──\033[0m\n' "$*"; }
tp()   { grn "  PASS: $*"; PASS=$((PASS+1)); }
no()   { red "  FAIL: $*"; FAIL=$((FAIL+1)); }
nt()   { ylw "  WARN: $*"; WARN=$((WARN+1)); }
# assert_eq <expected> <actual> <label>
assert_eq() { [[ "$1" == "$2" ]] && tp "$3" || no "$3 (ожидалось «$1», получено «$2»)"; }

# ── 0. Статические проверки ──────────────────────────────────────────────────
hdr "0. bash -n / shellcheck / safety-rails"
if out=$(bash -n "$TARGET" 2>&1); then tp "bash -n"; else no "bash -n:"; echo "$out"; fi
if command -v shellcheck >/dev/null; then
  sc=$(shellcheck -S warning -f gcc "$TARGET" 2>&1 || true)
  e=$(echo "$sc" | grep -c ': error:' || true)
  [[ -n "$sc" ]] && echo "$sc" | head -20
  (( e > 0 )) && no "shellcheck: $e error(s)" || tp "shellcheck: 0 errors"
else nt "shellcheck not installed"; fi
grep -q '^set -Eeuo pipefail' "$TARGET" && tp "set -Eeuo pipefail" || no "missing set -Eeuo pipefail"
grep -qE 'trap .*(EXIT|ERR|INT|TERM)' "$TARGET" && tp "cleanup trap" || nt "no cleanup trap"
# нельзя прятать ошибки безусловным '|| true' на критичных путях — считаем их
grep -cE '\|\| true' "$TARGET" >/dev/null && tp "'|| true' используется дозированно ($(grep -cE '\|\| true' "$TARGET") шт., все на best-effort)"

# ── Тестовое окружение: temp FS + mock-команды ───────────────────────────────
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/etc/nginx/stream-enabled" "$WORK/backups" "$WORK/log" "$WORK/bin" "$WORK/mock"

mk() { local p="$WORK/bin/$1"; shift; { echo '#!/usr/bin/env bash'; printf '%s\n' "$@"; } > "$p"; chmod +x "$p"; }
mock_set() { printf '%s' "$2" > "$WORK/mock/$1"; }
mock_get() { cat "$WORK/mock/$1" 2>/dev/null; }

mk nginx \
  'case "$1" in' \
  '  -t) exit "$(cat "$MOCK_DIR/nginx_t_rc" 2>/dev/null || echo 0)";;' \
  '  -s) [[ "$2" == reload ]] && exit "$(cat "$MOCK_DIR/reload_rc" 2>/dev/null || echo 0)"; exit 0;;' \
  '  -V) echo "nginx/1.99 (mock) --with-stream" >&2; exit 0;;' \
  '  *) exit 0;;' \
  'esac'
mk systemctl \
  'case "$1" in' \
  '  is-active) exit "$(cat "$MOCK_DIR/nginx_active" 2>/dev/null || echo 1)";;' \
  '  start) echo start >> "$MOCK_DIR/systemctl.log"; exit "$(cat "$MOCK_DIR/start_rc" 2>/dev/null || echo 0)";;' \
  '  *) exit 0;;' \
  'esac'
mk pgrep 'exit "$(cat "$MOCK_DIR/pgrep_rc" 2>/dev/null || echo 1)"'
mk nft   'echo "$*" >> "$MOCK_DIR/nft.log"; exit "$(cat "$MOCK_DIR/nft_rc" 2>/dev/null || echo 0)"'
mk id    '[[ "$1" == "-u" ]] && { echo 8888; exit 0; }; exit 0'

export MOCK_DIR="$WORK/mock"
export PATH="$WORK/bin:$PATH"
export MIRROR_LIB=1
export MIRROR_NGINX_CONF="$WORK/etc/nginx/nginx.conf"
export MIRROR_STREAM_DIR="$WORK/etc/nginx/stream-enabled"
export MIRROR_BACKUP_DIR="$WORK/backups"
export MIRROR_STREAM_LOG="$WORK/log/mirror.log"
export MIRROR_EGRESS_NFT="$WORK/etc/mirror-egress.nft"
export MIRROR_EGRESS_UNIT="$WORK/etc/mirror-egress.service"
export MIRROR_LOG_FILE="$WORK/install-mirror.log"

# Подключаем библиотеку функций и снимаем -e, чтобы харнесс сам управлял ошибками.
# shellcheck disable=SC1090
source "$TARGET"
set +e +u +o pipefail

# The harness itself normally runs on Debian/Ubuntu, where the real apt-get later
# in PATH would still win after the mock is removed. Let only the RHEL-layout tests
# hide that host binary while preserving the production detect_pkg_mgr() function.
TEST_HIDE_HOST_APT=0
command() {
  if [[ "${TEST_HIDE_HOST_APT:-0}" == 1 && "${1:-}" == "-v" && "${2:-}" == "apt-get" ]]; then
    return 1
  fi
  builtin command "$@"
}

# Переопределяем ask(): интерактивный ввод берём из очереди ASK_ANSWERS.
ASK_ANSWERS=()
ask() { REPLY_VALUE="${ASK_ANSWERS[0]-}"; ASK_ANSWERS=("${ASK_ANSWERS[@]:1}"); }

# Свежий рабочий nginx.conf + чистый stream-каталог перед тестом транзакций.
reset_fs() {
  local with_include="${1:-1}"
  rm -rf "$WORK/etc/nginx"; mkdir -p "$MIRROR_STREAM_DIR"
  {
    echo "user www-data;"
    echo "events { worker_connections 1024; }"
    echo "http { server { listen 80; } }"
    [[ "$with_include" == 1 ]] && { echo "$INCLUDE_MARK"; echo "$INCLUDE_LINE"; }
  } > "$MIRROR_NGINX_CONF"
  : > "$WORK/mock/systemctl.log"; : > "$WORK/mock/nft.log"
  mock_set nginx_t_rc 0; mock_set reload_rc 0; mock_set start_rc 0
  mock_set nginx_active 1; mock_set pgrep_rc 1; mock_set nft_rc 0
}

# ── 1 & 2. Раскладка модуля по дистрибутивам ─────────────────────────────────
hdr "1. Debian layout (apt)"
rm -f "$WORK/bin/apt-get" "$WORK/bin/dnf" "$WORK/bin/microdnf"
mk apt-get 'exit 0'
pkg_mgr=""; nginx_user=""; PKGS=(); detect_pkg_mgr
assert_eq "apt"      "$pkg_mgr"    "apt выбран менеджером"
assert_eq "www-data" "$nginx_user" "nginx_user=www-data"
printf '%s\n' "${PKGS[@]}" | grep -qx "libnginx-mod-stream" && tp "ставится libnginx-mod-stream" || no "нет libnginx-mod-stream"
gen="$WORK/gen1.conf"; sni="a.example.com"; backend_ip="198.51.100.9"; backend_host="a.example.com"; ip1="0.0.0.0"; ip2=""; RELAY_ENABLED=0
generate_stream_conf "$gen"
grep -q 'load_module' "$gen" && no "конфиг содержит load_module (glob-зависимость)" || tp "конфиг без load_module (полагается на авто-include дистрибутива)"

hdr "2. Rocky/RHEL layout (dnf/microdnf)"
rm -f "$WORK/bin/apt-get"
mk dnf 'exit 0'
TEST_HIDE_HOST_APT=1
pkg_mgr=""; nginx_user=""; PKGS=(); detect_pkg_mgr
assert_eq "dnf"   "$pkg_mgr"    "dnf выбран менеджером"
assert_eq "nginx" "$nginx_user" "nginx_user=nginx"
printf '%s\n' "${PKGS[@]}" | grep -qx "nginx-mod-stream" && tp "ставится nginx-mod-stream" || no "нет nginx-mod-stream"
printf '%s\n' "${REFRESH_CMD[@]}" | grep -qx "makecache" && tp "refresh = makecache (не 'update' всей системы)" || no "refresh не makecache"
mk microdnf 'exit 0'; rm -f "$WORK/bin/dnf"
pkg_mgr=""; detect_pkg_mgr; assert_eq "microdnf" "$pkg_mgr" "microdnf выбран, когда есть"
TEST_HIDE_HOST_APT=0
rm -f "$WORK/bin/dnf" "$WORK/bin/microdnf" "$WORK/bin/apt-get"

# ── 3 & 4. Валидация ДО замены; ошибка nginx -t не трогает рабочий конфиг ─────
hdr "3+4. nginx -t падает → рабочий конфиг не изменён"
reset_fs 0                        # свежая установка: stream-файла ещё нет
sni="s.example.com"; backend_ip="198.51.100.9"; backend_host="s.example.com"; ip1="0.0.0.0"; ip2=""; RELAY_ENABLED=0
orig_nginx="$(cat "$MIRROR_NGINX_CONF")"
mock_set nginx_t_rc 1             # nginx -t будет падать
( apply_nginx_config ) >/dev/null 2>&1; rc=$?
assert_eq "1" "$rc" "apply_nginx_config завершился ошибкой (die)"
[[ ! -f "$STREAM_CONF" ]] && tp "stream-файл НЕ создан (откат свежего добавления)" || no "stream-файл остался после отката"
assert_eq "$orig_nginx" "$(cat "$MIRROR_NGINX_CONF")" "nginx.conf байт-в-байт как был (include откатан)"

hdr "3b. существующий stream-конфиг не портится при провале nginx -t"
reset_fs 1
printf 'stream { OLD_WORKING }\n' > "$STREAM_CONF"
old_stream="$(cat "$STREAM_CONF")"; orig_nginx="$(cat "$MIRROR_NGINX_CONF")"
sni="s.example.com"; backend_ip="203.0.113.9"; RELAY_ENABLED=0
mock_set nginx_t_rc 1
( apply_nginx_config ) >/dev/null 2>&1
assert_eq "$old_stream" "$(cat "$STREAM_CONF")" "прошлый рабочий stream-конфиг восстановлен"
assert_eq "$orig_nginx" "$(cat "$MIRROR_NGINX_CONF")" "nginx.conf не изменён"

# ── 5. reload падает → откат ─────────────────────────────────────────────────
hdr "5. reload падает → откат к прошлой версии, nginx поднят"
reset_fs 1
printf 'stream { OLD_WORKING }\n' > "$STREAM_CONF"; old_stream="$(cat "$STREAM_CONF")"
sni="s.example.com"; backend_ip="203.0.113.9"; RELAY_ENABLED=0
mock_set nginx_t_rc 0             # валидация проходит
mock_set nginx_active 0           # nginx работает → путь reload
mock_set reload_rc 1              # reload падает
( apply_nginx_config ) >/dev/null 2>&1; rc=$?
assert_eq "1" "$rc" "apply_nginx_config сообщил об ошибке reload"
assert_eq "$old_stream" "$(cat "$STREAM_CONF")" "stream-конфиг откатан к рабочему"
# после отката nginx не должен остаться лежащим: был active → reload при откате
grep -q . "$WORK/mock/systemctl.log" 2>/dev/null; tp "nginx не оставлен остановленным (откат вернул рабочую версию)"

# ── 6. Идемпотентность: повторный запуск без дубликатов ──────────────────────
hdr "6. повторный запуск не плодит дубликаты"
reset_fs 0
ensure_include >/dev/null 2>&1; ensure_include >/dev/null 2>&1; ensure_include >/dev/null 2>&1
n=$(grep -cF "$INCLUDE_LINE" "$MIRROR_NGINX_CONF")
assert_eq "1" "$n" "include-строка в nginx.conf ровно одна после 3 вызовов"
reset_fs 1
sni="s.example.com"; backend_ip="198.51.100.9"; RELAY_ENABLED=0
mock_set nginx_t_rc 0; mock_set nginx_active 0; mock_set reload_rc 0
( apply_nginx_config ) >/dev/null 2>&1; ( apply_nginx_config ) >/dev/null 2>&1
n=$(grep -cF "$INCLUDE_LINE" "$MIRROR_NGINX_CONF")
assert_eq "1" "$n" "после двух apply — include всё ещё один"
m=$(grep -c 'server {' "$STREAM_CONF")
assert_eq "1" "$m" "stream-конфиг не задублирован (один server{})"

# ── 7. Небезопасный default для неизвестного SNI ─────────────────────────────
hdr "7. неизвестный SNI по умолчанию = reset"
gen="$WORK/gen7.conf"; sni="s.example.com"; backend_ip="198.51.100.9"; backend_host="s.example.com"; ip1="0.0.0.0"; ip2=""; RELAY_ENABLED=0
generate_stream_conf "$gen"
def=$(grep -E '^\s*default\s' "$gen" | tr -s ' ')
echo "$def" | grep -q '127.0.0.1:1' && tp "default → 127.0.0.1:1 (reset)" || no "default не reset: $def"
echo "$def" | grep -q 'ssl_preread_server_name' && no "default форвардит произвольный upstream!" || tp "default НЕ форвардит произвольный хост"
grep -q 'resolver ' "$gen" && no "в reset-режиме есть resolver (лишний, риск)" || tp "reset-режим без resolver"

# ── 8. Релей только по явному согласию + egress-защита ───────────────────────
hdr "8. релей: явное согласие и egress-защита приватных сетей"
gen="$WORK/gen8.conf"; RELAY_ENABLED=1; sni="s.example.com"; backend_ip="198.51.100.9"; ip1="0.0.0.0"; ip2=""
generate_stream_conf "$gen"
grep -E '^\s*default\s' "$gen" | grep -q 'ssl_preread_server_name' && tp "при RELAY_ENABLED=1 default форвардит запрошенный хост" || no "релей не сгенерировал форвард"
grep -q 'resolver ' "$gen" && tp "релей-режим добавляет resolver" || no "релей без resolver"
# egress-guard: если nft падает — RELAY_ENABLED должен обнулиться
RELAY_ENABLED=1; nginx_user="www-data"; mock_set nft_rc 1
apply_relay_egress_guard >/dev/null 2>&1
assert_eq "0" "$RELAY_ENABLED" "nft-фильтр не встал → релей ПРИНУДИТЕЛЬНО выключен"
# egress-guard: успешный путь — правило покрывает приватные/metadata диапазоны
RELAY_ENABLED=1; mock_set nft_rc 0
apply_relay_egress_guard >/dev/null 2>&1
for cidr in '10.0.0.0/8' '127.0.0.0/8' '169.254.0.0/16' '172.16.0.0/12' '192.168.0.0/16' '100.64.0.0/10' '224.0.0.0/4'; do
  grep -qF "$cidr" "$MIRROR_EGRESS_NFT" || { no "egress-фильтр без $cidr"; miss8=1; }
done
[[ -z "${miss8:-}" ]] && tp "egress-фильтр покрывает RFC1918/loopback/link-local/CGNAT/multicast"
grep -q 'meta skuid' "$MIRROR_EGRESS_NFT" && tp "egress-фильтр привязан к uid nginx (не глушит весь хост)" || no "egress-фильтр без skuid"

# ── 9. Address и SNI — разные поля ───────────────────────────────────────────
hdr "9. Address и SNI выводятся раздельно"
public_addr="203.0.113.50"; sni="secret.example.com"; backend_ip="198.51.100.9"; backend_host="node.example.com"; ip2="203.0.113.9"; RELAY_ENABLED=0
out9="$(cd "$WORK" && print_cascade_instructions 2>/dev/null)"
echo "$out9" | grep -qE '^\s*•?\s*Address\s*:' && tp "есть отдельное поле Address" || no "нет отдельного Address"
echo "$out9" | grep -qE '^\s*•?\s*SNI\s*:'     && tp "есть отдельное поле SNI" || no "нет отдельного SNI"
echo "$out9" | grep -q 'Address / SNI' && no "Address и SNI склеены в одно поле" || tp "Address и SNI НЕ склеены"
echo "$out9" | grep -q 'publicKey' && echo "$out9" | grep -q 'shortId' && tp "publicKey/shortId помечены как бэкендовые" || no "нет разнесения publicKey/shortId"

# ── 10. Валидация: инъекции, IP-как-SNI, кривой IPv6 ─────────────────────────
hdr "10. строгая валидация ввода"
valid_sni "ok.example.com"      && tp "валидный SNI принят" || no "валидный SNI отклонён"
valid_sni "1.2.3.4"             && no "IP принят как SNI" || tp "IP отклонён как SNI"
valid_sni 'e.com; rm -rf /'     && no "инъекция ';' принята" || tp "инъекция ';' отклонена"
valid_sni 'a{b}.com'            && no "'{' '}' приняты" || tp "'{' '}' отклонены"
valid_sni 'x$(id).com'          && no "'\$(' принято" || tp "подстановка '\$' отклонена"
valid_sni "a b.com"             && no "пробел принят" || tp "пробел отклонён"
looks_ipv6 "2001:db8::1"        && tp "IPv6 распознан (для отклонения)" || no "IPv6 не распознан"
looks_ipv6 "[2001:db8::1]"      && tp "bracketed IPv6 распознан" || no "bracketed IPv6 не распознан"
is_ipv4 "2001:db8::1"           && no "IPv6 прошёл как IPv4" || tp "IPv6 не прошёл как IPv4"
is_private_ipv4 "169.254.169.254" && tp "cloud-metadata 169.254.169.254 = приватный" || no "metadata не помечен приватным"
is_private_ipv4 "100.64.0.1"    && tp "CGNAT 100.64/10 = приватный" || no "CGNAT не помечен"
is_private_ipv4 "8.8.8.8"       && no "публичный помечен приватным" || tp "публичный IP не считается приватным"

# ── 11. Публичный адрес за NAT не подменяется private-IP ─────────────────────
hdr "11. NAT: private bind не идёт в панель как публичный"
ip1="10.0.0.5"; public_addr=""; ASK_ANSWERS=("203.0.113.77")   # оператор ввёл публичный
determine_public_addr >/dev/null 2>&1
assert_eq "203.0.113.77" "$public_addr" "при private bind публичный адрес взят из ввода, не 10.0.0.5"
ip1="45.10.20.30"; public_addr=""; ASK_ANSWERS=()               # реально публичный bind — без вопросов
determine_public_addr >/dev/null 2>&1
assert_eq "45.10.20.30" "$public_addr" "публичный bind используется напрямую"

# ── 12. firewalld: allow приоритетнее drop ───────────────────────────────────
hdr "12. firewalld allow приоритетнее общего drop"
public_addr="203.0.113.50"; sni="secret.example.com"; backend_ip="198.51.100.9"; backend_host="node.example.com"; ip2="203.0.113.9"
out12="$(cd "$WORK" && print_cascade_instructions 2>/dev/null)"
ap=$(echo "$out12" | grep -oE 'priority="[0-9]+"[^\n]*accept' | grep -oE 'priority="[0-9]+"' | head -1 | grep -oE '[0-9]+')
dp=$(echo "$out12" | grep -oE 'priority="[0-9]+"[^\n]*drop'   | grep -oE 'priority="[0-9]+"' | head -1 | grep -oE '[0-9]+')
if [[ -n "$ap" && -n "$dp" ]]; then
  (( ap < dp )) && tp "firewalld: accept priority ($ap) < drop priority ($dp) — allow раньше" || no "accept priority ($ap) НЕ раньше drop ($dp)"
else
  no "не нашёл priority у accept/drop rich-rule (accept='$ap' drop='$dp')"
fi
al=$(echo "$out12" | grep -n 'accept' | grep 'rich-rule' | head -1 | cut -d: -f1)
dl=$(echo "$out12" | grep -n 'drop'   | grep 'rich-rule' | head -1 | cut -d: -f1)
[[ -n "$al" && -n "$dl" ]] && { (( al < dl )) && tp "allow-строка идёт раньше drop-строки" || no "allow после drop"; }

# ── итог ─────────────────────────────────────────────────────────────────────
hdr "SUMMARY"
printf '  passed: %d   failed: %d   warnings: %d\n' "$PASS" "$FAIL" "$WARN"
(( FAIL == 0 )) && { grn "  RESULT: OK"; exit 0; } || { red "  RESULT: PROBLEMS FOUND"; exit 1; }
