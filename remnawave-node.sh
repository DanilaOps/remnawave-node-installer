#!/usr/bin/env bash
#
# remnawave-node.sh — self-contained installer for a Remnawave selfsteal node.
#
# Installs, on a single fresh server:
#   * Docker + compose
#   * Remnawave node container (ghcr.io/remnawave/node) — inline, no upstream script
#   * Nginx selfsteal in TCP mode (127.0.0.1:<selfsteal-port>, PROXY protocol)
#   * A TLS certificate (Let's Encrypt on 443, or Cloudflare DNS-01 wildcard)
#   * A single VLESS Reality + Vision inbound
#   * Certificate auto-renewal
#
# And creates, in the Remnawave panel via its HTTP API:
#   * a config-profile holding the Xray config
#   * the node (linked to the profile)
#   * a host (selfsteal Reality entry)
#
# Firewall hardening is delegated to node-accelerator (kept by design). We track a
# fork (drobyazkome/node-accelerator) pinned at v3.8-rw1 = upstream jestivald v3.8 +
# a dpkg-lock-timeout fix so the XanMod build doesn't fail against unattended-upgrades.
#
# Nothing here depends on third-party installer scripts except the firewall step.
#
set -Eeuo pipefail

# This installer uses bash-4.4+ features (empty-array expansion under `set -u`,
# associative arrays, mapfile, ${var^^}). Target OSes (Ubuntu 22.04+/24.04,
# Debian 12) all ship bash >= 5.0; fail fast on anything older (RHEL/CentOS 7).
if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4) )); then
  echo "Requires bash >= 4.4 (found ${BASH_VERSION})." >&2; exit 1
fi

# Ubuntu 22.04+/24.04 ship needrestart, which prompts (or stalls under a pipe)
# during apt upgrades — including node-accelerator's CrowdSec APT step. Force it
# fully non-interactive process-wide so no apt call can hang waiting on a TTY.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export NEEDRESTART_SUSPEND=1

# ── Defaults ────────────────────────────────────────────────────────────────
INSTALLER_VERSION="3.3.2-rw2"

# Pin the node image to a released version, not a moving :latest, so the same script
# yields the same Node/Xray build (reproducible installs + validation). Override with
# --node-image or REMNANODE_IMAGE. For a digest pin that survives a re-tag, resolve
# this release with `docker buildx imagetools inspect` and use image@sha256:... .
NODE_IMAGE="${REMNANODE_IMAGE:-ghcr.io/remnawave/node:3.3.2}"
NODE_CONTAINER="remnanode"
NGINX_IMAGE="nginx:1.29.3-alpine"
NGINX_CONTAINER="nginx-selfsteal"

NODE_DIR="/opt/remnanode"
NGINX_DIR="/opt/nginx-selfsteal"
STATE_DIR="/opt/remnawave-node/state"
ACME_HOME="/root/.acme.sh"

# RemnaNode 3.3.2 normally uses the Xray bundled in its image. Pin a known-good
# core on the host and bind-mount it over /usr/local/bin/xray; the image's
# /usr/local/bin/rw-core symlink then executes this exact binary. Keeping the
# binary outside the container makes the pin survive compose recreates/resume.
XRAY_CORE_VERSION="26.6.27"
XRAY_CORE_REPO="XTLS/Xray-core"
XRAY_CORE_DIR="${NODE_DIR}/xray-core-${XRAY_CORE_VERSION}"
XRAY_CORE_BIN="${XRAY_CORE_DIR}/xray"

# Bootstrap installer is fetched at the pinned NA_REF (not a hardcoded 'main'), so
# the bootstrap and the modules it pulls come from the same ref.
NODE_ACCELERATOR_REPO="drobyazkome/node-accelerator"   # fork of jestivald/node-accelerator

DEFAULT_NODE_PORT="2222"
DEFAULT_MASK="reality"           # reality (Xray owns 443, Reality) | grpc-tls (nginx owns 443, real cert, gRPC behind TLS)
DEFAULT_GRPC_PORT="11443"        # loopback port Xray gRPC inbound listens on (grpc-tls mask)
DEFAULT_GRPC_SERVICE="grpc"      # gRPC serviceName; nginx routes /<serviceName>/Tun -> Xray
DEFAULT_MODE="socket"            # socket | tcp — fallback transport nginx <- Xray
SOCKET_PATH="/dev/shm/nginx.sock"
DEFAULT_SELFSTEAL_PORT="9443"
DEFAULT_RENEW_PORT="8443"
DEFAULT_CERT_MODE="le443"        # le443 | cf-dns
DEFAULT_TEMPLATE="builtin"       # builtin generator (no external fetch) | sni-templates id/name
TEMPLATES_REPO="DigneZzZ/remnawave-scripts"
GEO_REPO="runetfreedom/russia-v2ray-rules-dat"   # updated RU geosite/geoip
# Decoy templates (folder names under sni-templates/), id order matches DigneZzZ.
TEMPLATE_FOLDERS=(10gag convertit converter downloader filecloud games-site modmanager speedtest YouTube 503-1 503-2)
DEFAULT_TCP_PORTS="80,443,2087"
DEFAULT_UDP_PORTS="443,2087"
DEFAULT_NA_REF="v3.8-rw1"        # fork tag: upstream v3.8 + dpkg-lock-timeout fix
DEFAULT_COUNTRY="NL"
# firefox/chrome are stable across clients; "randomized" breaks some Xray builds
# (macOS: "tls: CurvePreferences includes unsupported curve") — live-tested 2026-07-07.
DEFAULT_FP="firefox"

# ── CLI-populated globals ───────────────────────────────────────────────────
DRY_RUN=0
NONINTERACTIVE=0
DOMAIN=""
PANEL_URL=""
PANEL_TOKEN=""
COUNTRY=""
NODE_NAME=""
HOST_REMARK=""
HOST_ADDRESS=""                  # host connect address: grpc-tls defaults to DOMAIN, reality to public IP
SQUAD_NAME=""                    # Internal Squad to enable the inbound in (users see it); optional
SQUAD_UUID=""
SQUAD_CREATE=0                   # create SQUAD_NAME if it does not exist (interactive "new squad")
PROFILE_NAME=""
ADOPT_PROFILE=0                  # allow adopting a DIFFERENTLY-NAMED profile that owns our inbound tag
# ── Cascade bridge (this node = exit; accepts SS traffic from an entry node) ──
BRIDGE=0                         # 1 = also stand up a Shadowsocks bridge inbound for a cascade
BRIDGE_ENTRY_IP=""               # entry node public IP allowed to reach the SS bridge port
BRIDGE_SS_PORT="9999"            # SS bridge inbound port (firewalled to BRIDGE_ENTRY_IP only)
BRIDGE_METHOD="chacha20-ietf-poly1305"  # SS cipher shared by entry + exit
BRIDGE_USER=""                   # panel username whose ssPassword becomes the bridge secret
BRIDGE_SS_PASSWORD=""            # resolved at panel step (from existing user or freshly minted)
ENTRY_DOMAIN=""                  # entry node's own selfsteal domain (for the printed entry config)
BRIDGE_TAG=""                    # SS inbound tag, derived from node name
TAG_NAMESPACE=""                 # deterministic per-node prefix for all Xray tags
OUTBOUND_TAG_DIRECT=""
OUTBOUND_TAG_BLOCK=""
CERT_MODE=""
CF_TOKEN=""
ACME_EMAIL=""
SECRET_KEY_OVERRIDE=""
TEMPLATE=""
RANDOMIZE=1
ROTATE_KEYS=0
NAMESPACE_HASH=1                 # 1 = append a hash of NODE_NAME to tag namespace so
                                 # names differing only by punctuation stay distinct.
                                 # ON by default for FRESH installs (globally-unique tags).
                                 # load_inputs forces it OFF on --resume of state that
                                 # predates this key, so deployed nodes are never re-tagged.
                                 # Override either way: --namespace-hash / --no-namespace-hash.
GEO=1
GEO_DIR=""
GEO_TIMEOUT="600"                # overall cap (s) for the whole geo-download stage
NODE_PORT=""
MASK=""
GRPC_PORT=""
GRPC_SERVICE=""
HARDENING=1
MODE=""
TRANSPORT=""
XHTTP_PORT=""
XHTTP_PATH="/api/v1/update"
SELFSTEAL_PORT=""
RENEW_PORT=""
SSH_PORT=""
PANEL_WHITELIST=""
FRONT_IP=""                      # cascade back-end: lock tcp/443 to the SNI-mirror front's egress IP(s)
TCP_PORTS=""
UDP_PORTS=""
NA_REF=""
FP=""
SKIP_FIREWALL=0
SKIP_XRAY_VALIDATE=0
SKIP_UPDATE=0
SKIP_CROWDSEC=0                  # CrowdSec on by default. Its APT step is slow (minutes),
                                 # so --crowdsec's companion auto-bump gives protect a 900s
                                 # cap and na_run prints a heartbeat so a quiet install is
                                 # not mistaken for a hang. Turn it off with --skip-crowdsec.
PANEL_TOKEN_FILE=""              # read the token from a file (keeps it out of `ps`)
PANEL_TOKEN_FROM_CLI=0           # set when --panel-token was used (warn: visible in ps)
RESUME=0                         # skip already-completed expensive stages
REFRESH_DECOY=0                  # regenerate the decoy even on resume
PREFLIGHT=0                      # checks only, mutate nothing
NA_URL=""                        # override node-accelerator installer URL
NA_DIR=""                        # use a local node-accelerator checkout instead of fetching
NA_TAR=""                        # use a local node-accelerator tarball instead of fetching
CROWDSEC_TIMEOUT="180"           # seconds allowed for the node-accelerator protect phase
CROWDSEC_TIMEOUT_SET=0            # set when --crowdsec-timeout given explicitly (blocks auto-bump)
OPTIMIZE_TIMEOUT="0"             # optimize time cap (s); 0 = unlimited. XanMod/BBR installs are
                                 # legitimately slow — a cap can SIGKILL apt/dpkg mid-transaction.
NA_SAFETY_DELAY="600"            # node-accelerator auto-rollback window; we disarm it after verifying na_filter
CACHE_DIR="/opt/remnawave-node/cache"
CURRENT_STAGE=""                 # last stage entered (for the failure report)

# ── Runtime globals ─────────────────────────────────────────────────────────
NODE_PUBLIC_IP=""
NODE_SECRET_KEY=""
REALITY_PRIVATE=""
REALITY_PUBLIC=""
REALITY_SHORT_ID=""
CONFIG_PROFILE_UUID=""
INBOUND_UUID=""
declare -a INBOUND_UUIDS=()      # all created/linked inbound UUIDs (for Internal Squad)
NODE_UUID=""
HOST_UUID=""
INBOUND_TAG=""
INBOUND_TAG_XHTTP=""
INBOUND_TAG_GRPC=""
declare -a ACTIVE_INBOUNDS=()
declare -a HOST_UUIDS=()
BRIDGE_INBOUND_UUID=""           # panel UUID of the SS bridge inbound (linked to node, no host)
BRIDGE_SQUAD_UUID=""             # squad the node's inbounds live in (bridge user joins it)
ENTRY_REALITY_PRIVATE=""         # entry-node keypair, minted only to print its config
ENTRY_REALITY_PUBLIC=""
ENTRY_REALITY_SHORT_ID=""

# ── Colors / logging ────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; BLUE=$'\033[34m'; RESET=$'\033[0m'
else
  BOLD="" DIM="" RED="" GREEN="" YELLOW="" BLUE="" RESET=""
fi
log()  { printf '%s\n' "$*"; }
# Diagnostics (info/ok/warn) go to STDERR so they never contaminate a value
# captured via $( … ) — several panel_* helpers run inside command substitution
# whose stdout is parsed by jq. log() (data) stays on stdout.
info() { log "${BLUE}[*]${RESET} $*" >&2; }
ok()   { log "${GREEN}[+]${RESET} $*" >&2; }
warn() {
  log "${YELLOW}[!]${RESET} $*" >&2
  # Collect every warning for the end-of-install report (only written when
  # something was actually warned about). SCRATCH is set a few lines below,
  # before any warn() call can fire.
  [[ -n "${SCRATCH:-}" ]] && printf '%s\n' "$*" >> "$SCRATCH/warnings.log" 2>/dev/null || true
}
die()  { trap - ERR; set +e; log "${RED}[x]${RESET} $*" >&2; failure_report; exit 1; }
step() { log; log "${BOLD}== $* ==${RESET}"; }

SCRATCH="$(mktemp -d)" || { echo "mktemp -d failed — cannot create scratch dir" >&2; exit 1; }
chmod 700 "$SCRATCH"   # request bodies/headers carry the panel token + Reality key
CERT_STOPPED_CONTAINER=""   # set while a 443-owner is stopped for ACME; restarted on any exit
# External installers we shell out to (node-accelerator → CrowdSec/apt/dialog) can
# leave the controlling terminal in raw mode (ONLCR off → staircased output) if a
# timeout kills them mid-step. Reset the tty to a sane line-discipline. No-op when
# stdin is not a terminal (non-interactive / piped runs).
restore_tty() { [[ -t 0 ]] && stty sane 2>/dev/null || true; }
cleanup() {
  # If ACME issuance stopped the container that owns 443 and we exit before it is
  # restarted (e.g. issuance failed), bring it back so the node isn't left down.
  [[ -n "$CERT_STOPPED_CONTAINER" ]] && docker start "$CERT_STOPPED_CONTAINER" >/dev/null 2>&1 || true
  [[ -n "${SCRATCH:-}" ]] && rm -rf "${SCRATCH:?}"
  restore_tty   # never leave the operator with a garbled terminal
}
trap cleanup EXIT
on_error() {
  local line="$1" cmd="$2"
  trap - ERR; set +e   # don't let the report's own commands re-enter this handler
  log "${RED}[x]${RESET} Failed at line ${line}: ${cmd}" >&2
  failure_report
  exit 1
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

# ── Stage tracking (resume) ─────────────────────────────────────────────────
# Each expensive, idempotent stage records completion in $STATE_DIR/stages. With
# --resume a completed stage is skipped on re-run. Stages that populate globals
# needed later (panel resources, cert, keys) are NOT gated — they are already
# idempotent and always re-run cheaply.
stage_file() { printf '%s' "$STATE_DIR/stages"; }
stage_done() { [[ "$RESUME" == "1" ]] && [[ -f "$(stage_file)" ]] && grep -qxF "$1" "$(stage_file)"; }
stage_mark() {
  [[ "$DRY_RUN" == "1" ]] && return 0
  mkdir -p "$STATE_DIR" 2>/dev/null || return 0
  grep -qxF "$1" "$(stage_file)" 2>/dev/null || printf '%s\n' "$1" >> "$(stage_file)"
}
# run_stage <name> <fn> [args…] — skip if already done (resume), else run + record.
run_stage() {
  local name="$1"; shift
  CURRENT_STAGE="$name"
  if stage_done "$name"; then info "Resume: stage '$name' already complete — skipping."; return 0; fi
  "$@"
  stage_mark "$name"
}
# Warn (once, if applicable) that the OS needs a reboot after the kernel/libs
# upgrade. Used both in the success summary and in the failure report.
reboot_required_note() {
  [[ -f /var/run/reboot-required ]] || return 0
  local pkgs=""
  [[ -f /var/run/reboot-required.pkgs ]] && pkgs=" ($(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null | sed 's/ *$//'))"
  warn "A reboot is required to finish applying system updates${pkgs} — run 'reboot' when convenient." >&2
}
# Print a concise recovery report on failure: last stage, live container state,
# whether public 443 answers, and the exact command to resume. Never prints
# secrets. Silent during dry-run / before inputs are collected.
failure_report() {
  [[ "$DRY_RUN" == "1" ]] && return 0
  [[ -z "${DOMAIN:-}" ]] && return 0
  log >&2
  log "${YELLOW}== Install interrupted ==${RESET}" >&2
  [[ -n "$CURRENT_STAGE" ]] && log "  last stage:   $CURRENT_STAGE" >&2
  if command -v docker >/dev/null 2>&1; then
    local ns rs
    ns="$(docker inspect -f '{{.State.Status}}' "$NGINX_CONTAINER" 2>/dev/null || echo 'not created')"
    rs="$(docker inspect -f '{{.State.Status}}' "$NODE_CONTAINER" 2>/dev/null || echo 'not created')"
    log "  nginx ($NGINX_CONTAINER): $ns" >&2
    log "  node  ($NODE_CONTAINER): $rs" >&2
  fi
  if command -v curl >/dev/null 2>&1 && [[ -n "${NODE_PUBLIC_IP:-}" ]]; then
    local code
    code="$(curl -k -s -o /dev/null -w '%{http_code}' --resolve "$DOMAIN:443:$NODE_PUBLIC_IP" "https://$DOMAIN/" --max-time 8 2>/dev/null || echo 000)"
    [[ "$code" == 200 ]] && log "  public :443:  serving decoy (HTTP 200)" >&2 \
                         || log "  public :443:  not serving yet (HTTP $code)" >&2
  fi
  log >&2
  log "  Re-run to resume (reloads saved inputs + panel resources + Reality keys):" >&2
  log "    sudo bash $0 --resume -y" >&2
  log >&2
  reboot_required_note
}

# End-of-install report — written ONLY when warnings occurred during the run.
# A clean install stays quiet; a noisy one leaves an on-disk trail of everything
# that was warned about, plus the identity needed to audit it in the panel.
install_report() {
  [[ "$DRY_RUN" == "1" ]] && return 0
  [[ -s "${SCRATCH:-/nonexistent}/warnings.log" ]] || return 0
  local rf   # declare+assign separately (SC2155); $$ avoids same-second clobber
  rf="$STATE_DIR/install-report-$(date +%Y%m%d-%H%M%S)-$$.log"
  mkdir -p "$STATE_DIR"
  {
    echo "Install completed WITH WARNINGS — $(date -Iseconds)"
    echo "node:    ${NODE_NAME:-?} (${NODE_UUID:-?})"
    echo "profile: ${PROFILE_NAME:-?} (${CONFIG_PROFILE_UUID:-?})"
    echo "host(s): ${HOST_UUIDS[*]:-${HOST_UUID:-?}}"
    echo "domain:  ${DOMAIN:-?}   mask: ${MASK:-?}   transport: ${TRANSPORT:-?}"
    echo
    echo "── Warnings (in order) ──"
    cat "$SCRATCH/warnings.log"
  } > "$rf"
  chmod 600 "$rf"
  warn "Install finished with $(wc -l < "$SCRATCH/warnings.log" | tr -d ' ') warning(s) — report saved to $rf"
}

# ── Validators ──────────────────────────────────────────────────────────────
valid_domain() { [[ "$1" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]; }
valid_port()   { [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && (( "$1" <= 65535 )); }
valid_email()  { [[ "$1" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; }
valid_cc()     { [[ "$1" =~ ^[A-Za-z]{2}$ ]]; }
valid_url()    { [[ "$1" =~ ^https?://[^[:space:]/]+ ]]; }
# Config-profile names: panel allows only letters, numbers, underscore, dash, space.
valid_profile_name() { [[ "$1" =~ ^[A-Za-z0-9_\ -]+$ ]]; }
# Panel username rules (backend-contract create-user): 3-36, letters/numbers/_/-.
valid_username() { [[ "$1" =~ ^[A-Za-z0-9_-]{3,36}$ ]]; }
sanitize_profile_name() { printf '%s' "$1" | tr -c 'A-Za-z0-9_ -' ' ' | tr -s ' ' | sed 's/^ *//;s/ *$//'; }

valid_ipv4_cidr() {
  local ip="${1%%/*}" mask="" o
  [[ "$1" == */* ]] && mask="${1#*/}"
  [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  for o in "${BASH_REMATCH[@]:1}"; do (( 10#$o <= 255 )) || return 1; done
  if [[ -n "$mask" ]]; then [[ "$mask" =~ ^[0-9]{1,2}$ ]] && (( 10#$mask <= 32 )) || return 1; fi
}
# Strict single IP address (IPv4 or IPv6, NO CIDR). Prefers python3's ipaddress;
# the shell fallback reuses valid_ipv4_cidr for v4 and a loose hex/colon check for v6.
valid_ip() {
  [[ -n "$1" && "$1" != */* ]] || return 1
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$1" <<'PY'
import sys, ipaddress
try: ipaddress.ip_address(sys.argv[1])
except ValueError: sys.exit(1)
sys.exit(0)
PY
    return $?
  fi
  if [[ "$1" == *:* ]]; then [[ "$1" =~ ^[0-9A-Fa-f:]+$ ]]; return; fi
  valid_ipv4_cidr "$1"
}
valid_whitelist() {
  [[ -n "$1" ]] || return 1
  # Prefer python3's ipaddress module: it validates IPv4/IPv6 addresses and CIDRs
  # with correct mask ranges (the shell regex below is loose on IPv6).
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$1" <<'PY'
import sys, ipaddress
items = [x for x in sys.argv[1].split(',') if x != '']
if not items:
    sys.exit(1)
for it in items:
    try:
        ipaddress.ip_network(it, strict=False) if '/' in it else ipaddress.ip_address(it)
    except ValueError:
        sys.exit(1)
sys.exit(0)
PY
    return $?
  fi
  # Shell fallback (IPv4 strict via valid_ipv4_cidr; IPv6 loose) when python3 is absent.
  local item; local -a items
  IFS=',' read -r -a items <<< "$1"
  for item in "${items[@]}"; do
    valid_ipv4_cidr "$item" && continue
    [[ "$item" == *:* && "$item" =~ ^[0-9A-Fa-f:]+(/[0-9]{1,3})?$ ]] && continue
    return 1
  done
}
valid_port_list() {
  local item; local -a items
  [[ -n "$1" ]] || return 1
  IFS=',' read -r -a items <<< "$1"
  for item in "${items[@]}"; do valid_port "$item" || return 1; done
}

# ── Helpers ─────────────────────────────────────────────────────────────────
need_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Run as root: sudo bash $0"; }
need_cmd()  { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }

read_default() {
  local prompt="$1" default="${2:-}" value
  if [[ "$NONINTERACTIVE" == "1" ]]; then printf '%s' "$default"; return; fi
  if [[ -n "$default" ]]; then read -r -p "$prompt [$default]: " value
  else read -r -p "$prompt: " value; fi
  printf '%s' "${value:-$default}"
}
yes_no() {
  local prompt="$1" default="${2:-y}" hint answer
  [[ "$default" == "y" ]] && hint="Y/n" || hint="y/N"
  if [[ "$NONINTERACTIVE" == "1" ]]; then [[ "$default" == "y" ]]; return; fi
  while true; do
    read -r -p "$prompt [$hint]: " answer; answer="${answer:-$default}"
    case "$answer" in
      y|Y|yes|Yes|YES|д|да|Да) return 0 ;;
      n|N|no|No|NO|н|нет|Нет) return 1 ;;
      *) warn "Answer y or n." ;;
    esac
  done
}
# Numbered single-choice prompt. Prints the chosen value on stdout (so it is safe
# in $( … )); the menu + warnings go to stderr. Accepts a 1-based number or the
# literal value. Usage: X="$(choose_one "<prompt>" "<default>" opt1 opt2 …)"
choose_one() {
  local prompt="$1" def="$2"; shift 2
  local opts=("$@") i n ans o
  if [[ "$NONINTERACTIVE" == "1" ]]; then printf '%s' "$def"; return 0; fi
  for i in "${!opts[@]}"; do
    n=$((i+1))
    if [[ "${opts[$i]}" == "$def" ]]; then printf '    %d) %s (default)\n' "$n" "${opts[$i]}" >&2
    else printf '    %d) %s\n' "$n" "${opts[$i]}" >&2; fi
  done
  while :; do
    ans="$(read_default "$prompt [number/name]" "$def")"
    if [[ "$ans" =~ ^[0-9]+$ ]] && (( ans >= 1 && ans <= ${#opts[@]} )); then printf '%s' "${opts[$((ans-1))]}"; return 0; fi
    for o in "${opts[@]}"; do [[ "$ans" == "$o" ]] && { printf '%s' "$o"; return 0; }; done
    warn "Enter 1-${#opts[@]} or a valid name." >&2
  done
}
run() {
  if [[ "$DRY_RUN" == "1" ]]; then printf 'DRY-RUN:'; printf ' %q' "$@"; printf '\n'; return 0; fi
  "$@"
}
backup_file() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  run cp -a "$f" "${f}.bak-$(date +%Y%m%d-%H%M%S)"
}
detect_ssh_port() {
  # Prefer sshd's own effective config (robust across OpenSSH 9.8+ where the
  # per-connection process is 'sshd-session' and the /sshd/ ss match misses).
  # Fall back to the socket parse, then to 22.
  local d
  d="$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}' || true)"
  [[ -z "$d" ]] && d="$(ss -tlnp 2>/dev/null | awk '/sshd/ { sub(/.*:/, "", $4); print $4; exit }' || true)"
  valid_port "${d:-}" && printf '%s' "$d" || printf '22'
}
# True for RFC1918 / CGNAT / link-local / loopback IPv4 — addresses a panel or client
# on the internet cannot reach. Used to reject a NAT'd route source as the node's
# public IP. Non-IPv4 or genuinely public addresses return false.
is_private_ip() {
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  local a="${BASH_REMATCH[1]}" b="${BASH_REMATCH[2]}"
  (( a==10 )) && return 0
  (( a==127 )) && return 0
  (( a==192 && b==168 )) && return 0
  (( a==172 && b>=16 && b<=31 )) && return 0
  (( a==169 && b==254 )) && return 0          # link-local
  (( a==100 && b>=64 && b<=127 )) && return 0 # CGNAT 100.64.0.0/10
  return 1
}
detect_public_ip() {
  local ip
  ip="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' | head -1 || true)"
  # On a NAT'd VPS/cloud instance the route source is a private/CGNAT address the
  # panel and clients cannot reach — fall back to an external echo in that case.
  if [[ -z "$ip" ]] || is_private_ip "$ip"; then
    ip="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
  fi
  valid_ip "$ip" || ip=""   # never let a captive-portal/error body leak into the IP
  printf '%s' "$ip"
}
# Capture ss output first, then grep from a here-string. Piping straight into
# `grep -q` lets grep close the pipe on the first match, so ss/awk take SIGPIPE
# and — under `set -o pipefail` — the pipeline returns 141 (a false "not
# listening") even when the port IS bound. Same trap noted in fetch_template.
port_listening() { local out; out="$(ss -tln 2>/dev/null | awk '{print $4}')"; grep -qE "(^|[:.])$1\$" <<<"$out"; }

# ── Panel API ───────────────────────────────────────────────────────────────
panel_req() {
  local method="$1" path="$2" body="${3:-}"
  local url="${PANEL_URL%/}${path}" resp code out
  # Keep the bearer token AND the request body (which carries the Reality private
  # key / SECRET_KEY) out of curl's argv — argv is world-readable via /proc on a
  # shared host. Both are passed by file from the 700 scratch dir instead.
  local hf="$SCRATCH/auth.hdr"
  printf 'Authorization: Bearer %s\n' "$PANEL_TOKEN" > "$hf"
  # Bounded: a hung/unreachable panel must fail the call, not wedge the install.
  local -a args=(-sS --connect-timeout 10 --max-time 60 -X "$method" "$url" -H "@$hf" -w $'\n___CODE___%{http_code}')
  if [[ -n "$body" ]]; then
    printf '%s' "$body" > "$SCRATCH/req.json"
    args+=(-H "Content-Type: application/json" --data "@$SCRATCH/req.json")
  fi
  resp="$(curl "${args[@]}" 2>/dev/null)" || return 1
  code="${resp##*___CODE___}"; out="${resp%___CODE___*}"; out="${out%$'\n'}"
  if [[ ! "$code" =~ ^2 ]]; then
    warn "Panel API ${method} ${path} -> HTTP ${code}"
    [[ -n "$out" ]] && log "${DIM}${out}${RESET}" >&2
    return 1
  fi
  printf '%s' "$out"
}
panel_check_auth() {
  info "Checking panel connectivity and token…"
  if [[ -n "$SECRET_KEY_OVERRIDE" ]]; then
    # --secret-key bypasses /api/keygen; validate the token against a lower-privilege
    # protected endpoint so a token WITHOUT the Keygen scope still passes this check.
    panel_req GET /api/nodes >/dev/null \
      || die "Cannot reach panel or token invalid (GET /api/nodes failed): $PANEL_URL"
  else
    panel_req GET /api/keygen >/dev/null \
      || die "Cannot reach panel or token lacks scope (GET /api/keygen failed). Pass --secret-key '<SECRET_KEY>' to bypass keygen: $PANEL_URL"
  fi
  ok "Panel reachable, token valid."
}
panel_get_keygen() {
  local r; r="$(panel_req GET /api/keygen)" || return 1
  # Remnawave 3.x returns response.secretKey. Keep the pre-3.x pubKey fallback so
  # the installer fails gracefully against an older panel instead of returning null.
  printf '%s' "$r" | jq -r '.response.secretKey // .response.pubKey // empty'
}
panel_next_sequence() {
  local cc="${1^^}" nodes
  nodes="$(panel_req GET /api/nodes 2>/dev/null || echo '')"
  [[ -z "$nodes" ]] && { printf '01'; return; }
  local n
  n="$(printf '%s' "$nodes" | jq -r --arg cc "$cc" \
    '[.response[]? | .name | capture("^" + $cc + "-(?<n>[0-9]+)") | .n | tonumber] | max // 0' 2>/dev/null || echo 0)"
  printf '%02d' $(( n + 1 ))
}
# Resolve the panel API token without ever putting it on a command line the way
# `--panel-token` does (that value is visible in `ps`). Precedence:
#   --panel-token (back-compat, warns) > --panel-token-file > REMNAWAVE_PANEL_TOKEN_FILE
#   > REMNAWAVE_PANEL_TOKEN > hidden interactive prompt.
resolve_panel_token() {
  local f=""
  if [[ -n "$PANEL_TOKEN" ]]; then
    [[ "$PANEL_TOKEN_FROM_CLI" == "1" ]] && \
      warn "--panel-token is visible in the process list (ps). Prefer --panel-token-file or REMNAWAVE_PANEL_TOKEN_FILE."
    return 0
  fi
  f="$PANEL_TOKEN_FILE"; [[ -z "$f" ]] && f="${REMNAWAVE_PANEL_TOKEN_FILE:-}"
  if [[ -n "$f" ]]; then
    [[ -f "$f" ]] || die "Panel token file not found: $f"
    PANEL_TOKEN="$(tr -d '\r\n' < "$f")"
    [[ -n "$PANEL_TOKEN" ]] || die "Panel token file is empty: $f"
    return 0
  fi
  if [[ -n "${REMNAWAVE_PANEL_TOKEN:-}" ]]; then
    PANEL_TOKEN="$REMNAWAVE_PANEL_TOKEN"; return 0
  fi
  [[ "$NONINTERACTIVE" == "1" ]] && die "Panel token required: pass --panel-token-file, REMNAWAVE_PANEL_TOKEN_FILE, or REMNAWAVE_PANEL_TOKEN."
  read -r -s -p "Panel API token: " PANEL_TOKEN; echo
  [[ -n "$PANEL_TOKEN" ]] || die "Panel token required."
}

# ── Reality keys ────────────────────────────────────────────────────────────
generate_reality_keys() {
  if [[ "$DRY_RUN" == "1" ]]; then
    REALITY_PRIVATE="DRY_PRIVATE"; REALITY_PUBLIC="DRY_PUBLIC"; REALITY_SHORT_ID="dryshortid"; return
  fi
  info "Generating Reality x25519 keypair (Xray ${XRAY_CORE_VERSION})…"
  local out priv pub
  out="$("$XRAY_CORE_BIN" x25519 2>/dev/null)" \
    || die "Failed to run 'xray x25519' from $XRAY_CORE_BIN."
  # Newer Xray-core (25.x) renamed the x25519 output labels: "Public key:" became
  # "Password:". Match both so a fresh node image doesn't break key generation.
  priv="$(printf '%s' "$out" | grep -iE 'private'          | awk '{print $NF}' | head -1 || true)"
  pub="$(printf '%s' "$out" | grep -iE 'public|password'  | awk '{print $NF}' | head -1 || true)"
  [[ -n "$priv" && -n "$pub" ]] || die "Could not parse xray x25519 output."
  REALITY_PRIVATE="$priv"; REALITY_PUBLIC="$pub"
  REALITY_SHORT_ID="$(openssl rand -hex 8)"
  ok "Reality keys ready (public: ${REALITY_PUBLIC:0:12}…, shortId: $REALITY_SHORT_ID)."
}

# ── Xray config (VLESS + Reality; tcp/raw + Vision, and/or xhttp) ───────────
# XHTTP fields in `extra` must be identical in the inbound and in the Host that
# produces client subscriptions.  Remnawave exposes the client half as
# `xhttpExtraParams`; keeping one generator avoids subtle client/server drift.
xhttp_extra_json() {
  jq -cn '{noSSEHeader:true, xPaddingBytes:"100-1000", scMaxBufferedPosts:30, scMaxEachPostBytes:1000000, scStreamUpServerSecs:"20-80"}'
}

# XHTTP in `both` needs a second public listener. Never allow it to reuse a
# port owned by this node, selfsteal, SSH, or the optional monitor.
xhttp_port_reserved() {
  local candidate="$1" reserved
  for reserved in 80 443 "${NODE_PORT:-}" "${SELFSTEAL_PORT:-}" "${RENEW_PORT:-}" "${SSH_PORT:-}" 45876; do
    [[ -n "$reserved" && "$candidate" == "$reserved" ]] && return 0
  done
  return 1
}

# One inbound JSON. $1=tag $2=port $3=network(raw|xhttp). All inbounds share the
# same Reality keypair. In socket mode the fallback target is the nginx unix
# socket; in tcp mode it is loopback.
build_reality_inbound() {
  local tag="$1" port="$2" net="$3" dest stream
  if [[ "$MODE" == "tcp" ]]; then dest="127.0.0.1:${SELFSTEAL_PORT}"; else dest="$SOCKET_PATH"; fi
  if [[ "$net" == "xhttp" ]]; then
    stream="$(jq -n --arg path "$XHTTP_PATH" --argjson extra "$(xhttp_extra_json)" \
      '{network:"xhttp", xhttpSettings:{mode:"packet-up", path:$path, extra:$extra}}')"
  else
    stream='{"network":"raw"}'
  fi
  jq -n --arg tag "$tag" --argjson port "$port" --arg dest "$dest" --arg sni "$DOMAIN" \
    --arg priv "$REALITY_PRIVATE" --arg sid "$REALITY_SHORT_ID" --argjson stream "$stream" '
  {
    tag: $tag, port: $port, listen: "0.0.0.0", protocol: "vless",
    settings: { clients: [], decryption: "none" },
    sniffing: { enabled: true, destOverride: ["http","tls","quic"], routeOnly: true },
    streamSettings: ( $stream + {
      security: "reality",
      realitySettings: { show:false, target:$dest, xver:1, shortIds:[$sid], privateKey:$priv, serverNames:[$sni] },
      sockopt: { tcpNoDelay: true, tcpFastOpen: true }
    })
  }'
}

# grpc-tls mask: a plain VLESS + gRPC inbound bound to loopback, security "none".
# Nginx terminates real TLS on public 443 and grpc_pass'es to this. No Reality here.
build_grpc_inbound() {
  local tag="$1" port="$2"
  jq -n --arg tag "$tag" --argjson port "$port" --arg svc "$GRPC_SERVICE" '
  {
    tag: $tag, port: $port, listen: "127.0.0.1", protocol: "vless",
    settings: { clients: [], decryption: "none" },
    sniffing: { enabled: true, destOverride: ["http","tls","quic"], routeOnly: true },
    streamSettings: {
      network: "grpc", security: "none",
      grpcSettings: { serviceName: $svc, multiMode: false },
      sockopt: { tcpNoDelay: true }
    }
  }'
}

# Shadowsocks bridge inbound (cascade exit). Listens on 0.0.0.0 so the entry node
# can reach it; the firewall (na_filter) restricts the source to BRIDGE_ENTRY_IP.
# The password is the panel user's ssPassword, resolved before this is called.
build_bridge_inbound() {
  jq -n --arg tag "$BRIDGE_TAG" --argjson port "$BRIDGE_SS_PORT" \
    --arg method "$BRIDGE_METHOD" --arg pw "$BRIDGE_SS_PASSWORD" '
  {
    tag: $tag, port: $port, listen: "0.0.0.0", protocol: "shadowsocks",
    settings: { method: $method, password: $pw, network: "tcp,udp" },
    sniffing: { enabled: true, destOverride: ["http","tls","quic"], metadataOnly: false },
    streamSettings: { sockopt: { tcpNoDelay: true, tcpFastOpen: true } }
  }'
}
build_xray_config() {
  local inbounds="[]" spec tag port net ib
  for spec in "${ACTIVE_INBOUNDS[@]}"; do
    IFS=: read -r tag port net <<< "$spec"
    if [[ "$net" == "grpc-tls" ]]; then
      ib="$(build_grpc_inbound "$tag" "$port")"
    else
      ib="$(build_reality_inbound "$tag" "$port" "$net")"
    fi
    inbounds="$(jq -n --argjson arr "$inbounds" --argjson ib "$ib" '$arr + [$ib]')"
  done
  # Cascade exit: append the SS bridge inbound so this node also accepts entry traffic.
  if [[ "$BRIDGE" == "1" && -n "$BRIDGE_SS_PASSWORD" ]]; then
    ib="$(build_bridge_inbound)"
    inbounds="$(jq -n --argjson arr "$inbounds" --argjson ib "$ib" '$arr + [$ib]')"
  fi
  jq -n --argjson inbounds "$inbounds" --arg direct "$OUTBOUND_TAG_DIRECT" --arg block "$OUTBOUND_TAG_BLOCK" '
{
  log: { loglevel: "warning" },
  # Half-close linger fix: uplinkOnly/downlinkOnly 0 tears a connection down the
  # instant one side closes (default ~1s per hop — costly for short-lived web
  # requests). connIdle 300 keeps keep-alive sockets 5 min; handshake 4 tolerates
  # slow first RTTs on weak networks while still dropping stalled handshakes.
  policy: {
    levels: { "0": { handshake: 4, connIdle: 300, uplinkOnly: 0, downlinkOnly: 0 } }
  },
  dns: {
    hosts: {
      "dns.google": ["8.8.8.8", "8.8.4.4"],
      "cloudflare-dns.com": ["1.1.1.1", "1.0.0.1"]
    },
    # Flat UDP resolvers only. On a datacenter node plain UDP:53 to these is not
    # censored, so DoH would add ~96ms on cache-miss (measured on the NL/exit prod
    # nodes) with no censorship-resistance upside. hosts{} still pins the DoH FQDNs
    # for any downstream config that references them.
    servers: [ "1.1.1.1", "1.0.0.1", "8.8.8.8", "9.9.9.9" ],
    queryStrategy: "UseIPv4",
    serveStale: true,
    serveExpiredTTL: 43200
  },
  inbounds: $inbounds,
  outbounds: [
    { tag: $direct, protocol: "freedom", settings: { domainStrategy: "UseIPv4" },
      streamSettings: { sockopt: { tcpNoDelay: true, tcpFastOpen: true } } },
    { tag: $block, protocol: "blackhole" }
  ],
  routing: {
    domainStrategy: "IPIfNonMatch",
    domainMatcher: "hybrid",
    rules: [
      { type: "field", domain: ["geosite:private", "geosite:category-ads-all"], outboundTag: $block },
      { type: "field", ip: ["geoip:private"], outboundTag: $block },
      { type: "field", protocol: ["bittorrent"], outboundTag: $block },
      # Drop QUIC (HTTP/3 over UDP:443) inside the tunnel: browsers fall back to
      # TLS-over-TCP (spec-mandated), which Reality/Vision handles best. UDP:443
      # tunneled over TCP is slower (double congestion control), evades sniffing
      # for domain rules, and is often throttled to foreign IPs from RU anyway.
      { type: "field", network: "udp", port: 443, outboundTag: $block },
      { type: "field", network: "tcp,udp", outboundTag: $direct }
    ]
  }
}'
}

# Validate the generated config with the same pinned Xray binary that the node
# will run. It is mounted into a disposable node container so the image's bundled
# geosite/geoip assets remain available during `xray run -test`.
validate_xray_config() {
  local cfg="$1"
  [[ "$DRY_RUN" == "1" ]] && return 0
  [[ "$SKIP_XRAY_VALIDATE" == "1" ]] && { info "Xray config validation skipped (--skip-xray-validate)."; return 0; }
  command -v docker >/dev/null 2>&1 || { warn "docker unavailable — skipping Xray config validation."; return 0; }
  local f="$SCRATCH/xray-config.json"
  printf '%s' "$cfg" > "$f"; chmod 600 "$f"   # config embeds the Reality private key
  info "Validating generated Xray config (xray -test)…"
  if docker run --rm --entrypoint /usr/local/bin/xray \
       -v "$XRAY_CORE_BIN:/usr/local/bin/xray:ro" \
       -v "$f:/cfg/config.json:ro" "$NODE_IMAGE" \
       run -test -c /cfg/config.json >/dev/null 2>"$SCRATCH/xray-test.err"; then
    ok "Xray config valid."
  else
    warn "Xray rejected the generated config:"
    sed 's/^/    /' "$SCRATCH/xray-test.err" >&2 2>/dev/null || true
    die "Invalid Xray config — aborting before pushing to panel. Override with --skip-xray-validate."
  fi
}

# ── System update + automatic security updates ──────────────────────────────
# Full package upgrade + enable unattended-upgrades so the box keeps pulling
# security fixes on its own. Runs first thing after the operator confirms; skip
# with --skip-update. apt-only; a no-op elsewhere.
system_update() {
  [[ "$SKIP_UPDATE" == "1" ]] && { warn "System update skipped (--skip-update)."; return; }
  command -v apt-get >/dev/null 2>&1 || { info "Non-apt system — skipping OS update."; return; }
  step "System update + automatic security updates"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: apt-get update && apt-get -y full-upgrade; install+enable unattended-upgrades"
    return
  fi
  export DEBIAN_FRONTEND=noninteractive
  # A fresh cloud VM usually still holds the dpkg lock: cloud-init and the first
  # unattended-upgrades run race us for it, and apt then fails (or used to hang)
  # with no hint. Wait for init to settle, then let apt itself wait on the lock.
  if command -v cloud-init >/dev/null 2>&1; then
    info "Waiting for cloud-init to finish (up to 5 min)…"
    timeout 300 cloud-init status --wait >/dev/null 2>&1 || true
  fi
  info "Refreshing package lists…"
  apt-get -o DPkg::Lock::Timeout=300 update -qq || warn "apt-get update reported issues (continuing)."
  info "Upgrading all packages (this can take a while)…"
  # Keep existing config files on conflict; never open an interactive prompt.
  apt-get -y -qq -o DPkg::Lock::Timeout=300 \
    -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
    full-upgrade || warn "Package upgrade reported issues (continuing)."
  info "Enabling automatic security updates (unattended-upgrades)…"
  apt-get install -y -qq unattended-upgrades || warn "Could not install unattended-upgrades."
  # Turn on the periodic security-upgrade job. The distro's 50unattended-upgrades
  # template already whitelists the -security origin.
  cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
  systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
  apt-get -y -qq autoremove >/dev/null 2>&1 || true
  ok "System upgraded; automatic security updates enabled."
  # NB: keep this an `if`, not `[[ … ]] && warn`. As the function's last command a
  # bare test returns 1 when the flag file is absent, which trips the ERR trap and
  # aborts the whole install right after a *successful* update.
  if [[ -f /var/run/reboot-required ]]; then
    warn "A reboot is required to finish applying updates (kernel/libs) — reboot after the install completes."
  fi
}

# ── Base system ─────────────────────────────────────────────────────────────
install_base() {
  step "Base packages"
  if [[ "$DRY_RUN" == "1" ]]; then info "DRY-RUN: apt-get install curl jq openssl socat ca-certificates unzip"; return; fi
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      curl jq openssl socat ca-certificates iproute2 cron unzip >/dev/null
  else
    warn "Non-apt system: ensure curl, jq, openssl, socat, cron, unzip are installed."
  fi
  ok "Base packages ready."
}
install_docker() {
  step "Docker"
  if command -v docker >/dev/null 2>&1; then ok "Docker already present: $(docker --version)"; return; fi
  if [[ "$DRY_RUN" == "1" ]]; then info "DRY-RUN: install docker-ce from download.docker.com apt repo (GPG-verified)"; return; fi
  local os_id="" codename=""
  if [[ -r /etc/os-release ]]; then
    os_id="$(. /etc/os-release && printf '%s' "$ID")"
    codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"
  fi
  if command -v apt-get >/dev/null 2>&1 && [[ ("$os_id" == "ubuntu" || "$os_id" == "debian") && -n "$codename" ]]; then
    # Official apt repository with GPG-verified packages instead of piping
    # get.docker.com into sh (unverified root code, no pinned signing key).
    # Also guarantees the docker compose v2 plugin the rest of the script uses.
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL --connect-timeout 15 --max-time 60 "https://download.docker.com/linux/${os_id}/gpg" -o /etc/apt/keyrings/docker.asc \
      || die "Failed to download the Docker GPG key."
    chmod a+r /etc/apt/keyrings/docker.asc
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
      "$(dpkg --print-architecture)" "$os_id" "$codename" > /etc/apt/sources.list.d/docker.list
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o DPkg::Lock::Timeout=300 \
      docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null \
      || die "Docker installation from the official apt repository failed."
  else
    # Non-Debian-family fallback: the convenience script is the only portable path.
    warn "No Ubuntu/Debian apt detected — falling back to get.docker.com convenience script."
    local s="$SCRATCH/get-docker.sh"
    curl -fsSL --connect-timeout 15 --max-time 120 https://get.docker.com -o "$s" || die "Failed to download Docker installer."
    sh "$s" || die "Docker installation failed."
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  docker compose version >/dev/null 2>&1 || die "docker compose v2 plugin is missing after install."
  ok "Docker installed: $(docker --version)"
}

# Download and verify the exact Xray release used by the node. Checksums are from
# the official XTLS/Xray-core v26.6.27 release .dgst assets. Only architectures
# supported by both RemnaNode and this installer are accepted deliberately.
install_pinned_xray_core() {
  step "Pinned Xray core ${XRAY_CORE_VERSION}"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: download, SHA-256 verify and install Xray ${XRAY_CORE_VERSION} at $XRAY_CORE_BIN"
    return 0
  fi

  local asset expected_sha arch archive unpack actual_sha actual_version
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64)
      asset="Xray-linux-64.zip"
      expected_sha="b3e5902d06d6282fe53cfa2fc426058b9aeaa429b2c812e20887cd47f26d08bf"
      ;;
    aarch64|arm64)
      asset="Xray-linux-arm64-v8a.zip"
      expected_sha="13a251379bea366c2cf10363ad71e75734193d401f26f518bf0c25e5c8f8c931"
      ;;
    *) die "Unsupported architecture for pinned Xray ${XRAY_CORE_VERSION}: $arch" ;;
  esac

  if [[ -x "$XRAY_CORE_BIN" ]]; then
    actual_version="$("$XRAY_CORE_BIN" version 2>/dev/null | head -n 1 || true)"
    if [[ "$actual_version" == "Xray ${XRAY_CORE_VERSION} "* ]]; then
      ok "Pinned Xray already present: $actual_version"
      return 0
    fi
    warn "Existing pinned-core file has an unexpected version: ${actual_version:-unreadable}; replacing it."
  fi

  archive="$SCRATCH/$asset"
  unpack="$SCRATCH/xray-${XRAY_CORE_VERSION}"
  rm -rf "$unpack"
  mkdir -p "$unpack" "$XRAY_CORE_DIR"
  info "Downloading $asset from ${XRAY_CORE_REPO} v${XRAY_CORE_VERSION}…"
  curl -fL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 300 \
    -o "$archive" \
    "https://github.com/${XRAY_CORE_REPO}/releases/download/v${XRAY_CORE_VERSION}/${asset}" \
    || die "Failed to download Xray ${XRAY_CORE_VERSION}."

  actual_sha="$(sha256sum "$archive" | awk '{print $1}')"
  [[ "$actual_sha" == "$expected_sha" ]] \
    || die "Xray archive SHA-256 mismatch: expected $expected_sha, got $actual_sha"

  unzip -oj "$archive" xray -d "$unpack" >/dev/null \
    || die "Failed to extract xray from $asset."
  install -m 0755 "$unpack/xray" "$XRAY_CORE_BIN"
  actual_version="$("$XRAY_CORE_BIN" version 2>/dev/null | head -n 1 || true)"
  [[ "$actual_version" == "Xray ${XRAY_CORE_VERSION} "* ]] \
    || die "Installed Xray version mismatch: ${actual_version:-unreadable}"
  ok "Pinned Xray installed and verified: $actual_version"
}

# ── Decoy template (real masking site, not a bare stub) ─────────────────────
# Resolve a template argument: "builtin" | numeric id 1-N | folder name.
resolve_template() {
  local t="$1" i
  [[ "$t" == "builtin" ]] && { printf 'builtin'; return 0; }
  if [[ "$t" =~ ^[0-9]+$ ]]; then
    (( t >= 1 && t <= ${#TEMPLATE_FOLDERS[@]} )) && { printf '%s' "${TEMPLATE_FOLDERS[$((t-1))]}"; return 0; }
    return 1
  fi
  for i in "${TEMPLATE_FOLDERS[@]}"; do [[ "$i" == "$t" ]] && { printf '%s' "$t"; return 0; }; done
  return 1
}

# Generate a self-contained, per-install-unique business landing page into $dest.
# No external download and no public template to hash-match — the strongest form
# of "own decoy". Adapted from anfixit/routerus.
generate_builtin_site() {
  local dest="$1"
  info "Generating a built-in decoy landing (no external fetch)…"
  local THEMES=(
    "Web Development Studio|We build modern web applications|Web Development,Cloud Solutions,API Integration,DevOps Consulting"
    "Digital Marketing Agency|Data-driven marketing for growing brands|SEO Optimization,Content Strategy,PPC Management,Social Media"
    "Cloud Infrastructure|Enterprise-grade cloud hosting solutions|Managed Hosting,Auto Scaling,24/7 Monitoring,CDN Services"
    "Design Bureau|Creative solutions for digital products|UI/UX Design,Brand Identity,Motion Graphics,Print Design"
    "IT Consulting|Technology solutions for modern business|Infrastructure Audit,Security Assessment,Migration Planning,Team Training"
    "Software Solutions|Custom software for complex problems|Enterprise Apps,Mobile Development,Data Analytics,System Integration"
    "Network Services|Reliable connectivity for your business|Network Design,VoIP Solutions,Fiber Optics,Managed WiFi"
    "Data Analytics|Turn your data into actionable insights|Business Intelligence,Data Warehousing,ML Models,Dashboards"
  )
  local COLORS=(
    "#2563eb|#1e40af|#eff6ff" "#059669|#047857|#ecfdf5" "#7c3aed|#6d28d9|#f5f3ff"
    "#dc2626|#b91c1c|#fef2f2" "#0891b2|#0e7490|#ecfeff" "#d97706|#b45309|#fffbeb"
    "#4f46e5|#4338ca|#eef2ff" "#0d9488|#0f766e|#f0fdfa"
  )
  local BIZ_NAME BIZ_DESC BIZ_SERVICES COLOR1 COLOR2 BG_COLOR SITE_NAME YEAR
  IFS='|' read -r BIZ_NAME BIZ_DESC BIZ_SERVICES <<< "${THEMES[RANDOM % ${#THEMES[@]}]}"
  IFS='|' read -r COLOR1 COLOR2 BG_COLOR <<< "${COLORS[RANDOM % ${#COLORS[@]}]}"
  SITE_NAME="$(printf '%s' "$DOMAIN" | sed 's/\.[^.]*$//; s/[-_]/ /g' \
    | awk '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) tolower(substr($i,2))}1')"
  YEAR="$(date +%Y)"

  cat > "$dest/index.html" <<SITEEOF
<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${SITE_NAME} — ${BIZ_NAME}</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; color:#1f2937; background:#fff; }
.hero { background:linear-gradient(135deg,${COLOR1},${COLOR2}); color:#fff; padding:80px 20px; text-align:center; }
.hero h1 { font-size:2.5rem; font-weight:700; margin-bottom:1rem; }
.hero p { font-size:1.2rem; opacity:.9; max-width:600px; margin:0 auto; }
.container { max-width:960px; margin:0 auto; padding:60px 20px; }
.services { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:24px; margin-top:40px; }
.card { background:${BG_COLOR}; border-radius:12px; padding:24px; text-align:center; }
.card h3 { color:${COLOR1}; margin-bottom:8px; font-size:1.1rem; }
.card p { color:#6b7280; font-size:.9rem; line-height:1.5; }
.about { margin-top:60px; line-height:1.8; color:#4b5563; }
footer { text-align:center; padding:40px 20px; color:#9ca3af; font-size:.85rem; border-top:1px solid #f3f4f6; margin-top:60px; }
a { color:${COLOR1}; }
</style></head>
<body>
<div class="hero"><h1>${SITE_NAME}</h1><p>${BIZ_DESC}</p></div>
<div class="container">
<h2 style="text-align:center;font-size:1.8rem;">Our Services</h2>
<div class="services">
SITEEOF
  local svc
  IFS=',' read -ra svc <<< "$BIZ_SERVICES"
  local s
  for s in "${svc[@]}"; do
    cat >> "$dest/index.html" <<CARDEOF
<div class="card"><h3>${s}</h3><p>Professional ${s,,} services tailored to your business needs and goals.</p></div>
CARDEOF
  done
  cat >> "$dest/index.html" <<FOOTEOF
</div>
<div class="about"><h2 style="margin-bottom:16px;">About Us</h2>
<p>${SITE_NAME} is a team of experienced professionals delivering ${BIZ_NAME,,} services since 2019. We work with clients across Europe, helping them achieve their technology goals with modern, scalable solutions.</p>
<p style="margin-top:12px;">Based in Europe. Available worldwide. <a href="mailto:info@${DOMAIN}">Get in touch</a>.</p></div>
</div>
<footer>&copy; 2019-${YEAR} ${SITE_NAME}. All rights reserved. | <a href="mailto:info@${DOMAIN}">info@${DOMAIN}</a></footer>
</body></html>
FOOTEOF
  # tiny favicon so the tab isn't the default nginx one
  local hue=$(( RANDOM % 360 ))
  cat > "$dest/favicon.svg" <<SVG 2>/dev/null || true
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="hsl(${hue},68%,50%)"/></svg>
SVG
  ok "Built-in decoy generated: ${SITE_NAME} — ${BIZ_NAME}."
}

# Fetch a real decoy site (sni-templates/<folder>) into $dest. Primary path is the
# repo tarball (one request, no git); git sparse-checkout is the fallback.
fetch_template() {
  local folder="$1" dest="$2"
  local tb="$SCRATCH/templates.tgz" top="remnawave-scripts-main/sni-templates/${folder}"
  info "Fetching decoy template '$folder' from ${TEMPLATES_REPO}…"
  # Extract the one member directly and check the result — avoid `tar -tzf | grep`
  # which trips pipefail via SIGPIPE (grep -q closes the pipe, tar exits 141).
  if curl -fsSL --connect-timeout 15 --max-time 120 \
       "https://codeload.github.com/${TEMPLATES_REPO}/tar.gz/refs/heads/main" -o "$tb" 2>/dev/null; then
    tar -xzf "$tb" -C "$SCRATCH" "$top" 2>/dev/null || true
    if [[ -f "$SCRATCH/$top/index.html" ]]; then
      cp -a "$SCRATCH/$top/." "$dest/" && { ok "Template fetched ($(find "$dest" -type f | wc -l | tr -d ' ') files)."; return 0; }
    fi
  fi
  warn "Tarball fetch failed; trying git sparse-checkout…"
  if command -v git >/dev/null 2>&1; then
    local gd="$SCRATCH/tmpl-git"
    if git clone --depth 1 --filter=blob:none --sparse \
         "https://github.com/${TEMPLATES_REPO}.git" "$gd" >/dev/null 2>&1 \
       && ( cd "$gd" && git sparse-checkout set "sni-templates/${folder}" >/dev/null 2>&1 ) \
       && [[ -f "$gd/sni-templates/${folder}/index.html" ]]; then
      cp -a "$gd/sni-templates/${folder}/." "$dest/" && { ok "Template fetched via git."; return 0; }
    fi
  fi
  return 1
}

# Per-install byte-uniquification of a fetched template (anti-fingerprint):
# strip provenance, randomise brand/title/description, hue-shift colors, add byte
# noise, neutralise the api.ipify.org phone-home beacon, fix the Vite favicon.
# Ported from DigneZzZ selfsteal.sh so the served decoy never hash-matches the
# public sni-templates repo.
randomize_template() {
  local dir="$1"; [[ -d "$dir" ]] || return 0
  _hex() { openssl rand -hex "${1:-4}" 2>/dev/null || echo "$RANDOM$RANDOM"; }
  _pick() { local a=("$@"); echo "${a[RANDOM % ${#a[@]}]}"; }
  info "Mutating template for per-install uniqueness (anti-fingerprint)…"

  find "$dir" -type f \( -iname '*.md' -o -iname '*.markdown' -o -iname 'LICENSE' \
    -o -iname 'LICENSE.*' -o -iname '*.map' -o -iname '.gitignore' -o -iname '.gitattributes' \) \
    -delete 2>/dev/null || true

  local adjs=(Swift Bright Lumen Nimbus Vivid Prime Atlas Pulse Nova Quartz Onyx Vertex Cobalt Ember Drift Solace Zephyr Apex Halcyon Meridian Aero Cedar Indigo Mistral)
  local nouns=(Cloud Vault Hub Forge Works Studio Labs Stream Desk Space Grid Port Wave Loop Stack Nest Spark Core Pixel Harbor Bay Field Crest Point)
  local brand short new_title new_desc
  brand="$(_pick "${adjs[@]}") $(_pick "${nouns[@]}")"; short="${brand%% *}"
  new_title="$(_pick "$brand" "$brand — $(_pick Home Dashboard Portal Online Service Cloud Center Access)" "$short · $(_pick Files Media Tools Hub Suite)")"
  new_desc="$(_pick "Fast, simple and secure." "Your files, anywhere you go." "Built for speed and privacy." "Reliable service, every day." "Modern tools that just work." "Simple. Fast. Yours.")"
  local deg=$(( (RANDOM % 300) + 30 )) sat=$(( (RANDOM % 30) + 90 )) cssv; cssv="$(_hex 3)"

  local f
  while IFS= read -r -d '' f; do
    sed -i "s#<title>[^<]*</title>#<title>${new_title}</title>#" "$f" 2>/dev/null || true
    sed -i "s#MyWebSite#${brand}#g; s#MySite#${short}#g" "$f" 2>/dev/null || true
    sed -i "s#\(name=[\"']description[\"'][^>]*content=\)[\"'][^\"']*[\"']#\1\"${new_desc}\"#Ig" "$f" 2>/dev/null || true
    sed -i "/fonts\.googleapis\.com/d; /fonts\.gstatic\.com/d" "$f" 2>/dev/null || true
    sed -i "s#https\?://api\.ipify\.org[^\"')]*#/_s/ip#g" "$f" 2>/dev/null || true
    sed -i "s#/vite\.svg#/favicon.svg#g" "$f" 2>/dev/null || true
    sed -i -E "s#((href|src)=\"[^\"]*\.(css|js))(\?[^\"]*)?\"#\1?v=${cssv}\"#g" "$f" 2>/dev/null || true
    sed -i "s#</head>#<style>html{filter:hue-rotate(${deg}deg) saturate(${sat}%)}img,picture,video,svg,canvas{filter:hue-rotate(-${deg}deg)}</style><!-- $(_hex 6) --></head>#I" "$f" 2>/dev/null || true
  done < <(find "$dir" -type f -iname '*.html' -print0 2>/dev/null)

  while IFS= read -r -d '' f; do
    sed -i "s#https\?://api\.ipify\.org[^\"')]*#/_s/ip#g" "$f" 2>/dev/null || true
    printf '\n/* %s */\n' "$(_hex 6)" >> "$f" 2>/dev/null || true
  done < <(find "$dir" -type f \( -iname '*.css' -o -iname '*.js' \) -print0 2>/dev/null)

  local hue=$(( RANDOM % 360 ))
  cat > "$dir/favicon.svg" <<SVG 2>/dev/null || true
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="hsl(${hue},68%,50%)"/><circle cx="32" cy="32" r="13" fill="hsl($(( (hue + 45) % 360 )),72%,90%)"/></svg>
SVG

  while IFS= read -r -d '' f; do
    sed -i "s#MyWebSite#${brand}#g; s#MySite#${short}#g" "$f" 2>/dev/null || true
  done < <(find "$dir" -type f \( -iname '*.webmanifest' -o -iname 'manifest.json' \) -print0 2>/dev/null)
  ok "Template mutated (brand: ${brand}, hue +${deg}°)."
}

# Populate $NGINX_DIR/html with a real decoy site, or a minimal stub as a last
# resort (a bare stub is a masking giveaway — we warn loudly if it happens).
setup_decoy_content() {
  local html="$NGINX_DIR/html" folder
  run mkdir -p "$html"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: fetch template '$TEMPLATE' into $html + randomize=$RANDOMIZE"
    return
  fi
  # Resume keeps the already-served decoy stable (regenerating would silently swap
  # the visible page). --refresh-decoy forces a fresh one.
  if [[ "$RESUME" == "1" && "$REFRESH_DECOY" != "1" && -f "$html/index.html" ]]; then
    info "Resume: keeping existing decoy ($html/index.html) — pass --refresh-decoy to regenerate."
    return
  fi
  folder="$(resolve_template "$TEMPLATE")" || die "Unknown template '$TEMPLATE'. Valid: builtin | ${TEMPLATE_FOLDERS[*]}"
  rm -rf "${html:?}/"* 2>/dev/null || true
  # builtin: self-contained generator (default) — no network, always unique.
  if [[ "$folder" == "builtin" ]]; then
    generate_builtin_site "$html"
    return
  fi
  # otherwise fetch a real sni-template; on failure fall back to the built-in
  # generator (still a believable decoy, no bare stub).
  if fetch_template "$folder" "$html"; then
    [[ "$RANDOMIZE" == "1" ]] && randomize_template "$html"
  else
    warn "Template fetch failed — falling back to the built-in generator."
    generate_builtin_site "$html"
  fi
}

# ── grpc-tls mask: nginx owns public 443 (real cert), gRPC behind TLS ───────
# client -> domain:443 TLS/h2 -> nginx -> 127.0.0.1:GRPC_PORT -> Xray grpc inbound.
# Everything that is not the gRPC service path is served the real decoy site, so a
# prober hitting the domain sees a genuine HTTPS site with a valid certificate.
# Adapted from NikitaAzmov/GRPC.
write_selfsteal_grpc() {
  step "Nginx gRPC front (public :443 TLS, real cert, gRPC -> 127.0.0.1:${GRPC_PORT})"
  run mkdir -p "$NGINX_DIR/conf.d" "$NGINX_DIR/ssl" "$NGINX_DIR/html" "$NGINX_DIR/logs"

  setup_decoy_content   # real masking site into $NGINX_DIR/html (served by nginx itself)

  if [[ "$DRY_RUN" != "1" ]]; then
    cat > "$NGINX_DIR/nginx.conf" <<'CONF'
user root;
worker_processes auto;
worker_rlimit_nofile 1048576;
error_log /var/log/nginx/error.log warn;
pid /var/run/nginx.pid;
events { worker_connections 65535; multi_accept on; }
http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  sendfile on;
  tcp_nopush on;
  tcp_nodelay on;
  keepalive_timeout 65;
  server_tokens off;
  ssl_protocols TLSv1.2 TLSv1.3;
  ssl_prefer_server_ciphers off;
  client_max_body_size 0;
  large_client_header_buffers 8 32k;
  include /etc/nginx/conf.d/*.conf;
}
CONF

    cat > "$NGINX_DIR/conf.d/selfsteal.conf" <<CONF
upstream xray_grpc {
    server 127.0.0.1:${GRPC_PORT};
    keepalive 256;
}

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name ${DOMAIN};
    location ^~ /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}

server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    http2 on;
    server_name ${DOMAIN};

    ssl_certificate     /etc/nginx/ssl/fullchain.crt;
    ssl_certificate_key /etc/nginx/ssl/private.key;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    access_log /var/log/nginx/access.log;
    error_log  /var/log/nginx/error.log warn;

    root /var/www/html;
    index index.html;

    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;

    location = /health {
        default_type application/json;
        add_header Cache-Control "no-store" always;
        return 200 '{"status":"ok"}';
    }

    # gRPC tunnel -> Xray. Only this exact service path is proxied; anything else
    # falls through to the decoy site below.
    location ^~ /${GRPC_SERVICE}/Tun {
        grpc_pass grpc://xray_grpc;
        grpc_set_header Host \$host;
        grpc_set_header X-Real-IP \$remote_addr;
        grpc_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        grpc_set_header X-Forwarded-Proto https;
        grpc_read_timeout 900s;
        grpc_send_timeout 900s;
        grpc_connect_timeout 30s;
    }

    location / { try_files \$uri \$uri/ /index.html; }
    location ~* \.(css|js|png|jpg|jpeg|gif|ico|svg|woff2?)\$ {
        expires 30d; add_header Cache-Control "public, immutable";
    }
}
CONF

    cat > "$NGINX_DIR/docker-compose.yml" <<CONF
services:
  ${NGINX_CONTAINER}:
    image: ${NGINX_IMAGE}
    container_name: ${NGINX_CONTAINER}
    restart: always
    network_mode: host
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./conf.d:/etc/nginx/conf.d:ro
      - ./ssl:/etc/nginx/ssl:ro
      - ./html:/var/www/html:ro
      - ./logs:/var/log/nginx
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
CONF
  fi
  ok "gRPC front files written to $NGINX_DIR."
}

# ── Selfsteal (nginx: unix socket by default, or TCP loopback) ──────────────
write_selfsteal() {
  if [[ "$MASK" == "grpc-tls" ]]; then write_selfsteal_grpc; return; fi
  local endpoint shm_vol=""
  if [[ "$MODE" == "tcp" ]]; then
    endpoint="127.0.0.1:${SELFSTEAL_PORT}"
    step "Nginx selfsteal (TCP mode, ${endpoint})"
  else
    endpoint="unix:${SOCKET_PATH}"
    shm_vol=$'\n      - /dev/shm:/dev/shm'
    step "Nginx selfsteal (socket mode, ${SOCKET_PATH})"
  fi
  run mkdir -p "$NGINX_DIR/conf.d" "$NGINX_DIR/ssl" "$NGINX_DIR/html" "$NGINX_DIR/logs"

  setup_decoy_content   # real masking site into $NGINX_DIR/html (not a bare stub)

  if [[ "$DRY_RUN" != "1" ]]; then
    cat > "$NGINX_DIR/nginx.conf" <<'CONF'
user root;
worker_processes auto;
error_log /var/log/nginx/error.log warn;
pid /var/run/nginx.pid;
events { worker_connections 1024; }
http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  sendfile on;
  keepalive_timeout 65;
  server_tokens off;
  include /etc/nginx/conf.d/*.conf;
}
CONF

    cat > "$NGINX_DIR/conf.d/selfsteal.conf" <<CONF
# Xray Reality forwards fallback traffic here with xver: 1 (PROXY protocol v1).
# Endpoint: ${endpoint} (socket via /dev/shm by default, or loopback TCP).

# Default server: any connection whose SNI does NOT match our domain gets its
# TLS handshake rejected — exactly how a real host behaves for an unknown vhost,
# so a prober cannot fish the decoy with an arbitrary SNI (anti-DPI defence).
server {
    listen ${endpoint} ssl proxy_protocol default_server;
    http2 on;
    ssl_reject_handshake on;
}

# Real decoy for our selfsteal domain.
server {
    listen ${endpoint} ssl proxy_protocol;
    http2 on;
    server_name ${DOMAIN};

    ssl_certificate     /etc/nginx/ssl/fullchain.crt;
    ssl_certificate_key /etc/nginx/ssl/private.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    access_log /var/log/nginx/access.log;
    error_log  /var/log/nginx/error.log warn;

    root /var/www/html;
    index index.html;

    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;

    location / { try_files \$uri \$uri/ /index.html; }
    location ~* \.(css|js|png|jpg|jpeg|gif|ico|svg|woff2?)\$ {
        expires 30d; add_header Cache-Control "public, immutable";
    }
}
CONF

    cat > "$NGINX_DIR/docker-compose.yml" <<CONF
services:
  ${NGINX_CONTAINER}:
    image: ${NGINX_IMAGE}
    container_name: ${NGINX_CONTAINER}
    restart: always
    network_mode: host
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./conf.d:/etc/nginx/conf.d:ro
      - ./ssl:/etc/nginx/ssl:ro
      - ./html:/var/www/html:ro
      - ./logs:/var/log/nginx${shm_vol}
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
CONF
  fi
  ok "Selfsteal files written to $NGINX_DIR."
}
start_selfsteal() {
  info "Starting nginx selfsteal…"
  run bash -c "cd '$NGINX_DIR' && docker compose up -d"
  ok "Selfsteal running."
}

# ── Certificate ─────────────────────────────────────────────────────────────
install_acme() {
  if [[ -f "$ACME_HOME/acme.sh" ]]; then return; fi
  info "Installing acme.sh…"
  if [[ "$DRY_RUN" == "1" ]]; then info "DRY-RUN: curl https://get.acme.sh | sh -s email=$ACME_EMAIL"; return; fi
  local s="$SCRATCH/acme-install.sh"
  curl -fsSL --connect-timeout 15 --max-time 120 https://get.acme.sh -o "$s" || die "Failed to download acme.sh installer."
  sh "$s" email="$ACME_EMAIL" >/dev/null 2>&1 || die "acme.sh installation failed."
  "$ACME_HOME/acme.sh" --set-default-ca --server letsencrypt >/dev/null 2>&1 || true
}
issue_cert_le443() {
  step "Certificate — Let's Encrypt (TLS-ALPN on 443)"
  install_acme
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: acme.sh --issue --alpn --tlsport 443 -d $DOMAIN ; install-cert to $NGINX_DIR/ssl"
    return
  fi
  if cert_is_valid; then
    info "Certificate already valid >30d — skipping issuance, refreshing renewal setup."
  elif install_cached_cert_files; then
    info "Existing acme.sh certificate installed into $NGINX_DIR/ssl."
  else
    if port_listening 443; then
      # In reality mode Xray (node) owns 443; in grpc-tls nginx owns it.
      local busyc="$NODE_CONTAINER"
      [[ "$MASK" == "grpc-tls" ]] && busyc="$NGINX_CONTAINER"
      # Capture first: piping docker ps straight into `grep -qx` risks a
      # SIGPIPE+pipefail false negative (see port_listening) — here that would
      # skip stopping the 443 owner and let acme --issue fail on a busy port.
      local running; running="$(docker ps --format '{{.Names}}' 2>/dev/null)"
      if grep -qx "$busyc" <<<"$running"; then
        warn "Port 443 busy — stopping $busyc for issuance."
        # Record it BEFORE stopping so the EXIT trap restarts it if issuance fails.
        CERT_STOPPED_CONTAINER="$busyc"
        docker stop "$busyc" >/dev/null
        # Poll instead of a fixed sleep: a loaded host can take >2s to release the
        # socket, which previously caused a false "still busy" die.
        local _i; for _i in {1..10}; do port_listening 443 || break; sleep 1; done
      fi
      port_listening 443 && die "Port 443 still busy; free it and re-run."
    fi
    if "$ACME_HOME/acme.sh" --issue --alpn --tlsport 443 -d "$DOMAIN" \
      --keylength ec-256 --server letsencrypt; then
      install_cert_files || die "acme.sh --install-cert failed."
    elif install_cached_cert_files; then
      warn "acme.sh skipped issuance/renewal, but a valid cached certificate was installed."
    else
      die "Certificate issuance on 443 failed (check DNS A-record for $DOMAIN)."
    fi
    # NOTE: do NOT clear CERT_STOPPED_CONTAINER here. Several steps still run
    # before the replacement 443-owner is back up (setup_panel_resources,
    # setup_squad, setup_geo, write_node, …); if any fails, the EXIT trap must
    # still restart the stopped container. It is cleared only once the new owner
    # has started — start_selfsteal (grpc-tls) or start_node (reality).
  fi
  # Renewal cannot use 443 (Xray owns it in production): renew on RENEW_PORT
  # behind a temporary iptables redirect 443 -> RENEW_PORT.
  local conf
  conf="$(cert_conf_path)"
  if [[ -n "$conf" ]]; then
    if grep -q '^Le_TLSPort=' "$conf"; then
      sed "s|^Le_TLSPort=.*|Le_TLSPort='$RENEW_PORT'|" "$conf" > "$conf.p" && mv "$conf.p" "$conf"
    else
      printf "Le_TLSPort='%s'\n" "$RENEW_PORT" >> "$conf"
    fi
  fi
  write_renew_wrapper_le443
  ok "Certificate issued; renewal set to port $RENEW_PORT via 443 redirect."
}
issue_cert_cfdns() {
  step "Certificate — Cloudflare DNS-01 (wildcard *.$DOMAIN)"
  [[ -n "$CF_TOKEN" ]] || die "cf-dns mode needs --cf-token (Cloudflare API token, Zone:DNS:Edit)."
  install_acme
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: CF_Token=*** acme.sh --issue --dns dns_cf -d $DOMAIN -d *.$DOMAIN"
    return
  fi
  if cert_is_valid; then
    info "Certificate already valid >30d — skipping issuance, refreshing renewal setup."
  elif install_cached_cert_files; then
    info "Existing acme.sh certificate installed into $NGINX_DIR/ssl."
  else
    if CF_Token="$CF_TOKEN" "$ACME_HOME/acme.sh" --issue --dns dns_cf \
      -d "$DOMAIN" -d "*.$DOMAIN" --keylength ec-256 --server letsencrypt; then
      install_cert_files || die "acme.sh --install-cert failed."
    elif install_cached_cert_files; then
      warn "acme.sh skipped issuance/renewal, but a valid cached certificate was installed."
    else
      die "Cloudflare DNS-01 issuance failed (check CF token scope / zone)."
    fi
  fi
  write_renew_wrapper_cfdns
  ok "Wildcard certificate issued; renewal via Cloudflare DNS (no port needed)."
}
cert_is_valid() {
  # True if the installed cert exists and stays valid for > 30 days.
  local f="$NGINX_DIR/ssl/fullchain.crt"
  [[ -f "$f" ]] && openssl x509 -in "$f" -checkend $((30*86400)) -noout >/dev/null 2>&1
}
cert_conf_path() {
  local d
  for d in "$ACME_HOME/${DOMAIN}_ecc" "$ACME_HOME/${DOMAIN}"; do
    [[ -f "$d/$DOMAIN.conf" ]] && { printf '%s' "$d/$DOMAIN.conf"; return; }
  done
}
install_cached_cert_files() {
  # After a local reinstall /opt/nginx-selfsteal/ssl may be gone while acme.sh
  # still has a valid order and refuses a no-op --issue. Reinstall that cached
  # cert first; if it is missing/expired, the caller will perform a real issue.
  local conf
  conf="$(cert_conf_path)"
  [[ -n "$conf" ]] || return 1
  install_cert_files || return 1
  cert_is_valid
}
install_cert_files() {
  mkdir -p "$NGINX_DIR/ssl"
  "$ACME_HOME/acme.sh" --install-cert -d "$DOMAIN" --ecc \
    --fullchain-file "$NGINX_DIR/ssl/fullchain.crt" \
    --key-file "$NGINX_DIR/ssl/private.key" \
    --reloadcmd "docker exec $NGINX_CONTAINER nginx -s reload 2>/dev/null || true" \
    >/dev/null || return 1
  chmod 600 "$NGINX_DIR/ssl/private.key"
  chmod 644 "$NGINX_DIR/ssl/fullchain.crt"
}
write_renew_wrapper_le443() {
  local w="$NGINX_DIR/acme-renew.sh"
  cat > "$w" <<WRAP
#!/usr/bin/env bash
# Renew via TLS-ALPN. Validation hits 443 (Xray owns it) -> redirect to $RENEW_PORT.
set -u
iptables -t nat -I PREROUTING 1 -p tcp --dport 443 -j REDIRECT --to-port $RENEW_PORT 2>/dev/null || true
iptables -t nat -I OUTPUT 1 -p tcp --dport 443 -o lo -j REDIRECT --to-port $RENEW_PORT 2>/dev/null || true
rc=0
"$ACME_HOME/acme.sh" --cron --home "$ACME_HOME" || rc=\$?
iptables -t nat -D PREROUTING -p tcp --dport 443 -j REDIRECT --to-port $RENEW_PORT 2>/dev/null || true
iptables -t nat -D OUTPUT -p tcp --dport 443 -o lo -j REDIRECT --to-port $RENEW_PORT 2>/dev/null || true
exit \$rc
WRAP
  chmod 700 "$w"
  install_renew_cron "$w"
}
write_renew_wrapper_cfdns() {
  local w="$NGINX_DIR/acme-renew.sh"
  cat > "$w" <<WRAP
#!/usr/bin/env bash
set -u
export CF_Token='$CF_TOKEN'
exec "$ACME_HOME/acme.sh" --cron --home "$ACME_HOME"
WRAP
  chmod 700 "$w"
  install_renew_cron "$w"
}
# Idempotently install one cron line. Drops any prior line containing the
# fixed-string $match (and, if given, lines matching the ERE $extra), then writes
# the crontab once from memory. Building it in memory instead of
# `crontab -l | grep -v … | crontab -` avoids the `set -euo pipefail` abort when
# the crontab is empty or grep -v filters out every line.
install_cron_once() {
  local match="$1" line="$2" label="${3:-cron}" extra="${4:-}" current filtered
  # An empty $match makes `grep -vF ""` drop the WHOLE existing crontab (it would
  # be replaced by just $line). Guard against a caller passing an empty pattern.
  [[ -n "$match" && -n "$line" ]] || { warn "cron: empty match/line — refusing to rewrite crontab"; return 0; }
  command -v crontab >/dev/null 2>&1 || { warn "crontab missing; add manually: $line"; return 0; }
  current="$(crontab -l 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$current" | grep -vF "$match" || true)"
  [[ -n "$extra" ]] && filtered="$(printf '%s\n' "$filtered" | grep -vE "$extra" || true)"
  if { printf '%s\n' "$filtered" | sed '/^[[:space:]]*$/d'; printf '%s\n' "$line"; } | crontab -; then
    ok "$label"
  else
    warn "Could not install cron ($label). Add manually: $line"
  fi
}
install_renew_cron() {
  local w="$1"
  # Also drop the acme.sh-installed direct --cron job (renews without our 443
  # redirect / CF env) alongside any prior copy of our own wrapper entry.
  install_cron_once "$w" "0 4 * * * $w" "Auto-renewal cron installed (daily 04:00): $w" '\.acme\.sh.*--cron'
}
issue_certificate() {
  case "$CERT_MODE" in
    le443)  issue_cert_le443 ;;
    cf-dns) issue_cert_cfdns ;;
    *) die "Unknown cert mode: $CERT_MODE" ;;
  esac
}

# ── Node container ──────────────────────────────────────────────────────────
write_node() {
  step "Remnawave node container"
  [[ -n "$NODE_SECRET_KEY" ]] || die "SECRET_KEY empty — panel API step must run first."
  run mkdir -p "$NODE_DIR/logs"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: write $NODE_DIR/.env (NODE_PORT=$NODE_PORT, SECRET_KEY=***) + docker-compose.yml"
    return
  fi
  backup_file "$NODE_DIR/.env"
  # env_file wants single-line values: collapse PEM newlines to literal \n.
  local secret
  secret="$(printf '%s' "$NODE_SECRET_KEY" | awk 'BEGIN{ORS=""} NR>1{printf "\\n"} {printf "%s",$0}')"
  cat > "$NODE_DIR/.env" <<EOF
### Remnawave node — generated $(date -Iseconds) ###
NODE_PORT=${NODE_PORT}
SECRET_KEY=${secret}
EOF
  chmod 600 "$NODE_DIR/.env"

  # In reality socket mode Xray must reach the nginx unix socket via shared
  # /dev/shm. grpc-tls uses loopback TCP, so no shared memory is needed.
  local shm_vol=""
  [[ "$MASK" == "reality" && "$MODE" != "tcp" ]] && shm_vol=$'\n      - /dev/shm:/dev/shm'
  # Fresh RU geosite/geoip mounted into Xray's asset dir.
  local geo_vol=""
  [[ "$GEO" == "1" && -s "$GEO_DIR/geosite.dat" ]] && geo_vol=$'\n      - '"$GEO_DIR"$'/geosite.dat:/usr/local/share/xray/geosite.dat:ro\n      - '"$GEO_DIR"$'/geoip.dat:/usr/local/share/xray/geoip.dat:ro'

  cat > "$NODE_DIR/docker-compose.yml" <<EOF
services:
  ${NODE_CONTAINER}:
    image: ${NODE_IMAGE}
    container_name: ${NODE_CONTAINER}
    hostname: ${NODE_CONTAINER}
    env_file: [ .env ]
    network_mode: host
    restart: always
    cap_add: [ NET_ADMIN ]
    ulimits:
      nofile: { soft: 1048576, hard: 1048576 }
    volumes:
      - ${XRAY_CORE_BIN}:/usr/local/bin/xray:ro
      - ${NGINX_DIR}/ssl:/etc/xray/cert:ro
      - ${NODE_DIR}/logs:/var/log/xray${shm_vol}${geo_vol}
    logging:
      driver: json-file
      options: { max-size: "20m", max-file: "5" }
EOF
  ok "Node files written to $NODE_DIR."
}
start_node() {
  info "Starting node container…"
  run bash -c "cd '$NODE_DIR' && docker compose up -d --force-recreate"
  ok "Node running."
}

# Download updated RU geosite/geoip (runetfreedom) so the routing rules
# (geosite:*, geoip:*) use fresh, RU-tuned data instead of the image's bundled
# set. Files are bind-mounted into Xray's asset dir; a daily cron refreshes them.
setup_geo() {
  [[ "$GEO" == "1" ]] || return 0
  step "Geo lists (runetfreedom)"
  GEO_DIR="$NODE_DIR/geodata"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: download geosite.dat/geoip.dat from $GEO_REPO into $GEO_DIR + daily cron"
    return
  fi
  mkdir -p "$GEO_DIR"
  # Already have a complete pair (e.g. from an earlier run / resume)? Keep it and
  # skip the (large) re-download entirely.
  if [[ -s "$GEO_DIR/geosite.dat" && -s "$GEO_DIR/geoip.dat" ]]; then
    info "Geo lists already present ($(du -h "$GEO_DIR/geosite.dat" | cut -f1) + $(du -h "$GEO_DIR/geoip.dat" | cut -f1)) — keeping them."
    printf 'enabled\n' > "$STATE_DIR/geo.state" 2>/dev/null || true
    install_geo_cron; return 0
  fi
  local base="https://raw.githubusercontent.com/${GEO_REPO}/release" ok_all=1 f
  # Overall time budget for the whole stage so a slow mirror can't stall the install
  # for many minutes with no output; each file gets whatever budget remains.
  local deadline=$(( SECONDS + GEO_TIMEOUT ))
  info "Downloading RU geosite/geoip (resumable; overall budget ${GEO_TIMEOUT}s, --geo-timeout to change)…"
  for f in geosite geoip; do
    local remain=$(( deadline - SECONDS ))
    if (( remain <= 5 )); then
      warn "Geo time budget (${GEO_TIMEOUT}s) exhausted — skipping ${f}.dat."; ok_all=0; break
    fi
    local tmp="$GEO_DIR/${f}.dat.tmp" resume=""
    [[ -s "$tmp" ]] && resume=", resuming partial $(du -h "$tmp" 2>/dev/null | cut -f1)"
    info "  ${f}.dat — max ${remain}s${resume}"
    # Short connect timeout, per-file cap = remaining budget, retry transient
    # errors, resume a partial .tmp (-C -) instead of restarting.
    if curl -fsSL -C - --connect-timeout 15 --max-time "$remain" \
         --retry 4 --retry-delay 3 --retry-all-errors \
         "$base/${f}.dat" -o "$tmp" \
       && [[ -s "$tmp" ]]; then
      mv "$tmp" "$GEO_DIR/${f}.dat"
      ok "  ${f}.dat: $(du -h "$GEO_DIR/${f}.dat" | cut -f1)"
    else
      rm -f "$tmp"; ok_all=0
      warn "  Failed to download ${f}.dat — Xray will keep its bundled geo data."
    fi
  done
  if [[ "$ok_all" != "1" || ! -s "$GEO_DIR/geosite.dat" || ! -s "$GEO_DIR/geoip.dat" ]]; then
    # Never mount a partial file — remove any leftover .tmp and record the state so
    # write_node mounts nothing and resume can retry geo next run.
    rm -f "$GEO_DIR"/*.dat.tmp 2>/dev/null || true
    warn "Geo lists incomplete — skipping the mount (Xray keeps its bundled geo data). Re-run with --resume to retry."
    GEO=0
    printf 'disabled\n' > "$STATE_DIR/geo.state" 2>/dev/null || true
    return 0
  fi
  printf 'enabled\n' > "$STATE_DIR/geo.state" 2>/dev/null || true
  install_geo_cron
}
# Daily refresh + node restart so Xray reloads the new data. Split out so the
# "geo already present" fast path reinstalls the cron without re-downloading.
install_geo_cron() {
  local upd="$STATE_DIR/update-geo.sh"
  mkdir -p "$STATE_DIR"
  # Size floor: a truncated download or an error page is far smaller than any
  # real .dat (geoip ~10MB, geosite ~1MB). Never replace a good file with junk,
  # and only bounce the node when a file actually changed — an unconditional
  # nightly restart would cut every live client for nothing. `--force-recreate`
  # (not `docker restart`) because single-file bind mounts can keep serving the
  # pre-`mv` inode across a plain restart.
  cat > "$upd" <<EOF
#!/usr/bin/env bash
set -u
base="https://raw.githubusercontent.com/${GEO_REPO}/release"
min_size=100000
changed=0
for f in geosite geoip; do
  tmp="${GEO_DIR}/\${f}.dat.tmp" dst="${GEO_DIR}/\${f}.dat"
  if curl -fsSL --max-time 120 "\$base/\${f}.dat" -o "\$tmp" \\
      && [ -s "\$tmp" ] && [ "\$(stat -c%s "\$tmp")" -ge "\$min_size" ]; then
    if ! cmp -s "\$tmp" "\$dst" 2>/dev/null; then
      mv "\$tmp" "\$dst"; changed=1
    else
      rm -f "\$tmp"
    fi
  else
    rm -f "\$tmp"
  fi
done
if [ "\$changed" = "1" ]; then
  cd "${NODE_DIR}" && docker compose up -d --force-recreate >/dev/null 2>&1
fi
EOF
  chmod 700 "$upd"
  install_cron_once "$upd" "0 3 * * * $upd" "Geo auto-update cron installed (daily 03:00, restart only on change)."
}

# ── Panel lookups (idempotency) ─────────────────────────────────────────────
# Finders print the single matching UUID (empty when none → caller creates). On
# MULTIPLE matches they print the candidate UUIDs to stderr and return 3 so the
# caller aborts instead of silently patching an arbitrary first object.
panel_find_profile_uuid() {
  local name="$1" r uuids
  r="$(panel_req GET /api/config-profiles 2>/dev/null)" || return 0
  uuids="$(printf '%s' "$r" | jq -r --arg n "$name" \
    '.response.configProfiles[]? | select(.name == $n) | .uuid')"
  (( $(grep -c . <<<"$uuids") > 1 )) && {
    printf 'AMBIGUOUS: %s config profiles named "%s": %s\n' \
      "$(grep -c . <<<"$uuids")" "$name" "$(tr '\n' ' ' <<<"$uuids")" >&2; return 3; }
  printf '%s' "$(head -n1 <<<"$uuids")"
}
panel_find_profile_uuid_by_inbound_tags() {
  # Remnawave requires inbound tags to be globally unique. If another profile
  # already owns one of our tags, creating a new profile with that tag fails.
  # Prints "uuid<TAB>profile-name<TAB>tag" of the owner (empty when none) — the
  # CALLER decides whether adopting it is allowed. Updating a foreign profile in
  # place would replace its whole config and hijack whatever nodes it serves.
  local r spec tag line
  r="$(panel_req GET /api/config-profiles 2>/dev/null)" || return 0
  for spec in "${ACTIVE_INBOUNDS[@]}"; do
    IFS=: read -r tag _ _ <<< "$spec"
    line="$(printf '%s' "$r" | jq -r --arg t "$tag" \
      '[.response.configProfiles[]?
        | select([(.inbounds // .config.inbounds // [])[]?.tag] | index($t))
        | "\(.uuid)\t\(.name)\t\($t)"] | .[0] // empty')"
    if [[ -n "$line" && "$line" != "null" ]]; then
      printf '%s' "$line"
      return 0
    fi
  done
  return 0
}
panel_find_node_uuid() {
  # Match by exact node name; fall back to node address. Ambiguity in whichever
  # dimension actually matched aborts (return 3) rather than picking the first.
  local name="$1" addr="$2" r uuids
  r="$(panel_req GET /api/nodes 2>/dev/null)" || return 0
  uuids="$(printf '%s' "$r" | jq -r --arg n "$name" '.response[]? | select(.name == $n) | .uuid')"
  if [[ -z "$uuids" ]]; then
    uuids="$(printf '%s' "$r" | jq -r --arg a "$addr" '.response[]? | select(.address == $a) | .uuid')"
    (( $(grep -c . <<<"$uuids") > 1 )) && {
      printf 'AMBIGUOUS: %s nodes at address %s: %s\n' \
        "$(grep -c . <<<"$uuids")" "$addr" "$(tr '\n' ' ' <<<"$uuids")" >&2; return 3; }
  else
    (( $(grep -c . <<<"$uuids") > 1 )) && {
      printf 'AMBIGUOUS: %s nodes named "%s": %s\n' \
        "$(grep -c . <<<"$uuids")" "$name" "$(tr '\n' ' ' <<<"$uuids")" >&2; return 3; }
  fi
  printf '%s' "$(head -n1 <<<"$uuids")"
}
panel_find_host_uuid() {
  # Match a host by remark + address (hosts have no unique name field).
  local remark="$1" addr="$2" r uuids
  r="$(panel_req GET /api/hosts 2>/dev/null)" || return 0
  uuids="$(printf '%s' "$r" | jq -r --arg m "$remark" --arg a "$addr" \
    '.response[]? | select(.remark == $m and .address == $a) | .uuid')"
  (( $(grep -c . <<<"$uuids") > 1 )) && {
    printf 'AMBIGUOUS: %s hosts with remark "%s" @ %s: %s\n' \
      "$(grep -c . <<<"$uuids")" "$remark" "$addr" "$(tr '\n' ' ' <<<"$uuids")" >&2; return 3; }
  printf '%s' "$(head -n1 <<<"$uuids")"
}
# ── Cascade bridge: panel user ↔ shared SS secret ───────────────────────────
# Resolve the SS bridge password from the panel user BRIDGE_USER. If the user
# already exists, reuse its ssPassword (so re-runs stay stable and an existing
# cascade keeps working); otherwise mint a fresh 32-char secret to create it with.
# Sets BRIDGE_SS_PASSWORD and BRIDGE_USER_EXISTS.
BRIDGE_USER_EXISTS=0
panel_resolve_bridge_password() {
  [[ "$BRIDGE" == "1" ]] || return 0
  local existing pw
  # GET /api/users/by-username/<name> → 200 with the user (incl. ssPassword) or 404.
  if existing="$(panel_req GET "/api/users/by-username/${BRIDGE_USER}" 2>/dev/null)"; then
    pw="$(printf '%s' "$existing" | jq -r '.response.ssPassword // empty' 2>/dev/null || echo '')"
    if [[ -n "$pw" ]]; then
      BRIDGE_SS_PASSWORD="$pw"; BRIDGE_USER_EXISTS=1
      ok "Bridge user '$BRIDGE_USER' exists — reusing its ssPassword as the bridge secret."
      return 0
    fi
    # User exists but returned no ssPassword (unexpected) — cannot derive a stable
    # secret; refuse rather than silently minting one the entry side won't share.
    die "User '$BRIDGE_USER' exists but has no ssPassword in the API response — pick another --bridge-user."
  fi
  # 404 (or unreachable): mint a fresh secret. Hex, 32 chars — within SS 8..32.
  BRIDGE_SS_PASSWORD="$(openssl rand -hex 16)"
  BRIDGE_USER_EXISTS=0
  ok "Bridge user '$BRIDGE_USER' is new — generated a fresh ssPassword (will create the user)."
}
# Create BRIDGE_USER (if new) with the resolved ssPassword, or patch an existing
# one, and attach it to the node's Internal Squad so it can actually use the exit.
# 99-year expiry, unlimited traffic, ACTIVE — a service account for the cascade.
panel_ensure_bridge_user() {
  [[ "$BRIDGE" == "1" ]] || return 0
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: GET /api/users/by-username/$BRIDGE_USER; POST or PATCH /api/users (ssPassword, squad ${SQUAD_UUID:-<node squad>})"
    return 0
  fi
  # Squad to attach: prefer the one setup_squad resolved. Fall back to none.
  local squad_json="[]"
  [[ -n "$BRIDGE_SQUAD_UUID" ]] && squad_json="$(jq -cn --arg u "$BRIDGE_SQUAD_UUID" '[$u]')"
  # ISO expireAt ~99 years out. Date is unavailable in some restricted contexts, so
  # compute from the panel-independent `date` on the (Linux) node — always present here.
  local expire; expire="$(date -u -d '+99 years' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
  local body
  if [[ "$BRIDGE_USER_EXISTS" == "1" ]]; then
    # Patch: ensure the squad is attached (ssPassword already matches the inbound).
    local uuid; uuid="$(panel_req GET "/api/users/by-username/${BRIDGE_USER}" 2>/dev/null | jq -r '.response.uuid // empty')"
    [[ -n "$uuid" ]] || { warn "Could not resolve UUID for existing user '$BRIDGE_USER' — attach it to the squad manually."; return 0; }
    body="$(jq -n --arg u "$uuid" --argjson sq "$squad_json" '{uuid:$u, activeInternalSquads:$sq}')"
    if panel_req PATCH /api/users "$body" >/dev/null 2>&1; then
      ok "Bridge user '$BRIDGE_USER' attached to the node's Internal Squad."
    else
      warn "Could not PATCH user '$BRIDGE_USER' (scope/shape) — attach it to the squad manually."
    fi
    return 0
  fi
  body="$(jq -n --arg n "$BRIDGE_USER" --arg pw "$BRIDGE_SS_PASSWORD" --arg exp "$expire" --argjson sq "$squad_json" \
    '{username:$n, status:"ACTIVE", ssPassword:$pw, trafficLimitBytes:0, expireAt:$exp, activeInternalSquads:$sq}')"
  if panel_req POST /api/users "$body" >/dev/null; then
    ok "Created bridge user '$BRIDGE_USER' (unlimited, 99y, ACTIVE) with the bridge inbound enabled."
  else
    die "Failed to create bridge user '$BRIDGE_USER'. Check the token scope and that the username is free."
  fi
}
# Fallback host lookup when the remark changed on resume: identify the existing
# host by the inbound it serves (configProfileInboundUuid is unique per inbound),
# or by the address/port/sni tuple under the same profile. Prints the UUID only
# when EXACTLY one host matches; on multiple ambiguous matches it warns and prints
# nothing (never deletes — the caller then leaves them for manual cleanup).
panel_find_host_by_inbound() {
  local cp="$1" ib="$2" addr="$3" port="$4" sni="$5" r matches n
  r="$(panel_req GET /api/hosts 2>/dev/null)" || return 0
  matches="$(printf '%s' "$r" | jq -c --arg cp "$cp" --arg ib "$ib" --arg a "$addr" --argjson p "$port" --arg s "$sni" \
    '[.response[]?
      | select((.inbound.configProfileInboundUuid == $ib)
               or (.inbound.configProfileUuid == $cp and .address == $a and .port == $p and .sni == $s))
      | .uuid] | unique')"
  n="$(printf '%s' "$matches" | jq 'length' 2>/dev/null || echo 0)"
  if [[ "$n" == "1" ]]; then printf '%s' "$matches" | jq -r '.[0]'; return 0; fi
  if [[ "${n:-0}" -gt 1 ]]; then
    warn "Found $n candidate hosts for inbound $ib ($addr:$port) — ambiguous, not modifying any. Remove duplicates in the panel, then re-run." >&2
    return 2
  fi
  return 0
}
# Copy existing Reality keys from a profile into the REALITY_* globals so an
# update reuses them (regenerating would invalidate every existing subscription).
adopt_existing_reality_keys() {
  local cfg="$1" epriv esid epub
  # Robust to where the settings live (.response.config.inbounds vs nested):
  # find any realitySettings.privateKey / shortIds anywhere in the response.
  epriv="$(printf '%s' "$cfg" | jq -r '[.. | objects | select(has("privateKey")) | .privateKey] | map(select(. != null and . != "")) | .[0] // empty')"
  esid="$(printf '%s' "$cfg" | jq -r '[.. | objects | select(has("shortIds")) | .shortIds[]?] | map(select(. != null and . != "")) | .[0] // empty')"
  if [[ -n "$epriv" ]]; then
    REALITY_PRIVATE="$epriv"
    [[ -n "$esid" ]] && REALITY_SHORT_ID="$esid"
    # publicKey is no longer stored in the config; derive it from the private key
    # so existing client subscriptions keep matching (this is what keeps the key
    # STABLE across re-runs — without it every run would rotate the key).
    if [[ "$DRY_RUN" != "1" ]]; then
      epub="$("$XRAY_CORE_BIN" x25519 -i "$epriv" 2>/dev/null | grep -iE 'public|password' | awk '{print $NF}' | head -1 || true)"
      [[ -n "$epub" ]] && REALITY_PUBLIC="$epub"
    fi
    info "Reusing existing Reality key (public ${REALITY_PUBLIC:0:12}…, shortId $REALITY_SHORT_ID)."
  fi
}

# ── Panel orchestration ─────────────────────────────────────────────────────
setup_panel_resources() {
  step "Creating panel resources via API"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: GET /api/keygen; POST /api/config-profiles; GET profile; POST /api/nodes; POST /api/hosts"
    # Show the config-profile JSON that would be pushed (no secrets in it) — only
    # if jq is present; on a clean server dry-run must not require it (L2).
    if command -v jq >/dev/null 2>&1; then
      [[ "$MASK" == "reality" && -z "$REALITY_PRIVATE" ]] && generate_reality_keys
      [[ "$BRIDGE" == "1" && -z "$BRIDGE_SS_PASSWORD" ]] && BRIDGE_SS_PASSWORD="DRY_SS_PASSWORD"
      info "DRY-RUN generated Xray config (config-profile body):"
      build_xray_config | jq . 2>/dev/null || build_xray_config
    else
      warn "jq unavailable — skipping the Xray config JSON preview (install jq to preview it)."
    fi
    NODE_SECRET_KEY="DRY_SECRET_KEY"; CONFIG_PROFILE_UUID="dry-cp"; INBOUND_UUID="dry-ib"
    NODE_UUID="dry-node"; HOST_UUID="dry-host"; return
  fi

  if [[ -n "$SECRET_KEY_OVERRIDE" ]]; then
    NODE_SECRET_KEY="$SECRET_KEY_OVERRIDE"
    ok "Using provided SECRET_KEY (--secret-key), skipping /api/keygen."
  else
    info "Fetching node SECRET_KEY from the panel…"
    NODE_SECRET_KEY="$(panel_get_keygen)" \
      || die "GET /api/keygen failed (token lacks keygen scope?). Pass --secret-key to bypass."
    [[ -n "$NODE_SECRET_KEY" && "$NODE_SECRET_KEY" != "null" ]] || die "/api/keygen returned an empty SECRET_KEY."
    ok "Got SECRET_KEY ($(printf '%s' "$NODE_SECRET_KEY" | wc -c) chars)."
  fi

  # Cascade: the SS bridge inbound needs its password BEFORE build_xray_config runs.
  panel_resolve_bridge_password

  # ── 1. Config-profile: create, or reuse existing (by name) and update ──
  local _rc=0
  CONFIG_PROFILE_UUID="$(panel_find_profile_uuid "$PROFILE_NAME")" || _rc=$?
  (( _rc == 3 )) && die "Multiple config profiles named '$PROFILE_NAME' (see UUIDs above) — remove/rename the duplicate in the panel, or pass a unique --profile-name, then re-run."
  if [[ -z "$CONFIG_PROFILE_UUID" ]]; then
    # A DIFFERENT profile may own one of our inbound tags (tags are globally
    # unique in Remnawave). Updating it in place would replace its entire config
    # and hijack whatever nodes it serves — never do that silently. Adopt only
    # with explicit --adopt-profile; otherwise stop and let the user decide.
    local owner owner_uuid owner_name owner_tag
    owner="$(panel_find_profile_uuid_by_inbound_tags)"
    if [[ -n "$owner" ]]; then
      IFS=$'\t' read -r owner_uuid owner_name owner_tag <<< "$owner"
      if [[ "$ADOPT_PROFILE" == "1" ]]; then
        CONFIG_PROFILE_UUID="$owner_uuid"
        warn "Adopting foreign config-profile '$owner_name' ($owner_uuid) because it owns inbound tag '$owner_tag' (--adopt-profile). Its config will be REPLACED with this install's."
      else
        die "Inbound tag '$owner_tag' is already owned by config-profile '$owner_name' ($owner_uuid), not '$PROFILE_NAME'. Refusing to touch a foreign profile. Either: (a) delete/rename that profile in the panel, (b) pick a different node name (changes the tag), or (c) re-run with --adopt-profile to overwrite its config deliberately."
      fi
    fi
  fi
  local xray_cfg body resp
  if [[ -n "$CONFIG_PROFILE_UUID" ]]; then
    info "Config-profile '$PROFILE_NAME' exists ($CONFIG_PROFILE_UUID) — updating in place."
    local existing
    existing="$(panel_req GET "/api/config-profiles/${CONFIG_PROFILE_UUID}")" || die "GET config-profile failed."
    if [[ "$MASK" == "reality" ]]; then
      if [[ "$ROTATE_KEYS" == "1" ]]; then
        warn "Rotating Reality keys — existing client subscriptions must resync (new publicKey)."
        generate_reality_keys
      else
        adopt_existing_reality_keys "$existing"   # keep keys stable for subscriptions
        # Existing profile carried no usable Reality key (e.g. it was a grpc-tls
        # profile, or empty): mint one now rather than pushing an empty privateKey.
        [[ -n "$REALITY_PRIVATE" ]] || { warn "No Reality key in existing profile — generating a fresh keypair."; generate_reality_keys; }
      fi
    fi
    xray_cfg="$(build_xray_config)"; validate_xray_config "$xray_cfg"
    body="$(jq -n --arg u "$CONFIG_PROFILE_UUID" --argjson c "$xray_cfg" '{uuid:$u, config:$c}')"
    panel_req PATCH /api/config-profiles "$body" >/dev/null || die "Update config-profile failed."
    ok "config-profile updated: $CONFIG_PROFILE_UUID"
  else
    # New profile: mint Reality keys once, here (was previously done in main() even
    # on reruns that then adopted the old key — a wasted keygen with confusing output).
    [[ "$MASK" == "reality" && -z "$REALITY_PRIVATE" ]] && generate_reality_keys
    xray_cfg="$(build_xray_config)"; validate_xray_config "$xray_cfg"
    info "Creating config-profile '$PROFILE_NAME'…"
    body="$(jq -n --arg n "$PROFILE_NAME" --argjson c "$xray_cfg" '{name:$n, config:$c}')"
    resp="$(panel_req POST /api/config-profiles "$body")" || die "Create config-profile failed."
    CONFIG_PROFILE_UUID="$(printf '%s' "$resp" | jq -r '.response.uuid')"
    [[ -n "$CONFIG_PROFILE_UUID" && "$CONFIG_PROFILE_UUID" != "null" ]] || die "No config-profile UUID in response."
    ok "config-profile: $CONFIG_PROFILE_UUID"
  fi

  info "Reading panel-assigned inbound UUIDs…"
  local cp inbounds
  cp="$(panel_req GET "/api/config-profiles/${CONFIG_PROFILE_UUID}")" || die "GET config-profile failed."
  inbounds="$(printf '%s' "$cp" | jq -c '.response.inbounds // .response.config.inbounds // []')"

  local -a RES_UUID=() RES_PORT=() RES_NET=()
  local spec tag port net uuid
  for spec in "${ACTIVE_INBOUNDS[@]}"; do
    IFS=: read -r tag port net <<< "$spec"
    uuid="$(printf '%s' "$inbounds" | jq -r --arg t "$tag" 'map(select(.tag==$t)) | .[0].uuid // empty')"
    [[ -n "$uuid" && "$uuid" != "null" ]] || die "Could not resolve inbound UUID for tag '$tag'."
    RES_UUID+=("$uuid"); RES_PORT+=("$port"); RES_NET+=("$net")
    ok "inbound $tag → $uuid"
  done
  INBOUND_UUID="${RES_UUID[0]}"   # primary, kept for state/back-compat
  INBOUND_UUIDS=("${RES_UUID[@]}") # full set, for Internal Squad activation

  # Cascade: resolve the SS bridge inbound UUID too. It must be linked to the NODE
  # (so Xray binds the SS port) but NOT to a host (SS is a node-to-node relay, not
  # a subscription entry) and NOT to the users' squad — so it is deliberately kept
  # out of RES_UUID/INBOUND_UUIDS, which drive the host loop and squad activation.
  local -a NODE_ACTIVE=("${RES_UUID[@]}")
  if [[ "$BRIDGE" == "1" ]]; then
    BRIDGE_INBOUND_UUID="$(printf '%s' "$inbounds" | jq -r --arg t "$BRIDGE_TAG" 'map(select(.tag==$t)) | .[0].uuid // empty')"
    [[ -n "$BRIDGE_INBOUND_UUID" && "$BRIDGE_INBOUND_UUID" != "null" ]] || die "Could not resolve SS bridge inbound UUID for tag '$BRIDGE_TAG'."
    NODE_ACTIVE+=("$BRIDGE_INBOUND_UUID")
    ok "bridge inbound $BRIDGE_TAG → $BRIDGE_INBOUND_UUID (linked to node only)"
  fi
  local active_json
  active_json="$(printf '%s\n' "${NODE_ACTIVE[@]}" | jq -R . | jq -sc .)"

  # ── 2. Node: create, or reuse existing (by name/address); link all inbounds ──
  _rc=0
  NODE_UUID="$(panel_find_node_uuid "$NODE_NAME" "$NODE_PUBLIC_IP")" || _rc=$?
  (( _rc == 3 )) && die "Multiple panel nodes match name '$NODE_NAME' / address '$NODE_PUBLIC_IP' (see UUIDs above) — resolve the duplicate in the panel, then re-run."
  if [[ -n "$NODE_UUID" ]]; then
    info "Node exists ($NODE_UUID) — updating config-profile link."
    body="$(jq -n --arg u "$NODE_UUID" \
      --arg name "$NODE_NAME" --arg addr "$NODE_PUBLIC_IP" --arg cc "${COUNTRY^^}" \
      --argjson port "$NODE_PORT" --arg cp "$CONFIG_PROFILE_UUID" --argjson ibs "$active_json" \
      '{uuid:$u, name:$name, address:$addr, port:$port, countryCode:$cc,
        configProfile:{activeConfigProfileUuid:$cp, activeInbounds:$ibs}}')"
    panel_req PATCH /api/nodes "$body" >/dev/null || die "Update node failed."
    ok "node updated: $NODE_UUID"
  else
    info "Registering node '$NODE_NAME'…"
    body="$(jq -n \
      --arg name "$NODE_NAME" --arg addr "$NODE_PUBLIC_IP" --arg cc "${COUNTRY^^}" \
      --argjson port "$NODE_PORT" --arg cp "$CONFIG_PROFILE_UUID" --argjson ibs "$active_json" \
      '{name:$name, address:$addr, port:$port, countryCode:$cc,
        configProfile:{activeConfigProfileUuid:$cp, activeInbounds:$ibs}}')"
    resp="$(panel_req POST /api/nodes "$body")" || die "Create node failed."
    NODE_UUID="$(printf '%s' "$resp" | jq -r '.response.uuid')"
    [[ -n "$NODE_UUID" && "$NODE_UUID" != "null" ]] || die "No node UUID in response."
    ok "node: $NODE_UUID"
  fi

  local check active linked_cp
  check="$(panel_req GET "/api/nodes/${NODE_UUID}" 2>/dev/null || echo '')"
  # The node must point at THE profile this install created/updated — a mismatch
  # means the panel silently kept (or someone raced in) a foreign profile, and
  # clients would receive someone else's config.
  linked_cp="$(printf '%s' "$check" | jq -r '.response.configProfile.activeConfigProfileUuid // empty' 2>/dev/null || echo '')"
  [[ "$linked_cp" == "$CONFIG_PROFILE_UUID" ]] \
    || die "Node $NODE_UUID is linked to config-profile '${linked_cp:-<none>}', expected '$CONFIG_PROFILE_UUID' ('$PROFILE_NAME'). Foreign profile attached — fix the node's config profile in the panel, then re-run with --resume."
  active="$(printf '%s' "$check" | jq '[.response.configProfile.activeInbounds[]?] | length' 2>/dev/null || echo 0)"
  (( active >= ${#RES_UUID[@]} )) || die "Node linked $active inbound(s), expected ${#RES_UUID[@]} — Xray may bind nothing."
  ok "Node linked to profile $CONFIG_PROFILE_UUID with $active inbound(s)."

  # ── 3. One host per active inbound (create or update by remark+address) ──
  HOST_UUIDS=()
  local i suffix hremark extra seclayer h_uuid
  for i in "${!RES_UUID[@]}"; do
    uuid="${RES_UUID[$i]}"; net="${RES_NET[$i]}"; port="${RES_PORT[$i]}"
    suffix=""; extra='{}'; seclayer="DEFAULT"
    # Panel expects alpn as a string enum ('h2'), not an array.
    if [[ "$net" == "xhttp" ]]; then
      suffix=" XHTTP"; extra="$(jq -n --arg p "$XHTTP_PATH" --argjson xp "$(xhttp_extra_json)" '{path:$p, alpn:"h2", xhttpExtraParams:$xp}')"
    elif [[ "$net" == "grpc-tls" ]]; then
      # nginx terminates real TLS, so the client link is security=TLS, network=grpc.
      # The inbound listens on the loopback GRPC_PORT, but clients reach the public
      # nginx port 443 — the host must advertise 443, not the inbound port. host/sni
      # carry the DOMAIN; alpn offers h2 first then http/1.1 (matches NikitaAzmov/GRPC).
      suffix=" gRPC"; seclayer="TLS"; port=443
      extra="$(jq -n --arg p "$GRPC_SERVICE" --arg h "$DOMAIN" '{path:$p, alpn:"h2,http/1.1", host:$h}')"
    fi
    hremark="${HOST_REMARK}${suffix}"; hremark="${hremark:0:40}"
    local _hrc=0
    h_uuid="$(panel_find_host_uuid "$hremark" "$HOST_ADDRESS")" || _hrc=$?
    (( _hrc == 3 )) && die "Multiple hosts with remark '$hremark' @ '$HOST_ADDRESS' (see UUIDs above) — remove the duplicate in the panel, then re-run."
    # Remark may have changed since the first run → the exact match misses. Fall
    # back to the inbound/address identity so we patch (and rename) the existing
    # host instead of creating a duplicate.
    if [[ -z "$h_uuid" ]]; then
      if h_uuid="$(panel_find_host_by_inbound "$CONFIG_PROFILE_UUID" "$uuid" "$HOST_ADDRESS" "$port" "$DOMAIN")"; then
        [[ -n "$h_uuid" ]] && info "Adopting existing host $h_uuid by inbound identity (remark → '$hremark')."
      else
        die "Ambiguous existing hosts for inbound $uuid ($HOST_ADDRESS:$port). Remove duplicates in the panel, then re-run."
      fi
    fi
    if [[ -n "$h_uuid" ]]; then
      body="$(jq -n --arg u "$h_uuid" --arg cp "$CONFIG_PROFILE_UUID" --arg ib "$uuid" --arg remark "$hremark" \
        --arg addr "$HOST_ADDRESS" --arg sni "$DOMAIN" --arg fp "$FP" --arg node "$NODE_UUID" --arg sl "$seclayer" --argjson port "$port" --argjson extra "$extra" \
        '{uuid:$u, inbound:{configProfileUuid:$cp, configProfileInboundUuid:$ib}, remark:$remark, address:$addr, port:$port, sni:$sni, fingerprint:$fp, securityLayer:$sl, nodes:[$node]} + $extra')"
      panel_req PATCH /api/hosts "$body" >/dev/null || die "Update host ($hremark) failed."
      ok "host updated: $hremark → $h_uuid"
    else
      body="$(jq -n --arg cp "$CONFIG_PROFILE_UUID" --arg ib "$uuid" --arg remark "$hremark" \
        --arg addr "$HOST_ADDRESS" --arg sni "$DOMAIN" --arg fp "$FP" --arg node "$NODE_UUID" --arg sl "$seclayer" --argjson port "$port" --argjson extra "$extra" \
        '{inbound:{configProfileUuid:$cp, configProfileInboundUuid:$ib}, remark:$remark, address:$addr, port:$port, sni:$sni, fingerprint:$fp, securityLayer:$sl, nodes:[$node]} + $extra')"
      resp="$(panel_req POST /api/hosts "$body")" || die "Create host ($hremark) failed."
      h_uuid="$(printf '%s' "$resp" | jq -r '.response.uuid')"
      ok "host: $hremark → $h_uuid"
    fi
    HOST_UUIDS+=("$h_uuid")
  done
  HOST_UUID="${HOST_UUIDS[0]}"

  # Same guarantee for hosts: every host this install touched must reference OUR
  # config-profile, or subscriptions would hand out a foreign config.
  local hosts_resp bad
  hosts_resp="$(panel_req GET /api/hosts 2>/dev/null || echo '')"
  for h_uuid in "${HOST_UUIDS[@]}"; do
    bad="$(printf '%s' "$hosts_resp" | jq -r --arg u "$h_uuid" --arg cp "$CONFIG_PROFILE_UUID" \
      '[.response[]? | select(.uuid == $u and .inbound.configProfileUuid != $cp) | .inbound.configProfileUuid] | .[0] // empty' 2>/dev/null || echo '')"
    [[ -z "$bad" ]] \
      || die "Host $h_uuid references config-profile '$bad', expected '$CONFIG_PROFILE_UUID' ('$PROFILE_NAME'). Foreign profile attached — fix the host in the panel, then re-run with --resume."
  done
  ok "All ${#HOST_UUIDS[@]} host(s) reference profile $CONFIG_PROFILE_UUID."
  save_state
}
save_state() {
  [[ "$DRY_RUN" == "1" ]] && return
  mkdir -p "$STATE_DIR"
  # Persist identity fields too (node_name/profile_name/host_remark/country/
  # host_address/mask/transport) so a later --resume reuses the SAME names instead
  # of regenerating a fresh <CC>-<seq> and drifting / creating duplicate hosts.
  jq -n \
    --arg domain "$DOMAIN" --arg node "$NODE_UUID" --arg cp "$CONFIG_PROFILE_UUID" \
    --arg ib "$INBOUND_UUID" --arg host "$HOST_UUID" --arg pub "$REALITY_PUBLIC" \
    --arg sid "$REALITY_SHORT_ID" --arg now "$(date -Iseconds)" \
    --arg node_name "$NODE_NAME" --arg profile_name "$PROFILE_NAME" \
    --arg host_remark "$HOST_REMARK" --arg country "$COUNTRY" \
    --arg host_address "$HOST_ADDRESS" --arg mask "$MASK" --arg transport "$TRANSPORT" \
    '{domain:$domain, node_uuid:$node, config_profile_uuid:$cp, inbound_uuid:$ib,
      host_uuid:$host, reality_public_key:$pub, reality_short_id:$sid,
      node_name:$node_name, profile_name:$profile_name, host_remark:$host_remark,
      country:$country, host_address:$host_address, mask:$mask, transport:$transport,
      created_at:$now}' \
    > "$STATE_DIR/node.json"
  chmod 600 "$STATE_DIR/node.json"
  info "State saved to $STATE_DIR/node.json"
}
# On --resume, reload the identity chosen by the original run BEFORE collect_inputs
# generates defaults, so names stay stable. CLI/env values always win; a changed
# --domain is rejected rather than silently drifting.
load_resume_state() {
  [[ "$RESUME" == "1" ]] || return 0
  local sf="$STATE_DIR/node.json"
  [[ -f "$sf" ]] || { info "Resume: no saved state at $sf — using provided/generated values."; return 0; }
  command -v jq >/dev/null 2>&1 || return 0
  local s_domain s_node s_profile s_remark s_cc s_addr s_mask s_transport
  s_domain="$(jq -r '.domain // empty'       "$sf" 2>/dev/null)"
  s_node="$(jq -r   '.node_name // empty'     "$sf" 2>/dev/null)"
  s_profile="$(jq -r '.profile_name // empty' "$sf" 2>/dev/null)"
  s_remark="$(jq -r  '.host_remark // empty'  "$sf" 2>/dev/null)"
  s_cc="$(jq -r      '.country // empty'      "$sf" 2>/dev/null)"
  s_addr="$(jq -r    '.host_address // empty' "$sf" 2>/dev/null)"
  s_mask="$(jq -r    '.mask // empty'         "$sf" 2>/dev/null)"
  s_transport="$(jq -r '.transport // empty'  "$sf" 2>/dev/null)"
  if [[ -n "$s_domain" && -n "$DOMAIN" && "$s_domain" != "$DOMAIN" ]]; then
    die "Resume state domain ($s_domain) != requested --domain ($DOMAIN). Use the original domain, or run a fresh install without --resume."
  fi
  [[ -z "$DOMAIN"       && -n "$s_domain"    ]] && DOMAIN="$s_domain"
  [[ -z "$NODE_NAME"    && -n "$s_node"      ]] && NODE_NAME="$s_node"
  [[ -z "$PROFILE_NAME" && -n "$s_profile"   ]] && PROFILE_NAME="$s_profile"
  [[ -z "$HOST_REMARK"  && -n "$s_remark"    ]] && HOST_REMARK="$s_remark"
  [[ -z "$COUNTRY"      && -n "$s_cc"        ]] && COUNTRY="$s_cc"
  [[ -z "$HOST_ADDRESS" && -n "$s_addr"      ]] && HOST_ADDRESS="$s_addr"
  [[ -z "$MASK"         && -n "$s_mask"      ]] && MASK="$s_mask"
  [[ -z "$TRANSPORT"    && -n "$s_transport" ]] && TRANSPORT="$s_transport"
  info "Resume: restored identity from $sf (node=${NODE_NAME:-?}, profile=${PROFILE_NAME:-?})."
}

# ── Full-input persistence (fast resume) ────────────────────────────────────
# node.json only carries identity and is written late (after panel resources).
# To make `--resume` work after an *early* failure (e.g. the system-update stage)
# without re-typing every answer, we snapshot ALL operator/plan values — the panel
# token included — to $STATE_DIR/inputs.env right after the plan is confirmed. The
# file is chmod 600 (root-only) and `source`-able; --resume reloads it.
#
# SAVED_KEYS is the single source of truth for both save and load. Secrets live in
# this file too (user opted in): it never leaves the box and is 600, and sourcing
# keeps the token out of `ps` (unlike --panel-token on the command line).
readonly SAVED_KEYS="DOMAIN PANEL_URL PANEL_TOKEN PANEL_WHITELIST FRONT_IP CERT_MODE CF_TOKEN \
ACME_EMAIL COUNTRY NODE_PORT SELFSTEAL_PORT RENEW_PORT SSH_PORT MASK FP TEMPLATE MODE \
TCP_PORTS UDP_PORTS NA_REF NODE_NAME HOST_REMARK PROFILE_NAME HOST_ADDRESS SQUAD_NAME \
SQUAD_UUID SQUAD_CREATE TRANSPORT XHTTP_PORT XHTTP_PATH GRPC_PORT GRPC_SERVICE \
NODE_PUBLIC_IP NODE_IMAGE SKIP_FIREWALL SKIP_UPDATE SKIP_CROWDSEC HARDENING GEO RANDOMIZE ROTATE_KEYS NAMESPACE_HASH \
BRIDGE BRIDGE_ENTRY_IP BRIDGE_SS_PORT BRIDGE_METHOD BRIDGE_USER ENTRY_DOMAIN"

# Write every SAVED_KEYS value as a source-able KEY=value line. printf %q keeps
# tokens/spaces/IPs safe. Atomic (temp + mv) so a resume never reads a half file.
save_inputs() {
  [[ "$DRY_RUN" == "1" ]] && return 0
  mkdir -p "$STATE_DIR" 2>/dev/null || { warn "Could not create $STATE_DIR — inputs not saved (resume will re-prompt)."; return 0; }
  local f="$STATE_DIR/inputs.env" tmp v
  tmp="$(mktemp "$STATE_DIR/.inputs.XXXXXX" 2>/dev/null)" || { warn "Could not write $f — inputs not saved."; return 0; }
  chmod 600 "$tmp" 2>/dev/null || true   # tighten BEFORE the token is written
  {
    printf '# remnawave-node saved inputs — reused by: sudo bash %s --resume -y\n' "$0"
    printf '# chmod 600, root-only. Contains the panel API token. Delete to forget.\n'
    for v in $SAVED_KEYS; do printf '%s=%q\n' "$v" "${!v-}"; done
  } > "$tmp"
  mv -f "$tmp" "$f" 2>/dev/null || { rm -f "$tmp"; warn "Could not finalize $f."; return 0; }
  info "Saved inputs to $f (chmod 600) — resume with: sudo bash $0 --resume -y"
}

# On --resume, reload saved inputs BEFORE collect_inputs prompts. CLI/env values
# provided this run always win, so we snapshot the currently-set keys, source the
# file, then re-apply the snapshot over it.
load_inputs() {
  [[ "$RESUME" == "1" ]] || return 0
  local f="$STATE_DIR/inputs.env"
  [[ -r "$f" ]] || { info "Resume: no saved inputs at $f — using provided/generated values."; return 0; }
  local k; declare -A _cli=()
  # A key counts as "provided this run" only if it differs from the built-in default
  # captured in main() before parse_args — NOT merely by being non-empty, which every
  # non-empty-defaulted key always is. If the snapshot is somehow unset this degrades
  # to the old non-empty test (safe: saved value still wins for untouched empties).
  for k in $SAVED_KEYS; do
    { [[ -n "${CLI_SET[$k]:-}" ]] || [[ "${!k-}" != "${DEFAULT_SNAPSHOT[$k]-}" ]]; } && _cli[$k]="${!k}"
  done
  # Source in the current shell WITHOUT `set -a`: values become plain globals, not
  # exported — so the token is not leaked into apt/docker child environments.
  # shellcheck disable=SC1090
  source "$f"
  for k in "${!_cli[@]}"; do printf -v "$k" '%s' "${_cli[$k]}"; done
  # Back-compat: NAMESPACE_HASH now defaults ON for fresh installs, but state saved
  # before this key existed belongs to a node whose tags carry NO hash. If the file
  # predates the key and the operator did not pass a flag, force it OFF so a resume
  # never silently re-tags a deployed node (which would orphan its Hosts/Squad).
  if [[ -z "${CLI_SET[NAMESPACE_HASH]:-}" ]] && ! grep -q '^NAMESPACE_HASH=' "$f"; then
    NAMESPACE_HASH=0
  fi
  info "Resume: loaded saved inputs from $f (CLI/env overrides still win)."
}

# ── Internal Squad activation ───────────────────────────────────────────────
# Remnawave's access chain is: Config Profile -> active Inbounds on Node -> Host
# -> Internal Squad -> Users. The steps above wire everything up to Host, but a
# user only receives the inbound once it is enabled in an Internal Squad. If a
# squad is named, add our inbound UUID(s) to it (union — never removes existing);
# otherwise warn loudly with the manual steps. All API calls are best-effort:
# on any shape/scope mismatch we warn instead of failing the whole install.
setup_squad() {
  step "Internal Squad (user access)"
  local tags="" spec t
  for spec in "${ACTIVE_INBOUNDS[@]}"; do IFS=: read -r t _ _ <<< "$spec"; tags+="${tags:+, }$t"; done

  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: GET /api/internal-squads; PATCH add inbound(s) [$tags] to squad '${SQUAD_UUID:-${SQUAD_NAME:-<none>}}'"
    return
  fi
  if [[ -z "$SQUAD_UUID" && -z "$SQUAD_NAME" ]]; then
    warn "No Internal Squad selected — users will NOT see this inbound yet."
    warn "Finish in the panel: Internal Squads -> edit/create a squad -> enable inbound(s) [$tags] -> save."
    warn "Or re-run with --squad-name '<name>' (or --squad-uuid <uuid>) to automate this."
    return 0
  fi

  local squads suuid
  squads="$(panel_req GET /api/internal-squads 2>/dev/null || echo '')"
  [[ -n "$squads" ]] || { warn "Could not list Internal Squads (token scope?) — enable inbound(s) [$tags] manually."; return 0; }
  # Tolerant to either {response:{internalSquads:[…]}} or {response:[…]}.
  if [[ -n "$SQUAD_UUID" ]]; then
    suuid="$SQUAD_UUID"
  else
    suuid="$(printf '%s' "$squads" | jq -r --arg n "$SQUAD_NAME" \
      '[(.response.internalSquads? // .response // [])[] | select(.name==$n) | .uuid] | .[0] // empty' 2>/dev/null || echo '')"
  fi
  if [[ -z "$suuid" ]]; then
    # "New squad" chosen interactively (or --squad-name for a missing squad with
    # create intent): create it with our inbounds already attached.
    if [[ "$SQUAD_CREATE" == "1" && -n "$SQUAD_NAME" ]]; then
      local cbody cresp add0
      add0="$(printf '%s\n' "${INBOUND_UUIDS[@]}" | jq -R . | jq -sc .)"
      cbody="$(jq -n --arg n "$SQUAD_NAME" --argjson ibs "$add0" '{name:$n, inbounds:$ibs}')"
      cresp="$(panel_req POST /api/internal-squads "$cbody" 2>/dev/null || echo '')"
      suuid="$(printf '%s' "$cresp" | jq -r '.response.uuid // empty' 2>/dev/null || echo '')"
      [[ -n "$suuid" ]] && { ok "Created Internal Squad '$SQUAD_NAME' ($suuid) with inbound(s) [$tags]."; return 0; }
      warn "Could not create Internal Squad '$SQUAD_NAME' via API — create it manually and enable inbound(s) [$tags]."; return 0
    fi
    warn "Internal Squad '${SQUAD_NAME}' not found — enable inbound(s) [$tags] manually."; return 0
  fi

  # Current inbound UUIDs in the squad (inbounds may be uuid strings or objects).
  local cur add merged body
  cur="$(printf '%s' "$squads" | jq -c --arg u "$suuid" \
    '[(.response.internalSquads? // .response // [])[] | select(.uuid==$u) | (.inbounds // [])[] | (if type=="object" then .uuid else . end)]' 2>/dev/null || echo '[]')"
  [[ -n "$cur" && "$cur" != "null" ]] || cur='[]'
  add="$(printf '%s\n' "${INBOUND_UUIDS[@]}" | jq -R . | jq -sc .)"
  merged="$(jq -cn --argjson a "$cur" --argjson b "$add" '($a + $b) | unique')"
  body="$(jq -n --arg u "$suuid" --argjson ibs "$merged" '{uuid:$u, inbounds:$ibs}')"
  if panel_req PATCH /api/internal-squads "$body" >/dev/null 2>&1; then
    ok "Internal Squad updated ($suuid) — inbound(s) [$tags] enabled for users."
  else
    warn "Could not update Internal Squad via API (shape/scope mismatch) — enable inbound(s) [$tags] manually in the panel."
  fi
  BRIDGE_SQUAD_UUID="$suuid"   # cascade: the bridge user joins this same squad
}

# ── Firewall (node-accelerator, kept) ───────────────────────────────────────
# Split into protect (strict allowlist) and optimize (kernel tuning). protect runs
# BEFORE start_node so NODE_PORT is never open to the whole internet before the
# whitelist rule exists; optimize (which can be slow / may reboot) runs after the
# service is up.
NA_INSTALLER=""            # resolved once (download or local), reused by both actions
na_prepare() {
  # Resolve the installer once (local checkout / tarball / cached / download with
  # retries). Returns 1 only when firewall work is skipped so callers can bail.
  [[ "$SKIP_FIREWALL" == "1" ]] && return 1
  [[ -n "$NA_INSTALLER" ]] && return 0
  if [[ "$DRY_RUN" == "1" ]]; then NA_INSTALLER="$SCRATCH/na-install.sh"; return 0; fi

  # 1. Local checkout (fully offline): installer runs with its own modules alongside.
  if [[ -n "$NA_DIR" ]]; then
    [[ -f "$NA_DIR/install.sh" ]] || die "--node-accelerator-dir has no install.sh: $NA_DIR"
    NA_INSTALLER="$NA_DIR/install.sh"; info "Using local node-accelerator checkout: $NA_DIR"; return 0
  fi
  mkdir -p "$CACHE_DIR" 2>/dev/null || true
  # 2. Local tarball → extract into the cache, then run from there.
  if [[ -n "$NA_TAR" ]]; then
    [[ -f "$NA_TAR" ]] || die "--node-accelerator-tar not found: $NA_TAR"
    local xd="$CACHE_DIR/na-src"; rm -rf "$xd"; mkdir -p "$xd"
    tar -xzf "$NA_TAR" -C "$xd" 2>/dev/null || die "Failed to extract node-accelerator tarball: $NA_TAR"
    local ish; ish="$(find "$xd" -maxdepth 3 -name install.sh -type f 2>/dev/null | head -1 || true)"
    [[ -n "$ish" ]] || die "No install.sh found inside $NA_TAR"
    NA_DIR="$(dirname "$ish")"; NA_INSTALLER="$ish"; info "Using node-accelerator from tarball: $NA_TAR"; return 0
  fi
  local ref_safe="${NA_REF//\//_}" dir="$CACHE_DIR/node-accelerator-${NA_REF//\//_}"
  # 3. Explicit single-file URL override (legacy): fetch just install.sh. NOTE this
  #    path can still fetch modules from GitHub at protect time (install.sh has no
  #    local scripts/ beside it) — prefer --node-accelerator-dir/-tar for offline.
  if [[ -n "$NA_URL" ]]; then
    local cache="$CACHE_DIR/na-install-${ref_safe}.sh"
    if curl -fsSL --connect-timeout 15 --max-time 120 --retry 3 --retry-delay 3 --retry-all-errors \
         "$NA_URL" -o "$cache.tmp" 2>/dev/null && [[ -s "$cache.tmp" ]]; then
      mv "$cache.tmp" "$cache"; NA_INSTALLER="$cache"
      warn "Fetched node-accelerator install.sh only (--node-accelerator-url) — 'protect' may still download modules online."
      return 0
    fi
    rm -f "$cache.tmp"
    [[ -s "$cache" ]] && { NA_INSTALLER="$cache"; warn "Download failed — using cached install.sh: $cache"; return 0; }
    die "Failed to fetch node-accelerator from $NA_URL. Preload with --node-accelerator-dir/-tar, or --skip-firewall."
  fi
  # 4. Default: download the FULL checkout tarball (install.sh + scripts/*) so the
  #    firewall step runs fully offline — upstream install.sh only fetches modules
  #    when its local scripts/ dir is missing. Cache on disk; reuse if network dies.
  local tgz="$CACHE_DIR/na-src-${ref_safe}.tar.gz"
  local tarurl="https://codeload.github.com/${NODE_ACCELERATOR_REPO}/tar.gz/${NA_REF}"
  if curl -fsSL --connect-timeout 15 --max-time 180 --retry 3 --retry-delay 3 --retry-all-errors \
       "$tarurl" -o "$tgz.tmp" 2>/dev/null && [[ -s "$tgz.tmp" ]]; then
    mv "$tgz.tmp" "$tgz"
    rm -rf "$dir"; mkdir -p "$dir"
    if tar -xzf "$tgz" -C "$dir" --strip-components=1 2>/dev/null && [[ -f "$dir/install.sh" && -d "$dir/scripts" ]]; then
      NA_DIR="$dir"; NA_INSTALLER="$dir/install.sh"
      info "node-accelerator checkout fetched (ref $NA_REF, offline-ready with local scripts/)."; return 0
    fi
    warn "Fetched tarball but it lacks install.sh + scripts/ — falling back."
  fi
  rm -f "$tgz.tmp"
  # 5. Reuse a previously extracted checkout if the network is down right now.
  if [[ -f "$dir/install.sh" && -d "$dir/scripts" ]]; then
    NA_DIR="$dir"; NA_INSTALLER="$dir/install.sh"; warn "Download failed — using cached node-accelerator checkout: $dir"; return 0
  fi
  die "Failed to fetch node-accelerator checkout from $tarurl. Preload with --node-accelerator-dir <dir> / --node-accelerator-tar <file>, or --skip-firewall."
}
# Background liveness ticker for a long, quiet node-accelerator phase. Prints an
# elapsed-time line every 20s until the parent kills it. Runs as a background job.
na_heartbeat() {
  local action="$1" t=0
  while :; do
    sleep 20; t=$(( t + 20 ))
    printf '%s%s%s node-accelerator %s still running… %ds elapsed (CrowdSec/apt can take minutes)\n' \
      "${DIM}" "[·]" "${RESET}" "$action" "$t"
  done
}
# Run one node-accelerator action. $2 (optional) caps the whole phase in seconds
# so a hung sub-step (e.g. CrowdSec APT config) cannot wedge the install.
na_run() {
  local action="$1" tmo="${2:-0}"
  info "node-accelerator: $action"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY-RUN: NA_REF=$NA_REF FW_MODE=strict SSH_PORT=$SSH_PORT NODE_PORT=$NODE_PORT WHITELIST=$PANEL_WHITELIST TCP_PORTS=$TCP_PORTS UDP_PORTS=$UDP_PORTS SAFETY_DELAY=$NA_SAFETY_DELAY$( [[ "$SKIP_CROWDSEC" == "1" ]] && echo ' ENABLE_CROWDSEC=0 CROWDSEC=0' ) bash na-install.sh $action"
    return 0
  fi
  # Heartbeat: CrowdSec's APT step can run several quiet minutes with no output,
  # which looks like a hang. Print an elapsed-time line every 20s while the child
  # runs so the operator can see it is still working. Killed as soon as it returns.
  local hb=""
  na_heartbeat "$action" & hb=$!
  local rc=0
  (
    [[ -n "$NA_DIR" ]] && cd "$NA_DIR"
    export NA_REF REMNAWAVE_NONINTERACTIVE=1 FW_MODE=strict
    export SSH_PORT NODE_PORT TCP_PORTS UDP_PORTS
    export WHITELIST="$PANEL_WHITELIST"
    # node-accelerator v3.8 derives strict NODE_PORT protection from WHITELIST,
    # but populates its dedicated na_nodeport_wl_* sets only from NODE_PORT_PEERS.
    # Without this bridge the firewall accepts 80/443 yet silently drops the
    # panel's mTLS connection to NODE_PORT, leaving Xray with no delivered profile.
    export NODE_PORT_PEERS="$PANEL_WHITELIST" NODE_PORT_AUTOWL=1
    # Own the safety timer: give ourselves a controlled window so the auto-rollback
    # can't fire mid-install, then disarm it AFTER verifying na_filter is active.
    export SAFETY_DELAY="$NA_SAFETY_DELAY"
    # CrowdSec opt-out. ENABLE_CROWDSEC=0 is the toggle node-accelerator v3.8 reads;
    # the rest cover older/variant names. Keep all for compatibility.
    [[ "$SKIP_CROWDSEC" == "1" ]] && export ENABLE_CROWDSEC=0 CROWDSEC=0 INSTALL_CROWDSEC=0 SKIP_CROWDSEC=1 CROWDSEC_ENABLE=0
    if [[ "$tmo" != "0" ]] && command -v timeout >/dev/null 2>&1; then
      # No --foreground: timeout puts the child in its own process group and signals
      # the WHOLE group on expiry, so grandchildren (apt-get/dpkg pulling XanMod)
      # are terminated too. -k escalates to SIGKILL if they ignore SIGTERM — this is
      # what prevents an orphaned package-manager process after an optimize timeout.
      timeout --preserve-status -k 10s "${tmo}s" bash "$NA_INSTALLER" "$action"
    else
      bash "$NA_INSTALLER" "$action"
    fi
  ) </dev/null || rc=$?
  [[ -n "$hb" ]] && { kill "$hb" 2>/dev/null || true; wait "$hb" 2>/dev/null || true; }
  # Feeding /dev/null above stops apt/needrestart/dialog/CrowdSec from grabbing our
  # tty; restore_tty repairs it anyway in case a timeout-killed child left it raw.
  restore_tty
  return $rc
}
# Best-effort mirror of PANEL_WHITELIST into protect's na_nodeport_wl_* sets.
# The panel is ALREADY admitted by the general whitelist rule, which sits above
# the node-port drop in protect's input chain — so an empty na_nodeport_wl_* set
# does not block the panel. Populating it is belt-and-suspenders (survives a
# general-whitelist edit), hence warnings only, never a failed install. CIDR
# entries are skipped: the sets are plain ipv4_addr/ipv6_addr without `flags
# interval`, exactly like protect.sh's own add_npwl skips them.
sync_na_node_port_allowlist() {
  [[ -n "$PANEL_WHITELIST" ]] || return 0

  local entry set dump
  local -a entries=()
  IFS=',' read -r -a entries <<< "$PANEL_WHITELIST"
  for entry in "${entries[@]}"; do
    [[ -n "$entry" ]] || continue
    if [[ "$entry" == */* ]]; then
      info "node-port allowlist: skipping CIDR '$entry' (set holds single addresses; the general whitelist already admits it)."
      continue
    fi
    if [[ "$entry" == *:* ]]; then
      set="na_nodeport_wl_v6"
    else
      set="na_nodeport_wl_v4"
    fi

    nft list set inet na_filter "$set" >/dev/null 2>&1 || {
      warn "Firewall set '$set' not present — skipping node-port allowlist sync (panel is admitted via the general whitelist)."
      return 0
    }
    # An existing element makes nft return non-zero; that is harmless. nft may
    # also print the element in normalized form (IPv6 compression), so a failed
    # grep is only worth a warning, not a failed install.
    nft add element inet na_filter "$set" "{ $entry }" >/dev/null 2>&1 || true
    dump="$(nft list set inet na_filter "$set" 2>/dev/null || true)"
    grep -Fq "$entry" <<< "$dump" \
      || warn "Could not confirm '$entry' in $set — panel access still works via the general whitelist."
  done
  return 0
}

# Confirm the strict nft ruleset is really loaded. protect's exit code is NOT
# trustworthy on its own: it can time out during the CrowdSec step long after the
# nft table was applied, or (rarely) exit 0 without the table sticking. Returns 0
# only when `nft list table inet na_filter` succeeds; the node-port allowlist
# sync is best-effort on top.
verify_na_firewall_active() {
  command -v nft >/dev/null 2>&1 || { warn "nft not found — cannot verify firewall state."; return 1; }
  nft list table inet na_filter >/dev/null 2>&1 || return 1
  sync_na_node_port_allowlist
  if ! { [[ -f /etc/systemd/system/na-firewall.service ]] \
         || systemctl is-enabled na-firewall.service >/dev/null 2>&1; }; then
    warn "na_filter is loaded but na-firewall.service was not found — rules may not survive a reboot."
  fi
  local marker=/var/lib/node-accelerator/protect.installed
  if [[ -f "$marker" ]]; then
    grep -qi 'fw_mode=strict' "$marker" 2>/dev/null || warn "protect marker: fw_mode is not strict."
    grep -q "$NODE_PORT" "$marker" 2>/dev/null   || warn "protect marker: NODE_PORT $NODE_PORT not listed."
  fi
  return 0
}
# Stop the non-interactive safety timer that node-accelerator arms to auto-delete
# na_filter after SAFETY_DELAY. Call ONLY after verify passed, so a bad ruleset
# still rolls back on its own. Covers both the systemd unit and a pid-file fallback.
disarm_na_safety_timer() {
  # The unit can be transient, so list-unit-files may not show it. Stop by name
  # unconditionally; it is harmless when the unit does not exist.
  systemctl stop na-fw-safety.timer na-fw-safety.service 2>/dev/null || true
  systemctl disable na-fw-safety.timer na-fw-safety.service 2>/dev/null || true
  systemctl reset-failed na-fw-safety.timer na-fw-safety.service 2>/dev/null || true
  local pf
  for pf in /run/na-fw-safety.pid /var/run/na-fw-safety.pid /var/lib/node-accelerator/safety.pid; do
    [[ -f "$pf" ]] || continue
    kill "$(cat "$pf" 2>/dev/null)" 2>/dev/null || true
    rm -f "$pf" 2>/dev/null || true
  done
}
na_safety_timer_active() {
  systemctl is-active --quiet na-fw-safety.timer 2>/dev/null \
    || systemctl is-active --quiet na-fw-safety.service 2>/dev/null
}
run_firewall_protect() {
  [[ "$SKIP_FIREWALL" == "1" ]] && { warn "Skipping firewall (--skip-firewall)."; return; }
  step "Firewall — node-accelerator protect (strict allowlist, before node opens NODE_PORT)"
  na_prepare || return
  [[ "$DRY_RUN" == "1" ]] && { na_run protect "$CROWDSEC_TIMEOUT"; ok "Firewall (protect) [dry-run]."; return; }

  # protect applies the nft ruleset first, then (unless --skip-crowdsec) configures
  # CrowdSec — which has hung on slow APT mirrors. Cap the phase so a stuck CrowdSec
  # step can't wedge the install. We NEVER trust the exit code: verify na_filter.
  local rc=0
  na_run protect "$CROWDSEC_TIMEOUT" || rc=$?

  if ! verify_na_firewall_active; then
    die "Firewall NOT active — 'nft list table inet na_filter' failed$( [[ "$rc" != 0 ]] && echo " (protect rc=$rc)" ). Refusing to start the node with NODE_PORT $NODE_PORT exposed. Fix, then re-run with --resume (optionally --skip-crowdsec / --node-accelerator-dir)."
  fi

  # Firewall verified live → safe to disarm the auto-rollback timer, then re-verify
  # the table survived disarming before we let the node bind NODE_PORT.
  disarm_na_safety_timer
  if na_safety_timer_active; then
    die "node-accelerator safety timer is still active after disarm — refusing to start the node because it may remove na_filter. Stop na-fw-safety.timer manually, then re-run with --resume."
  fi
  verify_na_firewall_active \
    || die "na_filter disappeared after disarming the node-accelerator safety timer — refusing to start the node. Re-run with --resume."

  if [[ "$rc" == 0 ]]; then
    ok "Firewall (protect) verified active — NODE_PORT $NODE_PORT restricted to whitelist."
  else
    warn "node-accelerator protect did not exit cleanly (rc=$rc; CrowdSec/APT slow?), but nft na_filter is verified active — continuing."
  fi
}
run_firewall_optimize() {
  [[ "$SKIP_FIREWALL" == "1" ]] && return   # protect already emitted the skip warning
  step "Network tuning — node-accelerator optimize (kernel/BBR; may take several minutes)"
  na_prepare || return
  # node-accelerator's XanMod kernel build runs `apt-get install` WITHOUT a lock
  # timeout, so a fresh-boot unattended-upgrades holding /var/lib/dpkg/lock-frontend
  # makes every kernel candidate fail instantly ("Could not get lock … held by
  # unattended-upgr"). Block here on apt's own wait until the lock is free before we
  # hand off, so the build actually gets its turn.
  info "Waiting for apt/unattended-upgrades to release the dpkg lock before the kernel build…"
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 check >/dev/null 2>&1 \
    || warn "dpkg lock still busy after 300s — XanMod may need a manual re-run of the optimize line below."
  [[ "$OPTIMIZE_TIMEOUT" == "0" ]] && info "optimize runs without a time cap (--optimize-timeout to set one)."
  [[ "$SKIP_CROWDSEC" == "1" ]] && info "Note: with --skip-crowdsec a 'CrowdSec bouncer not active' line is expected and harmless."
  if na_run optimize "$OPTIMIZE_TIMEOUT"; then
    ok "Network tuning (optimize) applied."
  else
    # Do NOT treat this as fatal: the node is already up and verified. A cap that
    # SIGKILLs apt/dpkg mid-install can also leave packages half-configured.
    warn "node-accelerator optimize did not finish cleanly — node is up and unaffected."
    warn "Finish it by hand anytime (this is exactly what works after a timeout):"
    warn "    sudo bash \"${NA_INSTALLER:-<node-accelerator>/install.sh}\" optimize"
    warn "If apt was interrupted: sudo dpkg --configure -a  then re-run the line above."
  fi
}
# Cascade back-end lockdown: restrict tcp/443 to the SNI-mirror front's egress
# IP(s). Lives in its OWN nft table (mirror_gate) so it is independent of the
# node-accelerator na_filter table — a `drop` verdict here is final regardless of
# what na_filter accepts, and na-firewall reloads only na_filter, so we persist
# mirror_gate with a tiny oneshot unit. Loopback (the node's own :443 self-probe)
# and established/related flows stay allowed; only NEW :443 from anyone but the
# front is dropped. No-op when FRONT_IP is empty. Run AFTER verify so the probe
# sees an open :443 first.
apply_front_gate() {
  [[ -n "$FRONT_IP" ]] || return 0
  step "Front-gate — restrict :443 to SNI-mirror front(s): $FRONT_IP"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: nft table inet mirror_gate — accept tcp/443 from { $FRONT_IP } (+lo,established), drop the rest"
    return 0
  fi
  command -v nft >/dev/null 2>&1 || { warn "nft not found — cannot apply front-gate; :443 stays open. Restrict it by hand."; return 0; }

  local -a v4=() v6=(); local e
  local -a _entries=(); IFS=',' read -r -a _entries <<< "$FRONT_IP"
  for e in "${_entries[@]}"; do
    [[ -n "$e" ]] || continue
    if [[ "$e" == *:* ]]; then v6+=("$e"); else v4+=("$e"); fi
  done
  local v4set="" v6set=""
  ((${#v4[@]})) && v4set="$(IFS=,; echo "${v4[*]}")"
  ((${#v6[@]})) && v6set="$(IFS=,; echo "${v6[*]}")"

  local f=/etc/mirror-gate.nft
  {
    echo "#!/usr/sbin/nft -f"
    echo "# Managed by remnawave-node.sh (--front-ip). Restricts tcp/443 to the SNI-mirror front(s)."
    # Create-then-delete makes the reload atomic and idempotent (delete never fails
    # on a missing table because the bare 'table' line creates it first).
    echo "table inet mirror_gate"
    echo "delete table inet mirror_gate"
    echo "table inet mirror_gate {"
    echo "    chain input {"
    echo "        type filter hook input priority -5; policy accept;"
    echo "        iif lo accept"
    echo "        tcp dport 443 ct state established,related accept"
    [[ -n "$v4set" ]] && echo "        tcp dport 443 ip saddr { $v4set } accept"
    [[ -n "$v6set" ]] && echo "        tcp dport 443 ip6 saddr { $v6set } accept"
    echo "        tcp dport 443 ct state new drop"
    echo "    }"
    echo "}"
  } > "$f"
  chmod 0644 "$f"

  if ! nft -f "$f"; then
    warn "Failed to load mirror_gate ruleset — :443 remains open. Inspect: nft -f $f"
    return 0
  fi

  # Persist across reboots (na-firewall.service reloads only na_filter, not this).
  cat > /etc/systemd/system/mirror-gate.service <<UNIT
[Unit]
Description=Restrict tcp/443 to SNI-mirror front(s) (remnawave-node --front-ip)
After=network-pre.target nftables.service na-firewall.service
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/nft -f $f
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl enable mirror-gate.service >/dev/null 2>&1 || true

  ok "Front-gate active — tcp/443 accepts only { $FRONT_IP } (+ loopback, established)."
}

# Fetch the firewall installer BEFORE any panel resource is created, so an
# unreachable node-accelerator (or dead network) fails early instead of leaving
# orphan panel Config Profile / Node / Host objects behind (L3).
preflight_external_deps() {
  [[ "$SKIP_FIREWALL" == "1" ]] && return 0
  step "Preflight — external dependencies"
  na_prepare || return 0
  ok "node-accelerator installer ready (fetched before touching the panel)."
}

# ── RKN/DPI hardening (NikitaAzmov/RKN-PROTECT, selected safe parts) ─────────
# Runs after the firewall so its own nft table sits alongside node-accelerator's
# rules. Idempotent. Deliberately omits RST-drop (breaks the panel<->node link);
# RST-injection defence is done at the stack level via tcp_rfc1337 instead.
apply_rkn_hardening() {
  [[ "$HARDENING" == "1" ]] || { info "RKN hardening skipped (--no-hardening)."; return 0; }
  step "RKN/DPI hardening (RST-protection, TTL=128, drop unused protocols, SSH banner, fail2ban)"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: sysctl tcp_rfc1337; nftables inet rknnode ttl=128; modprobe.d dccp/sctp/rds/tipc off; sshd DebianBanner no + Banner none; fail2ban sshd jail (port ${SSH_PORT:-22})"
    return
  fi

  # 1. sysctl — RST-injection defence + safe redirect/ICMP hardening. Does NOT
  # touch tcp_timestamps/BBR (node-accelerator owns congestion control).
  cat > /etc/sysctl.d/99-rkn-node.conf <<'EOF'
# RKN/TSPU RST-injection defence at the TCP-stack level (safe with BBR/Remnawave).
net.ipv4.tcp_rfc1337 = 1
net.ipv4.tcp_syncookies = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1
EOF
  sysctl -p /etc/sysctl.d/99-rkn-node.conf >/dev/null 2>&1 || true
  ok "sysctl RST-protection applied (tcp_rfc1337=1)."

  # 2. nftables TTL/hoplimit=128 in postrouting (after Docker NAT) — normalises
  # the hop count / masks the OS from TSPU. Own table => no conflict with the
  # firewall backend.
  if ! command -v nft >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nftables >/dev/null 2>&1 || true
    fi
  fi
  if command -v nft >/dev/null 2>&1; then
    local nftbin; nftbin="$(command -v nft)"
    mkdir -p /etc/nftables.d
    # `table … {}` + `delete` first (same create-then-flush idiom node-accelerator
    # uses) so re-runs replace the table instead of appending duplicate
    # `ip ttl set 128` rules into the same chain.
    cat > /etc/nftables.d/rkn-node.nft <<'NFT'
table inet rknnode {}
delete table inet rknnode
table inet rknnode {
    chain postrouting {
        type filter hook postrouting priority mangle; policy accept;
        ip ttl set 128
        ip6 hoplimit set 128
    }
}
NFT
    # Syntax-check first (nft -c) so a bad ruleset is caught before it is applied.
    if ! "$nftbin" -c -f /etc/nftables.d/rkn-node.nft 2>/dev/null; then
      warn "RKN nft ruleset failed validation (nft -c) — skipping TTL=128 normalization."
    elif "$nftbin" -f /etc/nftables.d/rkn-node.nft 2>/dev/null; then
      ok "nftables TTL/hoplimit=128 applied (table inet rknnode)."
      cat > /etc/systemd/system/rkn-node-nft.service <<UNIT
[Unit]
Description=RKN node nftables TTL normalization
After=network-pre.target
Wants=network-pre.target
[Service]
Type=oneshot
ExecStart=${nftbin} -f /etc/nftables.d/rkn-node.nft
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
UNIT
      systemctl daemon-reload >/dev/null 2>&1 || true
      systemctl enable rkn-node-nft.service >/dev/null 2>&1 || true
    else
      warn "Could not load nftables TTL rule — skipping (kernel without nft?)."
    fi
  else
    warn "nft unavailable — skipping TTL=128 normalization."
  fi

  # 3. Disable unused network protocols (attack-surface reduction, Lynis NETW-3200).
  cat > /etc/modprobe.d/rkn-unused-protocols.conf <<'EOF'
install dccp /bin/false
install sctp /bin/false
install rds /bin/false
install tipc /bin/false
EOF
  local p
  for p in dccp sctp rds tipc; do
    lsmod 2>/dev/null | grep -q "^$p " && modprobe -r "$p" 2>/dev/null || true
  done
  ok "Unused protocols disabled (dccp/sctp/rds/tipc)."

  # 4. SSH banner minimization — drop the "-Debian/-Ubuntu" suffix and any login
  # banner so passive scanners (Shodan/Censys) glean less OS detail. Cosmetic:
  # OpenSSH still emits its own version in the protocol greeting.
  local sshd=/etc/ssh/sshd_config
  if [[ -f "$sshd" ]]; then
    backup_file "$sshd"
    if grep -qiE '^[[:space:]]*DebianBanner' "$sshd"; then
      sed -i 's/^[[:space:]]*[Dd]ebian[Bb]anner.*/DebianBanner no/' "$sshd"
    else
      printf '\nDebianBanner no\n' >> "$sshd"
    fi
    if grep -qiE '^[[:space:]]*Banner[[:space:]]' "$sshd"; then
      sed -i 's/^[[:space:]]*Banner[[:space:]].*/Banner none/' "$sshd"
    else
      printf 'Banner none\n' >> "$sshd"
    fi
    systemctl reload sshd >/dev/null 2>&1 || systemctl reload ssh >/dev/null 2>&1 || true
    ok "SSH banner minimized (DebianBanner no, Banner none)."
  fi

  # 5. fail2ban — SSH brute-force protection. node-accelerator IP-restricts the
  # panel port but leaves SSH exposed to the world, so ban repeat offenders.
  if ! command -v fail2ban-server >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban >/dev/null 2>&1 || true
  fi
  if command -v fail2ban-server >/dev/null 2>&1; then
    mkdir -p /etc/fail2ban/jail.d
    cat > /etc/fail2ban/jail.d/remnanode-sshd.conf <<F2B
[sshd]
enabled  = true
port     = ${SSH_PORT:-22}
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
F2B
    systemctl enable fail2ban >/dev/null 2>&1 || true
    systemctl restart fail2ban >/dev/null 2>&1 || true
    ok "fail2ban SSH jail active (port ${SSH_PORT:-22}, 5 tries/10m → 1h ban)."
  else
    warn "fail2ban unavailable — skipping SSH brute-force protection."
  fi
}

# ── Verify ──────────────────────────────────────────────────────────────────
verify() {
  step "Verification"
  command -v docker >/dev/null 2>&1 && docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true
  local running_core
  running_core="$(docker exec "$NODE_CONTAINER" /usr/local/bin/rw-core version 2>/dev/null | head -n 1 || true)"
  if [[ "$running_core" == "Xray ${XRAY_CORE_VERSION} "* ]]; then
    ok "Running core is pinned correctly: $running_core"
  else
    die "Running core is NOT Xray ${XRAY_CORE_VERSION}: ${running_core:-unreadable}. Check the /usr/local/bin/xray bind mount."
  fi
  if [[ -f "$NGINX_DIR/ssl/fullchain.crt" ]] && command -v openssl >/dev/null 2>&1; then
    openssl x509 -in "$NGINX_DIR/ssl/fullchain.crt" -noout -subject -dates || true
  fi
  if command -v curl >/dev/null 2>&1; then
    # Active-probe the public 443 with the real SNI, exactly as a censor would:
    # a plain TLS client fails Reality auth and is forwarded to the decoy. A 200
    # with the decoy HTML proves the whole chain (Xray → fallback → nginx) works.
    info "Active probe (public :443, real SNI → decoy):"
    # RemnaNode can be running while the panel is still delivering the profile
    # and Xray has not bound :443 yet. Retry briefly before treating it as a
    # listener or firewall fault.
    local probe_ok=0 probe_try probe_out=""
    for probe_try in 1 2 3 4 5; do
      # Capture attempts quietly: a refused first try while Xray is still
      # binding :443 is expected and must not read like a failure.
      if probe_out=$(curl -ksS --resolve "$DOMAIN:443:$NODE_PUBLIC_IP" "https://$DOMAIN/" -I --max-time 10 2>&1); then
        probe_ok=1
        printf '%s\n' "$probe_out"
        ok "Decoy answered on public :443 (attempt ${probe_try}/5) — Xray → fallback → nginx chain works."
        break
      fi
      if (( probe_try < 5 )); then
        info "Not listening yet — waiting for the panel to deliver the Xray profile (${probe_try}/5)…"
        sleep 3
      fi
    done
    if (( ! probe_ok )); then
      warn "Probe failed 5/5 times; last error: ${probe_out}"
      # The probe hits our OWN public IP from the box itself. On NAT/CGNAT the
      # hairpin (looping to the external IP from inside) often refuses even when
      # outside clients connect fine. Re-probe the same chain over loopback: if the
      # decoy answers there, the local Xray→fallback→nginx chain works and only the
      # self-hairpin failed — a soft note, not a listener/firewall fault.
      if curl -k --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/" -I --max-time 8 >/dev/null 2>&1; then
        warn "Could not reach our own public IP on :443 — normal on NAT/CGNAT (broken hairpin). The decoy answers over loopback, so the node itself is fine."
        warn "This does NOT prove EXTERNAL reachability — verify :443 from an OUTSIDE host, e.g.:  openssl s_client -connect ${NODE_PUBLIC_IP}:443 -servername ${DOMAIN}"
      else
        warn "Public :443 unreachable AND loopback :443 does not serve the decoy — a real fault below TLS/Xray: check listener, nftables, and provider firewall."
        info "Listeners expected on 80/443 (and XHTTP when selected):"
        ss -ltnp 2>/dev/null | grep -E ":(80|443|${RENEW_PORT}|${XHTTP_PORT:-8444}|${NODE_PORT})\b" || true
        if command -v nft >/dev/null 2>&1; then
          info "nftables input rules mentioning TCP/443:"
          nft list table inet na_filter 2>/dev/null | grep -E -i 'tcp.*443|443.*tcp' || true
        fi
        info "Recent RemnaNode/Xray logs:"
        docker logs "$NODE_CONTAINER" --tail 40 2>&1 || true
        docker exec "$NODE_CONTAINER" xlogs 2>&1 | tail -40 || true
      fi
    fi
    # A working :443 fallback alone does not prove `both`: the panel can deliver
    # the raw Reality inbound while the XHTTP inbound is absent or has a bad port.
    # Confirm every selected Xray inbound after the short delivery window above.
    local spec check_port missing_ports=""
    for spec in "${ACTIVE_INBOUNDS[@]}"; do
      IFS=: read -r _ check_port _ <<< "$spec"
      port_listening "$check_port" || missing_ports+="${missing_ports:+, }$check_port"
    done
    if [[ -n "$missing_ports" ]]; then
      warn "Expected Xray inbound listener(s) missing: $missing_ports. Check Node → active Config Profile/Inbounds and Xray logs."
    else
      info "Xray inbound listener(s) present: ${ACTIVE_INBOUNDS[*]}."
    fi
    if [[ "$MASK" == "grpc-tls" ]]; then
      info "Health endpoint (nginx):"
      curl -k --resolve "$DOMAIN:443:$NODE_PUBLIC_IP" "https://$DOMAIN/health" --max-time 8 || true
      log
      info "gRPC upstream: 127.0.0.1:${GRPC_PORT} $(port_listening "$GRPC_PORT" && echo "(listening)" || echo "(NOT listening — Xray may still be starting)")"
      info "Client host: security=TLS network=gRPC serviceName=${GRPC_SERVICE} alpn=h2 fp=${FP}"
    elif [[ "$MODE" == "tcp" ]]; then
      info "Local selfsteal probe (loopback, PROXY protocol):"
      curl -k --haproxy-protocol --resolve "$DOMAIN:$SELFSTEAL_PORT:127.0.0.1" \
        "https://$DOMAIN:$SELFSTEAL_PORT/" -I --max-time 8 || true
    else
      info "nginx unix socket: $([[ -S "$SOCKET_PATH" ]] && echo "$SOCKET_PATH present" || echo "MISSING $SOCKET_PATH")"
    fi
  fi
  # Firewall proof: the strict table must be live and the auto-rollback timer must
  # NOT be — an active safety timer can delete na_filter minutes after we finish.
  if [[ "$SKIP_FIREWALL" != "1" ]] && command -v nft >/dev/null 2>&1; then
    if nft list table inet na_filter >/dev/null 2>&1; then
      info "Firewall: nft table inet na_filter active (NODE_PORT $NODE_PORT restricted)."
    else
      warn "Firewall: nft table inet na_filter NOT present — NODE_PORT may be exposed."
    fi
    if na_safety_timer_active; then
      warn "Firewall: na-fw-safety.timer STILL ACTIVE — it may remove na_filter. Stop it: systemctl stop na-fw-safety.timer"
    else
      info "Firewall: safety timer inactive/not-found (rules are permanent)."
    fi
  fi
  # Network tuning proof. optimize (XanMod/BBRv3) is best-effort and may have timed
  # out or been skipped — never assume it worked. Show the ACTUAL running state so
  # there's no illusion that "optimize did everything".
  if command -v sysctl >/dev/null 2>&1; then
    local cc qd kern
    cc="$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo '?')"
    qd="$(sysctl -n net.core.default_qdisc 2>/dev/null || echo '?')"
    kern="$(uname -r 2>/dev/null || echo '?')"
    if [[ "$cc" == bbr* ]]; then
      info "Congestion control: $cc (qdisc $qd) — BBR active."
    else
      warn "Congestion control: $cc (qdisc $qd) — BBR NOT active. optimize may have failed/timed out; enable BBR manually (sysctl net.ipv4.tcp_congestion_control=bbr) or re-run optimize."
    fi
    case "$kern" in
      *xanmod*) info "Kernel: $kern (XanMod — BBRv3 capable).";;
      *)
        # XanMod installed but not booted yet: optimize installs the package, the
        # new kernel only takes over after a reboot. Say so explicitly instead of
        # letting "stock kernel" read as a failure.
        # Match xanmod anywhere in the package name: the kernel package is
        # linux-image-<ver>-xanmod<N> (not "linux-xanmod-*", which is only the meta).
        if dpkg -l 2>/dev/null | grep -qiE '^ii\s.*xanmod'; then
          warn "Kernel: $kern — XanMod is installed but NOT running. Reboot, then check 'uname -r' contains 'xanmod'."
        else
          info "Kernel: $kern (stock — XanMod not installed; plain BBR works on any 4.9+ kernel, BBRv3 needs XanMod)."
        fi
        ;;
    esac
  fi
  command -v crontab >/dev/null 2>&1 && { info "Renewal cron:"; crontab -l 2>/dev/null | grep -E 'acme-renew|acme' || warn "no renewal cron found"; }
  [[ -x /usr/local/bin/remnanode ]] && info "Manage this node with: remnanode  (status | logs | restart | template | renew | uninstall)" || true
}

# ── Maintenance: logrotate + container auto-restart watchdog ────────────────
setup_maintenance() {
  step "Maintenance (logrotate + auto-restart)"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: /etc/logrotate.d/remnawave-node + auto-restart cron"
    return
  fi
  cat > /etc/logrotate.d/remnawave-node <<EOF
${NODE_DIR}/logs/*.log ${NGINX_DIR}/logs/*.log {
    daily
    rotate 7
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
EOF
  ok "logrotate configured (/etc/logrotate.d/remnawave-node)."
  # Watchdog: restart:always covers crashes; this catches a container stuck in a
  # non-running state (exited/created) that compose won't auto-heal.
  local wd="$STATE_DIR/watchdog.sh"
  mkdir -p "$STATE_DIR"
  cat > "$wd" <<EOF
#!/usr/bin/env bash
for c in ${NODE_CONTAINER} ${NGINX_CONTAINER}; do
  st="\$(docker inspect -f '{{.State.Status}}' "\$c" 2>/dev/null || echo missing)"
  [ "\$st" = running ] || docker start "\$c" >/dev/null 2>&1 || true
done
EOF
  chmod 700 "$wd"
  install_cron_once "$wd" "*/5 * * * * $wd" "Auto-restart watchdog cron installed (*/5 min)."
}

# ── Management CLI (/usr/local/bin/remnanode) ───────────────────────────────
install_cli() {
  step "Management CLI (/usr/local/bin/remnanode)"
  if [[ "$DRY_RUN" == "1" ]]; then
    info "DRY-RUN: write $STATE_DIR/config.env + /usr/local/bin/remnanode"
    return
  fi
  mkdir -p "$STATE_DIR"
  cat > "$STATE_DIR/config.env" <<EOF
NODE_DIR="$NODE_DIR"
NGINX_DIR="$NGINX_DIR"
STATE_DIR="$STATE_DIR"
NODE_CONTAINER="$NODE_CONTAINER"
NGINX_CONTAINER="$NGINX_CONTAINER"
DOMAIN="$DOMAIN"
NODE_PUBLIC_IP="$NODE_PUBLIC_IP"
MASK="$MASK"
GRPC_SERVICE="$GRPC_SERVICE"
GRPC_PORT="$GRPC_PORT"
MODE="$MODE"
SOCKET_PATH="$SOCKET_PATH"
SELFSTEAL_PORT="$SELFSTEAL_PORT"
ACME_HOME="$ACME_HOME"
TEMPLATES_REPO="$TEMPLATES_REPO"
TEMPLATE_FOLDERS="${TEMPLATE_FOLDERS[*]}"
EOF
  write_cli_script /usr/local/bin/remnanode
  chmod +x /usr/local/bin/remnanode
  ok "CLI installed — run 'remnanode' for the menu, or 'remnanode status'."
}

# The CLI is a standalone script; it sources $STATE_DIR/config.env at runtime.
write_cli_script() {
  local out="$1"
  cat > "$out" <<'CLI'
#!/usr/bin/env bash
# remnawave-node management CLI. Generated by remnawave-node.sh.
set -uo pipefail
CFG="/opt/remnawave-node/state/config.env"
[[ -f "$CFG" ]] || { echo "config missing: $CFG (is the node installed?)"; exit 1; }
# shellcheck disable=SC1090
. "$CFG"
read -r -a TPL_ARR <<< "${TEMPLATE_FOLDERS:-}"

if [[ -t 1 ]]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; C=$'\033[36m'; Z=$'\033[0m'
else B="" G="" Y="" R="" C="" Z=""; fi
say(){ printf '%s\n' "$*"; }
ok(){ say "${G}[+]${Z} $*"; }
warn(){ say "${Y}[!]${Z} $*"; }
err(){ say "${R}[x]${Z} $*" >&2; }

dc_node(){ ( cd "$NODE_DIR" && docker compose "$@" ); }
dc_nginx(){ ( cd "$NGINX_DIR" && docker compose "$@" ); }

cmd_status(){
  say "${B}== Containers ==${Z}"
  docker ps -a --filter "name=${NODE_CONTAINER}" --filter "name=${NGINX_CONTAINER}" \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null
  say ""; say "${B}== Selfsteal ==${Z}"
  say "  domain:   $DOMAIN"
  say "  mask:     ${MASK:-reality}"
  if [[ "${MASK:-reality}" == "grpc-tls" ]]; then
    say "  grpc:     127.0.0.1:${GRPC_PORT:-11443} (service ${GRPC_SERVICE:-grpc})"
  elif [[ "$MODE" == "tcp" ]]; then say "  target:   127.0.0.1:${SELFSTEAL_PORT}"
  else say "  socket:   $([[ -S "$SOCKET_PATH" ]] && echo "$SOCKET_PATH (present)" || echo "$SOCKET_PATH MISSING")"; fi
  local crt="$NGINX_DIR/ssl/fullchain.crt"
  if [[ -f "$crt" ]] && command -v openssl >/dev/null 2>&1; then
    say "  cert:     $(openssl x509 -in "$crt" -noout -enddate 2>/dev/null | cut -d= -f2)"
  fi
  say ""; say "${B}== Active probe (:443 → decoy) ==${Z}"
  local code
  code="$(curl -k -s -o /dev/null -w '%{http_code}' --resolve "$DOMAIN:443:$NODE_PUBLIC_IP" "https://$DOMAIN/" --max-time 10 2>/dev/null || echo 000)"
  [[ "$code" == 200 ]] && ok "decoy served (HTTP $code)" || warn "probe returned HTTP $code"
  if [[ -f "$STATE_DIR/node.json" ]] && command -v jq >/dev/null 2>&1; then
    say ""; say "${B}== Panel resources ==${Z}"; jq -r 'to_entries[]|"  \(.key): \(.value)"' "$STATE_DIR/node.json" 2>/dev/null
  fi
}
cmd_logs(){
  local t="${1:-all}" follow=""; [[ "${2:-}" == "-f" || "${1:-}" == "-f" ]] && follow="-f"
  case "$t" in
    node) docker logs $follow --tail 100 "$NODE_CONTAINER" ;;
    nginx) docker logs $follow --tail 100 "$NGINX_CONTAINER" ;;
    *) say "${B}-- node --${Z}"; docker logs --tail 40 "$NODE_CONTAINER" 2>&1 | tail -40
       say "${B}-- nginx --${Z}"; docker logs --tail 40 "$NGINX_CONTAINER" 2>&1 | tail -40 ;;
  esac
}
cmd_up(){ dc_nginx up -d && dc_node up -d && ok "started"; }
cmd_down(){ dc_node down; dc_nginx down; ok "stopped"; }
cmd_restart(){ dc_nginx restart && dc_node restart && ok "restarted"; }
cmd_renew(){
  local w="$NGINX_DIR/acme-renew.sh"
  [[ -x "$w" ]] || { err "renew wrapper not found: $w"; return 1; }
  say "Running $w …"; "$w" && ok "renewal finished" || warn "renewal reported issues (see output)"
}
cmd_template(){
  local sel="${1:-}"
  if [[ -z "$sel" ]]; then
    say "${B}Templates:${Z}"; local i=1
    for t in "${TPL_ARR[@]}"; do printf "  %2d) %s\n" "$i" "$t"; i=$((i+1)); done
    say "Usage: remnanode template <id|name>"; return 0
  fi
  local folder=""
  if [[ "$sel" =~ ^[0-9]+$ ]]; then (( sel>=1 && sel<=${#TPL_ARR[@]} )) && folder="${TPL_ARR[$((sel-1))]}"
  else for t in "${TPL_ARR[@]}"; do [[ "$t" == "$sel" ]] && folder="$t"; done; fi
  [[ -n "$folder" ]] || { err "unknown template '$sel'"; return 1; }
  local html="$NGINX_DIR/html" tmp; tmp="$(mktemp -d)"
  say "Fetching '$folder'…"
  local top="remnawave-scripts-main/sni-templates/$folder"
  if curl -fsSL --max-time 120 "https://codeload.github.com/${TEMPLATES_REPO}/tar.gz/refs/heads/main" -o "$tmp/t.tgz" 2>/dev/null \
     && tar -xzf "$tmp/t.tgz" -C "$tmp" "$top" 2>/dev/null && [[ -f "$tmp/$top/index.html" ]]; then
    rm -rf "${html:?}/"*; cp -a "$tmp/$top/." "$html/"
    _mutate "$html"
    docker exec "$NGINX_CONTAINER" nginx -s reload 2>/dev/null || dc_nginx restart >/dev/null 2>&1
    ok "template '$folder' installed"
  else err "fetch failed"; fi
  rm -rf "$tmp"
}
_mutate(){
  local d="$1"; command -v openssl >/dev/null 2>&1 || return 0
  find "$d" -type f \( -iname '*.md' -o -iname '*.map' -o -iname 'LICENSE*' \) -delete 2>/dev/null || true
  local deg=$(( (RANDOM%300)+30 )) sat=$(( (RANDOM%30)+90 ))
  local f
  while IFS= read -r -d '' f; do
    sed -i "s#https\?://api\.ipify\.org[^\"')]*#/_s/ip#g" "$f" 2>/dev/null || true
    sed -i "s#/vite\.svg#/favicon.svg#g" "$f" 2>/dev/null || true
    sed -i "s#</head>#<style>html{filter:hue-rotate(${deg}deg) saturate(${sat}%)}img,picture,video,svg,canvas{filter:hue-rotate(-${deg}deg)}</style><!-- $(openssl rand -hex 6) --></head>#I" "$f" 2>/dev/null || true
  done < <(find "$d" -type f -iname '*.html' -print0 2>/dev/null)
}
cmd_uninstall(){
  warn "This removes the node + selfsteal containers and files on THIS server."
  warn "Panel resources (node/host/profile) are NOT touched."
  read -r -p "Type the domain ($DOMAIN) to confirm: " a
  [[ "$a" == "$DOMAIN" ]] || { say "aborted"; return 1; }
  # Refuse to rm anything unexpected: a truncated/edited config.env with an empty
  # or '/'-rooted path must never turn this into a root 'rm -rf /'. Both the :?
  # guards and the /opt/ prefix check below are belt-and-suspenders.
  case "$NODE_DIR:$NGINX_DIR:$STATE_DIR" in
    /opt/*:/opt/*:/opt/*) : ;;
    *) err "refusing to uninstall: unexpected paths (NODE_DIR=$NODE_DIR NGINX_DIR=$NGINX_DIR STATE_DIR=$STATE_DIR)"; return 1 ;;
  esac
  dc_node down 2>/dev/null; dc_nginx down 2>/dev/null
  rm -rf "${NODE_DIR:?}" "${NGINX_DIR:?}"
  crontab -l 2>/dev/null | grep -vF "$NGINX_DIR/acme-renew.sh" | grep -vF "$STATE_DIR/watchdog.sh" | crontab - 2>/dev/null || true
  rm -f /etc/logrotate.d/remnawave-node /usr/local/bin/remnanode
  rm -rf "${STATE_DIR:?}"
  ok "removed. (Delete the node/host in the panel manually if desired.)"
}
cmd_menu(){
  while true; do
    say ""; say "${C}${B}remnawave-node${Z} — $DOMAIN ($MODE)"
    say "  1) status     2) logs      3) restart   4) up"
    say "  5) down       6) template  7) renew     8) uninstall   0) exit"
    read -r -p "> " c
    case "$c" in
      1) cmd_status ;; 2) cmd_logs all ;; 3) cmd_restart ;; 4) cmd_up ;;
      5) cmd_down ;; 6) read -r -p "template id/name (blank=list): " t; cmd_template "$t" ;;
      7) cmd_renew ;; 8) cmd_uninstall ;; 0|q) break ;; *) warn "?" ;;
    esac
  done
}
case "${1:-menu}" in
  status) cmd_status ;;
  logs) shift; cmd_logs "$@" ;;
  up) cmd_up ;; down) cmd_down ;; restart) cmd_restart ;;
  renew) cmd_renew ;;
  template) shift; cmd_template "${1:-}" ;;
  uninstall) cmd_uninstall ;;
  menu|"") cmd_menu ;;
  -h|--help|help) say "remnanode {status|logs [node|nginx] [-f]|up|down|restart|template [id|name]|renew|uninstall|menu}" ;;
  *) err "unknown command: $1"; exit 1 ;;
esac
CLI
}

# ── CLI ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<HELP
remnawave-node.sh $INSTALLER_VERSION — Remnawave 3.3.2 selfsteal node installer + panel API automation.

Usage:
  sudo bash remnawave-node.sh [options]

Required (prompted if omitted, unless -y):
  --domain <d>            selfsteal domain (A-record must point here)
  --panel-url <url>       Remnawave panel base URL
  --panel-token-file <p>  read the panel API token from a file (preferred — not in \`ps\`)
                          also: REMNAWAVE_PANEL_TOKEN_FILE / REMNAWAVE_PANEL_TOKEN env
  --panel-token <token>   panel API token on the CLI (back-compat; VISIBLE in \`ps\`)
  --whitelist <ip[,ip]>   panel IP(s)/CIDR allowed to reach NODE_PORT
  --front-ip <ip[,ip]>    cascade back-end: lock tcp/443 to the SNI-mirror front's
                          egress IP(s) (nft table mirror_gate). Empty = 443 open to all.
                          Loopback + established connections stay allowed.

Certificate:
  --cert-mode <m>         le443 (default) | cf-dns
  --cf-token <token>      Cloudflare API token (Zone:DNS:Edit) — required for cf-dns
  --acme-email <email>    Let's Encrypt account email
  --secret-key <key>      node SECRET_KEY (bypass /api/keygen if token lacks scope)
  --template <id|name>    decoy site: 'builtin' (default, self-generated, no fetch),
                          or an sni-templates id 1-11 / name: ${TEMPLATE_FOLDERS[*]}
  --no-randomize          do not byte-uniquify the template (keep it identical
                          to the public sni-templates copy — not recommended)
  --randomize             force template randomization ON (inverse of --no-randomize;
                          overrides saved state on resume)
  --socket                nginx fallback via unix socket $SOCKET_PATH (default;
                          shared with the node over /dev/shm, no loopback port)
  --tcp                   nginx fallback via loopback TCP 127.0.0.1:<selfsteal-port>
  --mask <m>              masking model: reality (default — Xray owns 443, XTLS-Reality)
                          | grpc-tls (nginx owns 443 with a real cert, VLESS+gRPC
                          behind it; CDN/Cloudflare-frontable, real decoy site)
  --grpc-port <p>         loopback port for the gRPC inbound (grpc-tls; default $DEFAULT_GRPC_PORT)
  --grpc-service <name>   gRPC serviceName (grpc-tls; default $DEFAULT_GRPC_SERVICE) — nginx
                          routes /<name>/Tun to Xray; change it in both places together
  --transport <t>         reality mask only: tcp (default, Reality+Vision) | xhttp | both
  --xhttp-port <p>        XHTTP inbound port in 'both' mode (default 8444)
  --no-hardening          skip RKN/DPI hardening (tcp_rfc1337, TTL=128, drop unused protos, SSH banner, fail2ban)
  --hardening             force hardening ON (inverse of --no-hardening; overrides saved state on resume)
  --no-geo                do not download/mount runetfreedom geosite/geoip
  --geo                   force geo download ON (inverse of --no-geo)
  --rotate-keys           generate a fresh Reality keypair (existing clients must
                          resync — new publicKey); default reuses existing keys
  --no-rotate-keys        keep the existing Reality keypair (inverse of --rotate-keys;
                          overrides saved state on resume)
  --adopt-profile         allow overwriting a DIFFERENTLY-NAMED config-profile that
                          already owns this install's inbound tag; default refuses
                          (never touches a foreign profile silently)

Optional:
  --country <CC>          ISO-2 country code (default $DEFAULT_COUNTRY)
  --node-name <name>      panel node name (prompted; default <CC>-<seq> auto)
  --host-remark <text>    subscription host label (prompted; default "<node> REALITY")
  --host-address <a>      host connect address (default: DOMAIN for grpc-tls, public IP for reality)
  --node-public-ip <ip>   node public IP (default: auto-detect; needed behind NAT)
  --node-image <ref>      RemnaNode image (default pinned $NODE_IMAGE; or REMNANODE_IMAGE)
  --namespace-hash        append a NODE_NAME hash to tag namespace (default ON for fresh
                          installs → globally-unique tags; forced OFF when resuming
                          pre-hash state so a deployed node is not re-tagged)
  --no-namespace-hash     disable the namespace hash (inverse; overrides saved state on resume)
  --squad-name <name>     Internal Squad to enable the inbound in (users then see it)
  --squad-uuid <uuid>     Internal Squad by UUID (alternative to --squad-name)

Cascade bridge (make this an EXIT node that also accepts SS traffic from an entry node):
  --bridge                stand up a Shadowsocks bridge inbound on this node and, at the
                          end, print a ready entry-node Xray config (split-tunnel → bridge)
  --bridge-entry-ip <ip>  entry node public IP allowed to reach the bridge port (added to
                          the firewall whitelist; the port is NOT opened to the internet)
  --bridge-ss-port <p>    SS bridge inbound port (default $BRIDGE_SS_PORT)
  --bridge-user <name>    panel username to create/reuse; its ssPassword becomes the bridge
                          secret (username: 3-36 chars, letters/numbers/_/-)
  --entry-domain <d>      entry node's own selfsteal domain (used only in the printed config)
  --profile-name <name>   config-profile name (prompted; letters/numbers/_/-/space only)
  --node-port <p>         panel<->node control port (default $DEFAULT_NODE_PORT)
  --selfsteal-port <p>    local nginx HTTPS port (default $DEFAULT_SELFSTEAL_PORT)
  --renew-port <p>        LE443 renewal TLS-ALPN port (default $DEFAULT_RENEW_PORT)
  --ssh-port <p>          SSH port (auto-detected)
  --fingerprint <fp>      client fingerprint for host (default $DEFAULT_FP)
  --tcp-ports <list>      firewall TCP ports (default $DEFAULT_TCP_PORTS)
  --udp-ports <list>      firewall UDP ports (default $DEFAULT_UDP_PORTS)
  --na-ref <ref>          node-accelerator git ref (default $DEFAULT_NA_REF; installer
                          + modules both come from this ref)
  --node-accelerator-url <url>  override the node-accelerator install.sh URL
  --node-accelerator-dir <dir>  use a local node-accelerator checkout (offline)
  --node-accelerator-tar <tgz>  use a local node-accelerator tarball (offline)
  --skip-firewall         do not run node-accelerator
  --firewall              force firewall ON (inverse of --skip-firewall)
  --skip-crowdsec         tell node-accelerator to skip CrowdSec (default; avoids APT hangs / rc=143)
  --crowdsec              enable CrowdSec in node-accelerator protect (slower; may hit the phase timeout)
  --crowdsec-timeout <s>  cap the protect phase in seconds (default 180)
  --optimize-timeout <s>  cap the best-effort optimize phase in seconds (default 0 = unlimited;
                          optimize runs before node provisioning)
  --skip-xray-validate    do not validate the generated Xray config with 'xray -test'
  --skip-update           do not full-upgrade the OS / enable automatic security updates
  --resume                skip already-completed expensive stages ($STATE_DIR/stages)
                          and reload saved inputs (incl. token) from
                          $STATE_DIR/inputs.env (chmod 600) — usually just:
                          sudo bash remnawave-node.sh --resume -y
  --refresh-decoy         regenerate the decoy site even on --resume
  --geo-timeout <s>       overall cap for the geo-download stage (default $GEO_TIMEOUT; on
                          timeout geo is skipped and Xray keeps its bundled data)
  --preflight             read-only checks (OS/DNS/ports/panel), mutate nothing, then exit
  -y, --non-interactive   no prompts (fail if a required value is missing)
  --dry-run               print actions, change nothing
  -h, --help              this help
HELP
}
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --domain) DOMAIN="$2"; shift 2 ;;
      --panel-url) PANEL_URL="$2"; shift 2 ;;
      --panel-token) PANEL_TOKEN="$2"; PANEL_TOKEN_FROM_CLI=1; shift 2 ;;
      --panel-token-file) PANEL_TOKEN_FILE="$2"; shift 2 ;;
      --whitelist) PANEL_WHITELIST="${2//[[:space:]]/}"; shift 2 ;;
      --front-ip) FRONT_IP="${2//[[:space:]]/}"; CLI_SET[FRONT_IP]=1; shift 2 ;;
      --cert-mode) CERT_MODE="$2"; shift 2 ;;
      --cf-token) CF_TOKEN="$2"; shift 2 ;;
      --acme-email) ACME_EMAIL="$2"; shift 2 ;;
      --secret-key) SECRET_KEY_OVERRIDE="$2"; shift 2 ;;
      --template) TEMPLATE="$2"; shift 2 ;;
      --no-randomize) RANDOMIZE=0; CLI_SET[RANDOMIZE]=1; shift ;;
      --randomize) RANDOMIZE=1; CLI_SET[RANDOMIZE]=1; shift ;;
      --socket) MODE="socket"; shift ;;
      --tcp) MODE="tcp"; shift ;;
      --rotate-keys) ROTATE_KEYS=1; CLI_SET[ROTATE_KEYS]=1; shift ;;
      --no-rotate-keys) ROTATE_KEYS=0; CLI_SET[ROTATE_KEYS]=1; shift ;;
      --adopt-profile) ADOPT_PROFILE=1; shift ;;
      --mask) MASK="$2"; shift 2 ;;
      --grpc-port) GRPC_PORT="$2"; shift 2 ;;
      --grpc-service) GRPC_SERVICE="$2"; shift 2 ;;
      --no-hardening) HARDENING=0; CLI_SET[HARDENING]=1; shift ;;
      --hardening) HARDENING=1; CLI_SET[HARDENING]=1; shift ;;
      --transport) TRANSPORT="$2"; shift 2 ;;
      --xhttp-port) XHTTP_PORT="$2"; shift 2 ;;
      --no-geo) GEO=0; CLI_SET[GEO]=1; shift ;;
      --geo) GEO=1; CLI_SET[GEO]=1; shift ;;
      --country) COUNTRY="$2"; shift 2 ;;
      --node-name) NODE_NAME="$2"; shift 2 ;;
      --host-remark) HOST_REMARK="$2"; shift 2 ;;
      --host-address) HOST_ADDRESS="$2"; shift 2 ;;
      --node-public-ip) NODE_PUBLIC_IP="$2"; shift 2 ;;
      --node-image) NODE_IMAGE="$2"; shift 2 ;;
      --namespace-hash) NAMESPACE_HASH=1; CLI_SET[NAMESPACE_HASH]=1; shift ;;
      --no-namespace-hash) NAMESPACE_HASH=0; CLI_SET[NAMESPACE_HASH]=1; shift ;;
      --squad-name) SQUAD_NAME="$2"; shift 2 ;;
      --squad-uuid) SQUAD_UUID="$2"; shift 2 ;;
      --bridge) BRIDGE=1; shift ;;
      --bridge-entry-ip) BRIDGE_ENTRY_IP="${2//[[:space:]]/}"; BRIDGE=1; shift 2 ;;
      --bridge-ss-port) BRIDGE_SS_PORT="$2"; BRIDGE=1; shift 2 ;;
      --bridge-user) BRIDGE_USER="$2"; BRIDGE=1; shift 2 ;;
      --entry-domain) ENTRY_DOMAIN="$2"; BRIDGE=1; shift 2 ;;
      --profile-name) PROFILE_NAME="$2"; shift 2 ;;
      --node-port) NODE_PORT="$2"; shift 2 ;;
      --selfsteal-port) SELFSTEAL_PORT="$2"; shift 2 ;;
      --renew-port) RENEW_PORT="$2"; shift 2 ;;
      --ssh-port) SSH_PORT="$2"; shift 2 ;;
      --fingerprint) FP="$2"; shift 2 ;;
      --tcp-ports) TCP_PORTS="${2//[[:space:]]/}"; shift 2 ;;
      --udp-ports) UDP_PORTS="${2//[[:space:]]/}"; shift 2 ;;
      --na-ref) NA_REF="$2"; shift 2 ;;
      --skip-firewall) SKIP_FIREWALL=1; CLI_SET[SKIP_FIREWALL]=1; shift ;;
      --firewall) SKIP_FIREWALL=0; CLI_SET[SKIP_FIREWALL]=1; shift ;;
      --skip-crowdsec) SKIP_CROWDSEC=1; CLI_SET[SKIP_CROWDSEC]=1; shift ;;
      --crowdsec) SKIP_CROWDSEC=0; CLI_SET[SKIP_CROWDSEC]=1; shift ;;
      --skip-xray-validate) SKIP_XRAY_VALIDATE=1; shift ;;
      --skip-update) SKIP_UPDATE=1; shift ;;
      --node-accelerator-url) NA_URL="$2"; shift 2 ;;
      --node-accelerator-dir) NA_DIR="$2"; shift 2 ;;
      --node-accelerator-tar) NA_TAR="$2"; shift 2 ;;
      --crowdsec-timeout) CROWDSEC_TIMEOUT="$2"; CROWDSEC_TIMEOUT_SET=1; shift 2 ;;
      --optimize-timeout) OPTIMIZE_TIMEOUT="$2"; shift 2 ;;
      --resume) RESUME=1; shift ;;
      --refresh-decoy) REFRESH_DECOY=1; shift ;;
      --geo-timeout) GEO_TIMEOUT="$2"; shift 2 ;;
      --preflight) PREFLIGHT=1; DRY_RUN=1; shift ;;
      -y|--non-interactive) NONINTERACTIVE=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown argument: $1 (see --help)" ;;
    esac
  done
}

# Build inbound tags + ACTIVE_INBOUNDS from NODE_NAME / MASK / TRANSPORT. Idempotent
# and re-runnable (used by collect_inputs and by the plan editor when the user
# changes node-name/country/mask/transport).
compute_inbounds() {
  # Inbound tags must be GLOBALLY UNIQUE in Remnawave. A plain "<CC>-REALITY" collides
  # between two nodes in the same country, which previously made a second install
  # either hit A113 ("tags must be unique") or — worse — adopt/patch ANOTHER node's
  # config-profile. Derive a per-node token from the node name so tags are unique.
  local tagid; tagid="$(printf '%s' "$NODE_NAME" | tr -cd 'A-Za-z0-9' | tr '[:lower:]' '[:upper:]')"
  tagid="${tagid:0:24}"; [[ -n "$tagid" ]] || tagid="${COUNTRY^^}"
  # NAMESPACE_HASH (default ON for fresh installs): names differing only in
  # punctuation/spacing ("FI-01", "FI_01", "FI 01") strip to the same alnum token and
  # would collide on globally-unique tags. Appending a short deterministic hash of the
  # RAW node name keeps them distinct. Hashing NODE_NAME (not PROFILE_NAME) makes it
  # independent of when the profile name is finalized and it recomputes identically on
  # --resume (NODE_NAME is restored from state). load_inputs forces this OFF when
  # resuming state that predates the key, so a deployed node is never re-tagged.
  if [[ "$NAMESPACE_HASH" == "1" ]]; then
    local nshash; nshash="$(printf '%s' "$NODE_NAME" | sha256sum 2>/dev/null | head -c 6 | tr '[:lower:]' '[:upper:]')"
    [[ -n "$nshash" ]] || nshash="$(printf '%s' "$NODE_NAME" | cksum | tr -cd '0-9' | head -c 6)"
    [[ -n "$nshash" ]] && tagid="${tagid}-${nshash}"
  fi
  TAG_NAMESPACE="$tagid"
  INBOUND_TAG="${tagid}-REALITY"
  INBOUND_TAG_XHTTP="${tagid}-XHTTP"
  INBOUND_TAG_GRPC="${tagid}-GRPC"
  BRIDGE_TAG="BRIDGE_${tagid}_IN"   # cascade SS inbound; recomputed if node name changes
  # Config Profiles are full Xray configs and may be combined on a node. Keep
  # outbounds namespaced too — static DIRECT/BLOCK tags otherwise collide across
  # profiles exactly like generic inbound tags do.
  OUTBOUND_TAG_DIRECT="${tagid}-DIRECT"
  OUTBOUND_TAG_BLOCK="${tagid}-BLOCK"
  if [[ "$MASK" == "grpc-tls" ]]; then
    # One VLESS+gRPC inbound on loopback; nginx fronts it with real TLS on 443.
    TRANSPORT="grpc"
    GRPC_SERVICE="${GRPC_SERVICE:-$DEFAULT_GRPC_SERVICE}"
    if [[ "$NONINTERACTIVE" != "1" ]]; then
      GRPC_SERVICE="$(read_default "gRPC serviceName (nginx routes /<name>/Tun to Xray)" "$GRPC_SERVICE")"
    fi
    [[ "$GRPC_SERVICE" =~ ^[A-Za-z0-9._/-]+$ ]] || die "Bad gRPC serviceName (letters/numbers/._/-/ only)."
    while :; do GRPC_PORT="$(read_default "Local gRPC inbound port (loopback)" "${GRPC_PORT:-$DEFAULT_GRPC_PORT}")"; { valid_port "$GRPC_PORT" && [[ "$GRPC_PORT" != "443" ]]; } && break; warn "1..65535, not 443."; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --grpc-port"; done
    ACTIVE_INBOUNDS=("${INBOUND_TAG_GRPC}:${GRPC_PORT}:grpc-tls")
  else
    # Transport (tcp default | xhttp | both). xhttp needs its own port in `both`.
    if [[ -z "$TRANSPORT" && "$NONINTERACTIVE" != "1" ]]; then
      info "Transport: tcp (Vision, all clients) | xhttp (bypass VLESS-TCP block) | both"
      TRANSPORT="$(choose_one "Transport" "tcp" tcp xhttp both)"
    fi
    TRANSPORT="${TRANSPORT:-tcp}"
    case "$TRANSPORT" in tcp|xhttp|both) : ;; *) die "Bad --transport '$TRANSPORT' (tcp|xhttp|both)." ;; esac
    if [[ "$TRANSPORT" == "both" ]]; then
      while :; do
        XHTTP_PORT="$(read_default "XHTTP inbound port" "${XHTTP_PORT:-8444}")"
        if ! valid_port "$XHTTP_PORT"; then
          warn "XHTTP port must be in the range 1..65535."
        elif xhttp_port_reserved "$XHTTP_PORT"; then
          warn "XHTTP port $XHTTP_PORT is reserved by this node (80, 443, SSH, node API, selfsteal, renewal, or Beszel)."
        else
          break
        fi
        [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --xhttp-port '$XHTTP_PORT'."
      done
      ACTIVE_INBOUNDS=("${INBOUND_TAG}:443:raw" "${INBOUND_TAG_XHTTP}:${XHTTP_PORT}:xhttp")
      # XHTTP inbound port must be reachable through the firewall. On a resume
      # it is already in saved TCP_PORTS, so use an explicit conditional: a bare
      # false `[[ … ]]` would become this function's return status and trip set -e.
      if [[ ",$TCP_PORTS," != *",$XHTTP_PORT,"* ]]; then
        TCP_PORTS="$TCP_PORTS,$XHTTP_PORT"
      fi
    elif [[ "$TRANSPORT" == "xhttp" ]]; then
      ACTIVE_INBOUNDS=("${INBOUND_TAG_XHTTP}:443:xhttp")
    else
      ACTIVE_INBOUNDS=("${INBOUND_TAG}:443:raw")
    fi
  fi
}
# Interactive Internal Squad picker: list existing squads (numbered), plus "create
# new" and "skip". Best-effort — needs the token + a reachable panel; on any error
# it silently leaves SQUAD_* unset and setup_squad prints the manual step later.
pick_squad() {
  [[ "$NONINTERACTIVE" == "1" || "$DRY_RUN" == "1" ]] && return 0
  [[ -n "$SQUAD_UUID" || -n "$SQUAD_NAME" ]] && return 0   # already provided via flags
  command -v jq >/dev/null 2>&1 || return 0
  local squads; squads="$(panel_req GET /api/internal-squads 2>/dev/null || echo '')"
  [[ -n "$squads" ]] || { info "Internal Squad: could not list now (token scope / panel) — you can enable it later."; return 0; }
  local names uuids
  mapfile -t names < <(printf '%s' "$squads" | jq -r '(.response.internalSquads? // .response // [])[]?.name' 2>/dev/null)
  mapfile -t uuids < <(printf '%s' "$squads" | jq -r '(.response.internalSquads? // .response // [])[]?.uuid' 2>/dev/null)
  info "Internal Squad — enable this inbound so users receive it:"
  local i
  for i in "${!names[@]}"; do log "    $((i+1))) ${names[$i]}"; done
  local newn=$(( ${#names[@]} + 1 )) skipn=$(( ${#names[@]} + 2 ))
  log "    ${newn}) + create a new squad"
  log "    ${skipn}) skip (enable manually later)"
  local ans; ans="$(read_default "Squad [number]" "$skipn")"
  if [[ "$ans" =~ ^[0-9]+$ ]]; then
    if (( ans >= 1 && ans <= ${#names[@]} )); then
      SQUAD_UUID="${uuids[$((ans-1))]}"; SQUAD_NAME="${names[$((ans-1))]}"; SQUAD_CREATE=0
      info "Squad: ${SQUAD_NAME}"
    elif (( ans == newn )); then
      SQUAD_NAME="$(read_default "New squad name" "${COUNTRY^^}-users")"; SQUAD_UUID=""; SQUAD_CREATE=1
    fi
  fi
}
collect_inputs() {
  load_inputs         # --resume: reload ALL saved operator inputs (token incl.) first
  load_resume_state   # --resume: reload saved identity before defaults are generated
  # Domain
  while :; do DOMAIN="$(read_default "Selfsteal domain" "$DOMAIN")"; valid_domain "$DOMAIN" && break; warn "Invalid domain."; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --domain"; done
  # Cascade back-end: if this node sits behind an SNI-mirror front (a separate box
  # that L4-forwards :443 here by SNI), lock :443 to the front's egress IP so this
  # node's own IP cannot be probed on 443 directly. Asked early; --front-ip skips it.
  if [[ -z "${CLI_SET[FRONT_IP]:-}" && "$NONINTERACTIVE" != "1" ]]; then
    if [[ -n "$FRONT_IP" ]] || yes_no "Is this node behind an SNI-mirror front (cascade — lock :443 to the front)?" "n"; then
      while :; do
        FRONT_IP="$(read_default "Front (mirror) egress IP(s) allowed on :443, comma-sep; empty = keep 443 open" "$FRONT_IP")"
        FRONT_IP="${FRONT_IP//[[:space:]]/}"
        [[ -z "$FRONT_IP" ]] && break
        valid_whitelist "$FRONT_IP" && break
        warn "Invalid. Example: 203.0.113.5 or 203.0.113.5,2001:db8::1"
      done
    fi
  fi
  [[ -n "$FRONT_IP" ]] && { valid_whitelist "$FRONT_IP" || die "Bad --front-ip: $FRONT_IP"; }
  # Panel. Accept a bare host (p.example.com) and default the scheme to https://.
  while :; do
    PANEL_URL="$(read_default "Panel URL" "$PANEL_URL")"
    [[ -n "$PANEL_URL" && "$PANEL_URL" != http://* && "$PANEL_URL" != https://* ]] && PANEL_URL="https://$PANEL_URL"
    valid_url "$PANEL_URL" && break
    warn "Invalid URL."; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --panel-url"
  done
  resolve_panel_token
  # Whitelist (only needed when the firewall step runs)
  if [[ "$SKIP_FIREWALL" != "1" ]]; then
    while :; do PANEL_WHITELIST="$(read_default "Panel whitelist IP/CIDR (comma-sep)" "$PANEL_WHITELIST")"; PANEL_WHITELIST="${PANEL_WHITELIST//[[:space:]]/}"; valid_whitelist "$PANEL_WHITELIST" && break; warn "Invalid. Example: 1.2.3.4 or 1.2.3.4,5.6.7.0/24"; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --whitelist"; done
  fi
  # Cert mode
  CERT_MODE="${CERT_MODE:-$(read_default "Cert mode (le443|cf-dns)" "$DEFAULT_CERT_MODE")}"
  [[ "$CERT_MODE" == "le443" || "$CERT_MODE" == "cf-dns" ]] || die "cert-mode must be le443 or cf-dns."
  if [[ "$CERT_MODE" == "cf-dns" && -z "$CF_TOKEN" ]]; then
    [[ "$NONINTERACTIVE" == "1" ]] && die "--cf-token required for cf-dns"
    read -r -s -p "Cloudflare API token: " CF_TOKEN; echo
    [[ -n "$CF_TOKEN" ]] || die "CF token required for cf-dns."
  fi
  # ACME email
  while :; do ACME_EMAIL="$(read_default "ACME email (Let's Encrypt account)" "$ACME_EMAIL")"; valid_email "$ACME_EMAIL" && break; warn "Invalid email."; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --acme-email"; done
  # Country
  while :; do COUNTRY="$(read_default "Country code (ISO-2)" "${COUNTRY:-$DEFAULT_COUNTRY}")"; valid_cc "$COUNTRY" && break; warn "Two letters."; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --country"; done
  # Ports
  while :; do NODE_PORT="$(read_default "NODE_PORT" "${NODE_PORT:-$DEFAULT_NODE_PORT}")"; valid_port "$NODE_PORT" && break; warn "1..65535"; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --node-port"; done
  while :; do SELFSTEAL_PORT="$(read_default "Selfsteal port" "${SELFSTEAL_PORT:-$DEFAULT_SELFSTEAL_PORT}")"; valid_port "$SELFSTEAL_PORT" && break; warn "1..65535"; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --selfsteal-port"; done
  while :; do RENEW_PORT="$(read_default "Renewal port" "${RENEW_PORT:-$DEFAULT_RENEW_PORT}")"; valid_port "$RENEW_PORT" && break; warn "1..65535"; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --renew-port"; done
  SSH_PORT="$(read_default "SSH port" "${SSH_PORT:-$(detect_ssh_port)}")"; valid_port "$SSH_PORT" || die "Bad SSH port."
  # Masking model. reality: Xray owns 443 (XTLS-Reality). grpc-tls: nginx owns 443
  # with a real certificate and proxies VLESS+gRPC to a loopback Xray inbound.
  if [[ -z "$MASK" && "$NONINTERACTIVE" != "1" ]]; then
    info "Masking: reality (XTLS-Reality on 443) | grpc-tls (nginx real-cert TLS on 443, gRPC behind it — CDN-friendly)"
    MASK="$(choose_one "Masking model" "$DEFAULT_MASK" reality grpc-tls)"
  fi
  MASK="${MASK:-$DEFAULT_MASK}"
  case "$MASK" in reality|grpc-tls) : ;; *) die "Bad --mask '$MASK' (reality|grpc-tls)." ;; esac
  # grpc-tls rides a real browser-like TLS+h2 handshake; default fingerprint chrome.
  [[ "$MASK" == "grpc-tls" ]] && FP="${FP:-chrome}" || FP="${FP:-$DEFAULT_FP}"
  [[ "$FP" == "randomized" ]] && \
    warn "fingerprint=randomized may break some Xray clients (macOS Xray fails with 'tls: CurvePreferences includes unsupported curve'). Prefer firefox or chrome."
  TEMPLATE="${TEMPLATE:-$DEFAULT_TEMPLATE}"
  resolve_template "$TEMPLATE" >/dev/null || die "Bad --template '$TEMPLATE'. Valid: 1-${#TEMPLATE_FOLDERS[@]} or ${TEMPLATE_FOLDERS[*]}"
  MODE="${MODE:-$DEFAULT_MODE}"
  [[ "$MODE" == "socket" || "$MODE" == "tcp" ]] || die "Bad mode '$MODE' (use --socket or --tcp)."
  TCP_PORTS="${TCP_PORTS:-$DEFAULT_TCP_PORTS}"; valid_port_list "$TCP_PORTS" || die "Bad --tcp-ports."
  UDP_PORTS="${UDP_PORTS:-$DEFAULT_UDP_PORTS}"; valid_port_list "$UDP_PORTS" || die "Bad --udp-ports."
  # LE443 renewal validates on 443 → redirected to RENEW_PORT, so the firewall must
  # allow it. Add it here (not later in na_prepare) so print_plan shows the real list.
  if [[ "$CERT_MODE" == "le443" && ",$TCP_PORTS," != *",$RENEW_PORT,"* ]]; then
    TCP_PORTS="$TCP_PORTS,$RENEW_PORT"
  fi
  NA_REF="${NA_REF:-$DEFAULT_NA_REF}"
  [[ "$NA_REF" =~ ^[A-Za-z0-9._/-]+$ && "$NA_REF" != *..* ]] || die "Bad --na-ref."

  # Node name (prompted; suggest <CC>-<seq>). Dry-run stays fully local — it does
  # not query the panel for the next sequence, just uses 01.
  if [[ -z "$NODE_NAME" ]]; then
    local seq="01"
    [[ "$DRY_RUN" == "1" ]] || seq="$(panel_next_sequence "$COUNTRY" 2>/dev/null || echo '01')"
    NODE_NAME="$(read_default "Node name (as shown in panel)" "${COUNTRY^^}-${seq}")"
  fi
  [[ -n "$NODE_NAME" ]] || die "Node name required."
  # Host remark (prompted; label shown in the subscription)
  if [[ -z "$HOST_REMARK" ]]; then
    HOST_REMARK="$(read_default "Host label (shown in subscription)" "${NODE_NAME} REALITY")"
  fi
  [[ -n "$HOST_REMARK" ]] || die "Host remark required."
  compute_inbounds
  pick_squad
  # Config-profile name (prompted). Panel restricts to letters/numbers/_/-/space,
  # so the suggested default is a sanitized copy of the node name.
  if [[ -z "$PROFILE_NAME" ]]; then
    local default_profile; default_profile="$(sanitize_profile_name "$NODE_NAME")"
    [[ -n "$default_profile" ]] || default_profile="node-${COUNTRY,,}"
    while :; do
      PROFILE_NAME="$(read_default "Config-profile name (letters/numbers/_/-/space only)" "$default_profile")"
      valid_profile_name "$PROFILE_NAME" && break
      warn "Only letters, numbers, underscore, dash and space are allowed (no brackets/emoji)."
      [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --profile-name"
    done
  else
    valid_profile_name "$PROFILE_NAME" || die "Bad --profile-name: only letters/numbers/_/-/space allowed."
  fi
  # Respect an explicit --node-public-ip (or a value restored on --resume); only
  # auto-detect when unset. Auto-detection rejects a NAT'd private route source.
  [[ -n "$NODE_PUBLIC_IP" ]] || NODE_PUBLIC_IP="$(detect_public_ip)"
  if [[ -z "$NODE_PUBLIC_IP" ]]; then
    # Dry-run is a plan/CI sanity check — a placeholder keeps it going without network.
    [[ "$DRY_RUN" == "1" ]] && NODE_PUBLIC_IP="203.0.113.1" \
      || die "Could not detect a public IP (private/NAT source?); pass --node-public-ip <ip>."
  fi
  valid_ip "$NODE_PUBLIC_IP" || die "NODE_PUBLIC_IP '$NODE_PUBLIC_IP' is not a valid IP address."

  # Host connect address. grpc-tls advertises the DOMAIN (real TLS/SNI on nginx :443,
  # CDN-frontable). reality keeps the public IP by default (back-compat: existing
  # hosts match by remark+address; override with --host-address).
  if [[ -z "$HOST_ADDRESS" ]]; then
    [[ "$MASK" == "grpc-tls" ]] && HOST_ADDRESS="$DOMAIN" || HOST_ADDRESS="$NODE_PUBLIC_IP"
  fi
  valid_domain "$HOST_ADDRESS" || valid_ip "$HOST_ADDRESS" \
    || die "Bad --host-address '$HOST_ADDRESS' (domain, IPv4 or IPv6 address)."

  collect_bridge_inputs
  check_port_conflicts
}

# Cascade bridge questions. This node is the EXIT: it grows a Shadowsocks inbound
# that an entry node feeds over the shared SS secret. Asked last so NODE_NAME (tag)
# and the squad are already settled. Non-interactive requires the entry IP + user.
collect_bridge_inputs() {
  if [[ "$BRIDGE" != "1" && "$NONINTERACTIVE" != "1" ]]; then
    yes_no "Will this node be a cascade bridge (exit node accepting traffic from an entry node)?" "n" && BRIDGE=1
  fi
  [[ "$BRIDGE" == "1" ]] || return 0

  # Entry node IP — allowed to reach the SS port; everything else is firewalled off.
  while :; do
    BRIDGE_ENTRY_IP="$(read_default "Entry node IP (sends traffic to this bridge)" "$BRIDGE_ENTRY_IP")"
    BRIDGE_ENTRY_IP="${BRIDGE_ENTRY_IP//[[:space:]]/}"
    valid_ip "$BRIDGE_ENTRY_IP" && break
    warn "Invalid IP. Example: 203.0.113.10"; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --bridge-entry-ip"
  done
  # Whitelist the entry IP so na_filter lets it reach the SS port. The port itself
  # is deliberately NOT added to TCP_PORTS (that would open it to the whole internet).
  if [[ "$SKIP_FIREWALL" != "1" && ",$PANEL_WHITELIST," != *",$BRIDGE_ENTRY_IP,"* ]]; then
    PANEL_WHITELIST="${PANEL_WHITELIST:+$PANEL_WHITELIST,}$BRIDGE_ENTRY_IP"
    info "Added entry IP $BRIDGE_ENTRY_IP to the firewall whitelist."
  fi

  while :; do BRIDGE_SS_PORT="$(read_default "SS bridge port" "$BRIDGE_SS_PORT")"; { valid_port "$BRIDGE_SS_PORT" && [[ "$BRIDGE_SS_PORT" != "443" ]]; } && break; warn "1..65535, not 443."; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --bridge-ss-port"; done

  # Panel user whose ssPassword becomes the shared bridge secret.
  while :; do
    BRIDGE_USER="$(read_default "Panel username to create/reuse for the bridge secret" "$BRIDGE_USER")"
    valid_username "$BRIDGE_USER" && break
    warn "3-36 chars, letters/numbers/_/- only."; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --bridge-user"
  done

  # Entry node's own selfsteal domain — only used to label the printed entry config.
  while :; do
    ENTRY_DOMAIN="$(read_default "Entry node selfsteal domain (for its printed config)" "$ENTRY_DOMAIN")"
    valid_domain "$ENTRY_DOMAIN" && break
    warn "Invalid domain."; [[ "$NONINTERACTIVE" == "1" ]] && die "Bad --entry-domain"
  done
  # BRIDGE_TAG is derived in compute_inbounds (already run) so it tracks node-name edits.
}
check_port_conflicts() {
  local -A owner=(); local pair name p
  local -a portchecks=("443=nginx/Xray/ACME" "$NODE_PORT=NODE_PORT" "$SSH_PORT=SSH" "$RENEW_PORT=renewal")
  if [[ "$MASK" == "grpc-tls" ]]; then portchecks+=("$GRPC_PORT=grpc-inbound"); else portchecks+=("$SELFSTEAL_PORT=selfsteal"); fi
  [[ "$TRANSPORT" == "both" && -n "$XHTTP_PORT" ]] && portchecks+=("$XHTTP_PORT=xhttp-inbound")
  [[ "$BRIDGE" == "1" ]] && portchecks+=("$BRIDGE_SS_PORT=ss-bridge")
  for pair in "${portchecks[@]}"; do
    p="${pair%%=*}"; name="${pair#*=}"
    [[ -n "${owner[$p]:-}" ]] && die "Port conflict: $name and ${owner[$p]} both use $p."
    owner[$p]="$name"
  done
}

print_plan() {
  log
  log "${BOLD}Plan — installer $INSTALLER_VERSION${RESET}"
  log "  Domain:            $DOMAIN"
  log "  Public IP:         $NODE_PUBLIC_IP"
  log "  Panel:             $PANEL_URL"
  log "  Node name:         $NODE_NAME  (country ${COUNTRY^^})"
  log "  Host label:        $HOST_REMARK"
  log "  Host address:      $HOST_ADDRESS"
  log "  Internal Squad:    $( [[ -n "$SQUAD_UUID" ]] && echo "$SQUAD_UUID" || { [[ -n "$SQUAD_NAME" ]] && echo "$SQUAD_NAME" || echo "(none — enable inbound in a squad manually)"; } )"
  log "  Config-profile:    $PROFILE_NAME"
  log "  Node/SSH port:     $NODE_PORT / $SSH_PORT"
  log "  Masking model:     $MASK"
  if [[ "$MASK" == "grpc-tls" ]]; then
    log "  gRPC front:        nginx :443 real-cert TLS/h2 → 127.0.0.1:$GRPC_PORT  (service $GRPC_SERVICE)"
    log "  Transport:         VLESS+gRPC behind nginx TLS (client security=TLS)"
  else
    if [[ "$MODE" == "tcp" ]]; then
      log "  Selfsteal target:  127.0.0.1:$SELFSTEAL_PORT  (TCP, Reality dest, xver 1)"
    else
      log "  Selfsteal target:  $SOCKET_PATH  (unix socket, Reality dest, xver 1)"
    fi
    log "  Transport:         $TRANSPORT$( [[ "$TRANSPORT" == both ]] && echo " (tcp:443 + xhttp:$XHTTP_PORT)" )"
  fi
  log "  Decoy:             $( [[ "$TEMPLATE" == builtin ]] && echo "builtin generator (no external fetch)" || echo "$TEMPLATE (sni-templates, randomize=$( [[ "$RANDOMIZE" == 1 ]] && echo yes || echo no ))" )"
  log "  Geo lists:         $( [[ "$GEO" == 1 ]] && echo "runetfreedom (mounted + daily update)" || echo "image default" )"
  log "  Cert mode:         $CERT_MODE${CERT_MODE:+ }$( [[ "$CERT_MODE" == le443 ]] && echo "(renew port $RENEW_PORT)" || echo "(wildcard *.$DOMAIN)" )"
  log "  Firewall:          $( [[ "$SKIP_FIREWALL" == 1 ]] && echo skipped || echo "node-accelerator ($NA_REF), TCP $TCP_PORTS / UDP $UDP_PORTS" )"
  log "  OS update:         $( [[ "$SKIP_UPDATE" == 1 ]] && echo skipped || echo "full-upgrade + automatic security updates" )"
  log "  RKN hardening:     $( [[ "$HARDENING" == 1 ]] && echo "tcp_rfc1337 + TTL=128 + drop unused protos" || echo "skipped" )"
  log "  Panel whitelist:   $PANEL_WHITELIST"
  if [[ "$BRIDGE" == "1" ]]; then
    log "  Cascade bridge:    EXIT node — SS inbound $BRIDGE_METHOD :$BRIDGE_SS_PORT (tag $BRIDGE_TAG)"
    log "  Bridge entry IP:   $BRIDGE_ENTRY_IP  (whitelisted; port not opened to internet)"
    log "  Bridge user:       $BRIDGE_USER  (its ssPassword = shared secret)"
    log "  Entry config:      printed at the end (entry domain $ENTRY_DOMAIN → split-tunnel → this bridge)"
  fi
  log
}

# Interactive review: reprint the plan with numbered editable fields and let the
# user fix any of them before proceeding. Enter = accept. Skipped in dry-run and
# non-interactive runs (there the plan is just informational).
edit_plan() {
  [[ "$NONINTERACTIVE" == "1" || "$DRY_RUN" == "1" ]] && return 0
  local choice squad_label
  while :; do
    print_plan
    squad_label="$( [[ -n "$SQUAD_UUID" ]] && echo "$SQUAD_NAME ($SQUAD_UUID)" || { [[ -n "$SQUAD_NAME" ]] && echo "$SQUAD_NAME$( [[ "$SQUAD_CREATE" == 1 ]] && echo ' (new)')" || echo "(none)"; } )"
    log "${BOLD}Edit a field before install:${RESET}"
    log "     1) Domain            = $DOMAIN"
    log "     2) Panel URL         = $PANEL_URL"
    log "     3) Country           = ${COUNTRY^^}"
    log "     4) Node name         = $NODE_NAME"
    log "     5) Host label        = $HOST_REMARK"
    log "     6) Host address      = $HOST_ADDRESS"
    log "     7) Config-profile    = $PROFILE_NAME"
    log "     8) NODE_PORT         = $NODE_PORT"
    log "     9) SSH port          = $SSH_PORT"
    log "    10) Masking model     = $MASK"
    [[ "$MASK" != "grpc-tls" ]] && log "    11) Transport         = $TRANSPORT"
    log "    12) Cert mode         = $CERT_MODE"
    log "    13) Internal Squad    = $squad_label"
    [[ "$SKIP_FIREWALL" != "1" ]] && log "    14) Panel whitelist   = $PANEL_WHITELIST"
    [[ "$BRIDGE" == "1" ]] && log "    15) Cascade bridge    = entry $BRIDGE_ENTRY_IP → SS :$BRIDGE_SS_PORT, user '$BRIDGE_USER', entry-domain $ENTRY_DOMAIN"
    choice="$(read_default "Number to edit (Enter = proceed)" "")"
    [[ -z "$choice" ]] && break
    case "$choice" in
      1) while :; do DOMAIN="$(read_default "Selfsteal domain" "$DOMAIN")"; valid_domain "$DOMAIN" && break; warn "Invalid domain."; done ;;
      2) while :; do PANEL_URL="$(read_default "Panel URL" "$PANEL_URL")"; [[ "$PANEL_URL" != http://* && "$PANEL_URL" != https://* ]] && PANEL_URL="https://$PANEL_URL"; valid_url "$PANEL_URL" && break; warn "Invalid URL."; done ;;
      3) while :; do COUNTRY="$(read_default "Country code (ISO-2)" "$COUNTRY")"; valid_cc "$COUNTRY" && break; warn "Two letters."; done; compute_inbounds ;;
      4) while :; do NODE_NAME="$(read_default "Node name" "$NODE_NAME")"; [[ -n "$NODE_NAME" ]] && break; warn "Required."; done; compute_inbounds ;;
      5) while :; do HOST_REMARK="$(read_default "Host label" "$HOST_REMARK")"; [[ -n "$HOST_REMARK" ]] && break; warn "Required."; done ;;
      6) while :; do HOST_ADDRESS="$(read_default "Host address (domain/IPv4/IPv6)" "$HOST_ADDRESS")"; { valid_domain "$HOST_ADDRESS" || valid_ip "$HOST_ADDRESS"; } && break; warn "domain, IPv4 or IPv6."; done ;;
      7) while :; do PROFILE_NAME="$(read_default "Config-profile name" "$PROFILE_NAME")"; valid_profile_name "$PROFILE_NAME" && break; warn "letters/numbers/_/-/space only."; done ;;
      8) while :; do NODE_PORT="$(read_default "NODE_PORT" "$NODE_PORT")"; valid_port "$NODE_PORT" && break; warn "1..65535"; done; compute_inbounds ;;
      9) while :; do SSH_PORT="$(read_default "SSH port" "$SSH_PORT")"; valid_port "$SSH_PORT" && break; warn "1..65535"; done; compute_inbounds ;;
      10) MASK="$(choose_one "Masking model" "$MASK" reality grpc-tls)"
          # Re-derive the default fingerprint for the chosen mask so switching
          # grpc-tls -> reality doesn't leave the chrome FP stuck.
          [[ "$MASK" == "grpc-tls" ]] && FP="chrome" || FP="$DEFAULT_FP"
          compute_inbounds ;;
      11) if [[ "$MASK" == "grpc-tls" ]]; then warn "Transport is fixed to gRPC in grpc-tls mode."; else TRANSPORT="$(choose_one "Transport" "$TRANSPORT" tcp xhttp both)"; compute_inbounds; fi ;;
      12) CERT_MODE="$(choose_one "Cert mode" "$CERT_MODE" le443 cf-dns)"
          if [[ "$CERT_MODE" == "cf-dns" && -z "$CF_TOKEN" ]]; then read -r -s -p "Cloudflare API token: " CF_TOKEN; echo; fi ;;
      13) SQUAD_UUID=""; SQUAD_NAME=""; SQUAD_CREATE=0; pick_squad ;;
      14) if [[ "$SKIP_FIREWALL" != "1" ]]; then while :; do PANEL_WHITELIST="$(read_default "Panel whitelist IP/CIDR (comma-sep)" "$PANEL_WHITELIST")"; PANEL_WHITELIST="${PANEL_WHITELIST//[[:space:]]/}"; valid_whitelist "$PANEL_WHITELIST" && break; warn "Invalid."; done; fi ;;
      15) if [[ "$BRIDGE" == "1" ]]; then collect_bridge_inputs; check_port_conflicts; else warn "Bridge mode is off (re-run with --bridge to enable)."; fi ;;
      *) warn "Enter a listed number, or press Enter to proceed." ;;
    esac
  done
  check_port_conflicts   # re-validate after any port/mask change
}

# Read-only preflight: verify the host and panel are ready without mutating either.
# Reuses collected inputs; never prints secrets. Returns non-zero on a blocker.
run_preflight_checks() {
  step "Preflight checks (read-only — nothing is changed)"
  local fail=0
  ok "OS: $( ( . /etc/os-release 2>/dev/null; printf '%s' "${PRETTY_NAME:-unknown}" ) )"
  # Real if-then-else (not A && B || C): the pass/fail decision must not hinge on
  # ok()'s exit status (SC2015).
  if [[ -n "$NODE_PUBLIC_IP" ]]; then ok "Route source / public IP: $NODE_PUBLIC_IP"
  else warn "Could not detect a public IP."; fail=1; fi
  # DNS: selfsteal domain must resolve to this host.
  local dip; dip="$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)"
  if [[ -n "$dip" ]]; then
    [[ "$dip" == "$NODE_PUBLIC_IP" ]] && ok "DNS $DOMAIN → $dip (matches this host)" \
      || { warn "DNS $DOMAIN → $dip does NOT match this host ($NODE_PUBLIC_IP)."; fail=1; }
  else
    warn "DNS: $DOMAIN does not resolve."; fail=1
  fi
  # DNS: panel host.
  local phost="${PANEL_URL#*://}"; phost="${phost%%/*}"; phost="${phost%%:*}"
  if valid_ip "$phost"; then ok "Panel host is a literal IP: $phost"
  elif getent hosts "$phost" >/dev/null 2>&1; then ok "DNS panel host $phost resolves"
  else warn "DNS: panel host $phost does not resolve."; fi
  # Packages.
  local c miss=""
  for c in curl jq openssl docker nft timeout; do command -v "$c" >/dev/null 2>&1 || miss+=" $c"; done
  [[ -z "$miss" ]] && ok "Required commands present." || warn "Missing (installer adds most):$miss"
  # Occupied ports.
  if command -v ss >/dev/null 2>&1; then
    local p
    for p in 443 "$NODE_PORT" "$RENEW_PORT"; do
      port_listening "$p" && { warn "port $p already in use"; [[ "$p" == "443" ]] && fail=1; } || true
    done
    port_listening "$SSH_PORT" && ok "SSH port $SSH_PORT in use (expected)" || warn "SSH port $SSH_PORT not detected as listening"
  fi
  # Existing containers / dirs (installer reuses them; just report).
  if command -v docker >/dev/null 2>&1; then
    local n
    for n in "$NODE_CONTAINER" "$NGINX_CONTAINER"; do
      docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$n" && warn "container '$n' already exists (will be reused)" || true
    done
  fi
  local d
  for d in "$NODE_DIR" "$NGINX_DIR" "$STATE_DIR" "$ACME_HOME"; do
    [[ -e "$d" ]] && warn "exists: $d (install reuses/updates it)" || true
  done
  # Panel API scope (read-only GET).
  if panel_req GET /api/nodes >/dev/null 2>&1; then ok "Panel API reachable; token can list nodes."
  else warn "Panel API check failed — verify --panel-url and token scope."; fail=1; fi
  log
  if [[ "$fail" == "0" ]]; then ok "Preflight passed — no blocking issues."; return 0
  else warn "Preflight found blocking issues (above). Resolve them before installing."; return 1; fi
}

# ── Cascade: entry-node config generator ────────────────────────────────────
# The entry node is the OTHER machine (not managed by this installer). We mint a
# fresh Reality keypair for it and print a ready split-tunnel Xray config whose
# catch-all + AI traffic is relayed over Shadowsocks to THIS node (the exit),
# while RU/CDN/dev traffic exits directly. Written to $STATE_DIR/entry-node.json
# (chmod 600 — it holds the entry Reality private key + the shared SS password)
# and echoed. The operator pastes it into the entry node's config-profile.
generate_entry_reality_keys() {
  local out priv pub
  out="$("$XRAY_CORE_BIN" x25519 2>/dev/null)" \
    || die "Failed to run 'xray x25519' for the entry keypair."
  priv="$(printf '%s' "$out" | grep -iE 'private'         | awk '{print $NF}' | head -1 || true)"
  pub="$(printf '%s' "$out" | grep -iE 'public|password' | awk '{print $NF}' | head -1 || true)"
  [[ -n "$priv" && -n "$pub" ]] || die "Could not parse xray x25519 output for the entry keypair."
  ENTRY_REALITY_PRIVATE="$priv"; ENTRY_REALITY_PUBLIC="$pub"
  ENTRY_REALITY_SHORT_ID="$(openssl rand -hex 8)"
}
build_entry_config() {
  # Entry inbound tag, mirroring compute_inbounds' scheme but for the entry node.
  local entry_ns="${TAG_NAMESPACE:-ENTRY}" etag direct block ss_exit
  etag="ENTRY-${entry_ns}-REALITY"
  direct="ENTRY-${entry_ns}-DIRECT"
  block="ENTRY-${entry_ns}-BLOCK"
  ss_exit="ENTRY-${entry_ns}-SS-EXIT"
  jq -n \
    --arg itag "$etag" \
    --arg direct "$direct" \
    --arg block "$block" \
    --arg ss_exit "$ss_exit" \
    --arg sni "$ENTRY_DOMAIN" \
    --arg priv "$ENTRY_REALITY_PRIVATE" \
    --arg sid "$ENTRY_REALITY_SHORT_ID" \
    --arg ssaddr "$NODE_PUBLIC_IP" \
    --argjson ssport "$BRIDGE_SS_PORT" \
    --arg ssmethod "$BRIDGE_METHOD" \
    --arg sspw "$BRIDGE_SS_PASSWORD" '
  {
    log: { loglevel: "warning" },
    dns: {
      hosts: { "dns.google": ["8.8.8.8","8.8.4.4"], "cloudflare-dns.com": ["1.1.1.1","1.0.0.1"] },
      servers: [ "77.88.8.8", "1.1.1.1",
        { address: "https://cloudflare-dns.com/dns-query", timeoutMs: 5000 },
        { address: "https://dns.google/dns-query", timeoutMs: 5000 } ],
      serveStale: true, queryStrategy: "UseIPv4", serveExpiredTTL: 43200
    },
    inbounds: [ {
      tag: $itag, port: 443, listen: "0.0.0.0", protocol: "vless",
      settings: { clients: [], decryption: "none" },
      sniffing: { enabled: true, routeOnly: true, destOverride: ["http","tls","quic"] },
      streamSettings: {
        network: "raw", sockopt: { tcpNoDelay: true, tcpFastOpen: true },
        security: "reality",
        realitySettings: { show:false, xver:1, target:"/dev/shm/nginx.sock",
          shortIds:[$sid], privateKey:$priv, serverNames:[$sni] }
      }
    } ],
    outbounds: [
      { tag: $direct, protocol: "freedom", settings: { domainStrategy: "UseIPv4" },
        streamSettings: { sockopt: { tcpNoDelay: true, tcpFastOpen: true } } },
      { tag: $block, protocol: "blackhole" },
      { tag: $ss_exit, protocol: "shadowsocks",
        settings: { servers: [ { address:$ssaddr, port:$ssport, level:0, method:$ssmethod, password:$sspw } ] },
        streamSettings: { sockopt: { tcpNoDelay: true, tcpFastOpen: true, tcpKeepAliveIdle: 30 } } }
    ],
    routing: {
      domainMatcher: "hybrid", domainStrategy: "IPIfNonMatch",
      rules: [
        { type: "field", ip: ["geoip:private"], outboundTag: $block },
        { type: "field", domain: ["geosite:private","geosite:category-ads-all"], outboundTag: $block },
        { type: "field", protocol: ["bittorrent"], outboundTag: $block },
        # Drop all QUIC (HTTP/3 over UDP:443) before the RU/foreign split so every
        # site falls back to TLS-over-TCP — the transport Reality/Vision + the SS
        # exit hop carry best. Replaces the old youtube-only udp:443 block.
        { type: "field", network: "udp", port: 443, outboundTag: $block },
        { type: "field",
          domain: ["domain:gemini.google.com","domain:aistudio.google.com","domain:generativelanguage.googleapis.com",
                   "domain:chatgpt.com","domain:openai.com","domain:oaistatic.com","domain:oaiusercontent.com",
                   "domain:anthropic.com","domain:claude.ai"],
          outboundTag: $ss_exit },
        { type: "field",
          domain: ["geosite:youtube","geosite:category-ru","regexp:^.*\\.ru$","regexp:^.*\\.xn--p1ai$",
                   "regexp:^.*\\.by$","domain:google.ru"],
          outboundTag: $direct },
        { type: "field", ip: ["geoip:ru"], outboundTag: $direct },
        { type: "field",
          domain: ["geosite:microsoft","geosite:steam","domain:github.com","domain:githubusercontent.com",
                   "domain:apple.com","domain:icloud.com","domain:npmjs.org","domain:npmjs.com","domain:yarnpkg.com",
                   "domain:pypi.org","domain:pythonhosted.org","domain:docker.io","domain:docker.com","domain:ghcr.io",
                   "domain:cdnjs.cloudflare.com","domain:unpkg.com","domain:gstatic.com","domain:ajax.googleapis.com",
                   "domain:fonts.googleapis.com"],
          outboundTag: $direct },
        { type: "field", inboundTag: [$itag], outboundTag: $ss_exit }
      ]
    },
    policy: { levels: { "0": { connIdle: 300, handshake: 2, uplinkOnly: 0, downlinkOnly: 0 } } }
  }'
}
print_entry_config() {
  [[ "$BRIDGE" == "1" ]] || return 0
  [[ "$DRY_RUN" == "1" ]] && { info "DRY-RUN: would mint an entry Reality keypair and print the entry-node config."; return 0; }
  step "Entry-node config (paste into the entry node's config-profile)"
  generate_entry_reality_keys
  local cfg out="$STATE_DIR/entry-node.json"
  cfg="$(build_entry_config)"
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  printf '%s\n' "$cfg" > "$out" 2>/dev/null && chmod 600 "$out" 2>/dev/null \
    && ok "Saved entry-node config → $out (chmod 600; holds the entry private key + SS secret)."
  log
  log "${BOLD}── Entry node ($ENTRY_DOMAIN → this exit) ──${RESET}"
  log "  Entry Reality public key: $ENTRY_REALITY_PUBLIC"
  log "  Entry Reality shortId:    $ENTRY_REALITY_SHORT_ID"
  log "  Relays to exit:           $NODE_PUBLIC_IP:$BRIDGE_SS_PORT ($BRIDGE_METHOD)"
  log "  Bridge secret (ssPassword of user '$BRIDGE_USER'): $BRIDGE_SS_PASSWORD"
  log
  log "${DIM}Full config below (also saved to $out):${RESET}"
  printf '%s\n' "$cfg"
  log
}
main() {
  # Snapshot the built-in defaults BEFORE parse_args so load_inputs (--resume) can
  # tell a real CLI/env override (value changed from its default this run) from an
  # untouched default. Without this, every SAVED_KEY that has a non-empty default
  # (BRIDGE, GEO, HARDENING, SKIP_*, RANDOMIZE, XHTTP_PATH, bridge params…) would
  # look "provided" and clobber the saved state on resume.
  declare -gA DEFAULT_SNAPSHOT=()
  # CLI_SET[key]=1 for every SAVED_KEY explicitly passed this run. Lets load_inputs
  # (--resume) honour a flag that sets a value EQUAL to the built-in default (e.g.
  # --geo when GEO already defaults to 1) as a real override of saved state — the
  # snapshot-vs-default compare alone cannot see that case.
  declare -gA CLI_SET=()
  local _dk
  for _dk in $SAVED_KEYS; do DEFAULT_SNAPSHOT[$_dk]="${!_dk-}"; done
  parse_args "$@"
  # CrowdSec (opt-in via --crowdsec) has the slowest APT step in protect; the tight
  # default 180s cap is what SIGTERMs it (rc=143). When it is enabled and the operator
  # did not set --crowdsec-timeout explicitly, give it a generous cap so it installs
  # like the stock node-accelerator run. na_filter is applied and verified before
  # CrowdSec, so a longer phase is safe.
  if [[ "$SKIP_CROWDSEC" == "0" && "$CROWDSEC_TIMEOUT_SET" == "0" ]]; then
    CROWDSEC_TIMEOUT="900"
  fi
  # Dry-run / preflight are read-only sanity checks: they neither need root nor
  # write anything, and `ss` (port probing) is only used on the real install path.
  if [[ "$DRY_RUN" == "1" ]]; then
    [[ "$PREFLIGHT" == "1" ]] && warn "Preflight: checks only, nothing will be changed." \
                              || warn "Dry-run: nothing will be changed."
  else
    need_root
    need_cmd ss
  fi
  need_cmd curl; need_cmd sed; need_cmd grep; need_cmd awk

  install_base            # ensures jq/openssl/socat present early
  # On a real install jq/openssl are now present. In dry-run/preflight the base
  # step only prints, so a clean server may still lack them: warn and degrade the
  # config preview instead of dying (L2).
  if [[ "$DRY_RUN" == "1" ]]; then
    command -v jq >/dev/null 2>&1 || warn "jq not present — dry-run will skip the Xray config JSON preview."
    command -v openssl >/dev/null 2>&1 || warn "openssl not present — some previews limited."
  else
    need_cmd jq; need_cmd openssl
  fi

  collect_inputs
  # Interactive: review the plan with numbered fields and fix any before install.
  # Dry-run / non-interactive just print it.
  if [[ "$NONINTERACTIVE" == "1" || "$DRY_RUN" == "1" ]]; then print_plan; else edit_plan; fi
  [[ "$DRY_RUN" == "1" ]] || panel_check_auth

  # Preflight mode stops here after the read-only checks — server + panel untouched.
  # Drop the ERR trap first: run_preflight_checks does its own pass/fail reporting
  # and returns 1 on blockers, which would otherwise trip the trap and print a
  # spurious "Failed at line … return 1". Clearing it also silences ERR noise from
  # its read-only probes (e.g. sourcing /etc/os-release on a non-Linux host).
  if [[ "$PREFLIGHT" == "1" ]]; then trap - ERR; run_preflight_checks; return $?; fi

  yes_no "Proceed" "y" || die "Aborted."

  # Snapshot the confirmed plan NOW so a failure in any stage below can be resumed
  # with `sudo bash <script> --resume -y` — no re-typing domain/panel/token/ports.
  save_inputs

  run_stage system-update system_update   # full OS upgrade + enable automatic security updates
  run_stage docker install_docker
  run_stage "xray-core-${XRAY_CORE_VERSION}" install_pinned_xray_core

  # Fetch the node-accelerator installer up front: if it (or the network) is
  # unreachable, fail here — BEFORE any panel resource is created (L3).
  preflight_external_deps

  # Network tuning FIRST (user request): installing the XanMod kernel / BBRv3 is the
  # heaviest step and may flag a reboot, so do it before the node is provisioned —
  # any reboot then happens before there is a live node to disturb. Best-effort: a
  # slow/failed optimize (or a benign "CrowdSec bouncer not active" note when
  # --skip-crowdsec is set) must NOT abort the rest of the install.
  run_stage firewall-optimize run_firewall_optimize

  # Order matters for le443: the cert must be issued while 443 is still free,
  # i.e. before the node container (Xray) starts and binds 443. These steps are
  # not wrapped in run_stage (they set required globals / are already idempotent),
  # so set CURRENT_STAGE by hand for an accurate failure report.
  CURRENT_STAGE="write-selfsteal"; write_selfsteal
  CURRENT_STAGE="certificate";     issue_certificate
  CURRENT_STAGE="start-selfsteal"; start_selfsteal
  # grpc-tls: nginx owns :443, so start_selfsteal restored the container ACME may
  # have stopped — the recovery guard is no longer needed.
  [[ "$CERT_STOPPED_CONTAINER" == "$NGINX_CONTAINER" ]] && CERT_STOPPED_CONTAINER="" || true

  CURRENT_STAGE="panel-resources"; setup_panel_resources   # Reality keys, NODE_SECRET_KEY + panel UUIDs
  CURRENT_STAGE="internal-squad";  setup_squad             # enable the inbound in an Internal Squad (or warn)
  CURRENT_STAGE="bridge-user";     panel_ensure_bridge_user # cascade: create/attach the SS bridge user
  # geo is best-effort and self-idempotent; NOT wrapped in run_stage so a failed/
  # skipped download is retried on the next --resume instead of staying "done".
  CURRENT_STAGE="geo";             setup_geo
  CURRENT_STAGE="write-node";      write_node
  run_stage firewall-protect run_firewall_protect   # strict allowlist BEFORE the node binds NODE_PORT
  CURRENT_STAGE="start-node";      start_node
  # reality: Xray (node) owns :443 — now that it is back up, clear the guard.
  [[ "$CERT_STOPPED_CONTAINER" == "$NODE_CONTAINER" ]] && CERT_STOPPED_CONTAINER="" || true

  run_stage cli install_cli
  run_stage maintenance setup_maintenance
  run_stage rkn-hardening apply_rkn_hardening
  CURRENT_STAGE="verify"; [[ "$DRY_RUN" == "1" ]] || verify
  # Lock :443 to the front AFTER verify probed an open :443 (cascade back-end only).
  run_stage front-gate apply_front_gate

  step "Done"
  ok "Node '$NODE_NAME' installed and registered."
  [[ "$DRY_RUN" == "1" ]] && return
  log "  Node UUID:    $NODE_UUID"
  log "  Host UUID:    $HOST_UUID"
  if [[ "$MASK" == "grpc-tls" ]]; then
    log "  gRPC service: $GRPC_SERVICE  (client: address=$HOST_ADDRESS, security=TLS, network=gRPC, sni=$DOMAIN, alpn=h2,http/1.1, fp=$FP)"
    log "  gRPC upstream: nginx :443 → 127.0.0.1:$GRPC_PORT"
  else
    log "  Reality pub:  $REALITY_PUBLIC"
    log "  shortId:      $REALITY_SHORT_ID"
  fi
  log "  State:        $STATE_DIR/node.json"
  [[ -n "$FRONT_IP" ]] && log "  Front-gate:   tcp/443 restricted to $FRONT_IP (SNI-mirror cascade)"
  if [[ "$BRIDGE" == "1" ]]; then
    log "  Cascade:      EXIT node — SS bridge :$BRIDGE_SS_PORT ($BRIDGE_TAG), user '$BRIDGE_USER'"
  fi
  install_report
  print_entry_config
  reboot_required_note
}

main "$@"
