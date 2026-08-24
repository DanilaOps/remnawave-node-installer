#!/usr/bin/env bash
# check.sh — static QA harness for remnawave-node.sh.
#
# Runs a battery of checks and prints PASS/FAIL per check + summary:
#   1. bash -n           syntax
#   2. shellcheck        (warning+; full list, gating on error-severity only)
#   3. flag parity       parse_args flags <-> usage() text
#   4. README parity     script flags documented in README.ru.md
#   5. SAVED_KEYS        every persisted key is actually assigned in the script
#   6. duplicate funcs   same function defined twice (later silently wins)
#   7. dead funcs        defined but never referenced
#   8. undefined calls   name-like tokens invoked as commands but never defined
#      (heuristic; whitelisted externals)
#   9. safety rails      set -Eeuo pipefail, ERR trap, cleanup trap present
#  10. 3.3.2 compatibility: node image, pinned Xray, keygen response, log path
#  11. misc pitfalls
#
# Usage: bash check.sh [path/to/remnawave-node.sh]
set -u

TARGET="${1:-$(cd "$(dirname "$0")" && pwd)/remnawave-node.sh}"
README="$(dirname "$TARGET")/README.ru.md"

PASS=0; FAIL=0; WARN=0
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
grn()   { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()   { printf '\033[33m%s\033[0m\n' "$*"; }
hdr()   { printf '\n\033[1m── %s ──\033[0m\n' "$*"; }
pass()  { grn "  PASS: $*"; PASS=$((PASS+1)); }
fail()  { red "  FAIL: $*"; FAIL=$((FAIL+1)); }
note()  { ylw "  WARN: $*"; WARN=$((WARN+1)); }

[[ -f "$TARGET" ]] || { red "target not found: $TARGET"; exit 2; }

# ── 1. syntax ────────────────────────────────────────────────────────────────
hdr "1. bash -n (syntax)"
if out=$(bash -n "$TARGET" 2>&1); then pass "syntax OK"; else fail "syntax errors:"; echo "$out"; fi

# ── 2. shellcheck ────────────────────────────────────────────────────────────
hdr "2. shellcheck"
if command -v shellcheck >/dev/null; then
  sc=$(shellcheck -S warning -f gcc "$TARGET" 2>&1 || true)
  errors=$(echo "$sc" | grep -c ': error:' || true)
  warns=$(echo "$sc"  | grep -c ': warning:' || true)
  if [[ -n "$sc" ]]; then echo "$sc" | head -30; fi
  if (( errors > 0 )); then fail "shellcheck: $errors error(s)"; else pass "shellcheck: 0 errors ($warns warning(s))"; fi
else
  note "shellcheck not installed — skipped"
fi

# ── helpers to slice the script ──────────────────────────────────────────────
# body of a top-level function (from 'name()' to first '^}')
fnbody() { sed -n "/^$1()/,/^}/p" "$TARGET"; }

# copy of the script with QUOTED heredoc bodies removed (<<'TAG' ... TAG).
# Those bodies are literal payloads (generated scripts/configs), not installer
# code — analyzing them yields false duplicates/flags.
STRIPPED="$(mktemp)"; trap 'rm -f "$STRIPPED"' EXIT
awk '
  skip && $0 == tag { skip=0; next }
  skip { next }
  match($0, /<<-?'\''([A-Za-z_][A-Za-z0-9_]*)'\''/, m) { tag=m[1]; skip=1; print; next }
  { print }
' "$TARGET" 2>/dev/null > "$STRIPPED" || {
  # BSD awk has no match() 3rd arg — portable fallback
  awk "
    skip && \$0 == tag { skip=0; next }
    skip { next }
    /<<-?'[A-Za-z_][A-Za-z0-9_]*'/ {
      tag=\$0; sub(/.*<<-?'/,\"\",tag); sub(/'.*/,\"\",tag); skip=1; print; next
    }
    { print }
  " "$TARGET" > "$STRIPPED"
}

# ── 3. flag parity: parse_args <-> usage ─────────────────────────────────────
hdr "3. flag parity (parse_args <-> usage)"
pa_flags=$(fnbody parse_args | grep -oE '^\s+(-[a-zA-Z]\|)?(--[a-z0-9-]+(\|--[a-z0-9-]+)*)\)' \
           | tr -d ' )' | tr '|' '\n' | grep -E '^--' | sort -u)
us_flags=$(fnbody usage | grep -oE '\-\-[a-z0-9-]+' | sort -u)
miss_usage=$(comm -23 <(echo "$pa_flags") <(echo "$us_flags"))
miss_parse=$(comm -13 <(echo "$pa_flags") <(echo "$us_flags"))
[[ -z "$miss_usage" ]] && pass "all parse_args flags documented in usage()" \
  || { fail "flags parsed but MISSING from usage():"; echo "$miss_usage" | sed 's/^/    /'; }
[[ -z "$miss_parse" ]] && pass "all usage() flags handled by parse_args" \
  || { fail "flags in usage() but NOT parsed:"; echo "$miss_parse" | sed 's/^/    /'; }

# ── 4. README parity ─────────────────────────────────────────────────────────
hdr "4. README parity"
if [[ -f "$README" ]]; then
  # --help is self-describing; README needn't mention it
  readme_missing=$(while read -r f; do
    [[ "$f" == "--help" ]] && continue
    grep -qF -- "$f" "$README" || echo "$f"
  done <<< "$pa_flags")
  if [[ -z "$readme_missing" ]]; then pass "every flag mentioned in README.ru.md"
  else note "flags not mentioned in README.ru.md:"; echo "$readme_missing" | sed 's/^/    /'; fi
else
  note "README.ru.md not found — skipped"
fi

# ── 5. SAVED_KEYS integrity ──────────────────────────────────────────────────
hdr "5. SAVED_KEYS integrity"
saved=$(sed -n '/^readonly SAVED_KEYS="/,/"$/p' "$TARGET" | sed 's/readonly SAVED_KEYS="//; s/"$//' | tr ' \\' '\n\n' | grep -E '^[A-Z0-9_]+$' | sort -u)
if [[ -z "$saved" ]]; then
  fail "could not extract SAVED_KEYS"
else
  bad=""
  while read -r k; do
    # each saved key must be assigned somewhere (VAR= or read into it)
    grep -qE "(^|[^A-Z0-9_])${k}=" "$TARGET" || grep -qE "read [^#]*\b${k}\b" "$TARGET" || bad+="$k"$'\n'
  done <<< "$saved"
  n=$(echo "$saved" | wc -l | tr -d ' ')
  [[ -z "$bad" ]] && pass "all $n saved keys are assigned in the script" \
    || { fail "SAVED_KEYS never assigned:"; printf '%s' "$bad" | sed 's/^/    /'; }
fi

# ── 6. duplicate function definitions ────────────────────────────────────────
hdr "6. duplicate function definitions"
dups=$(grep -oE '^[a-zA-Z_][a-zA-Z0-9_]*\(\)' "$STRIPPED" | sort | uniq -d)
[[ -z "$dups" ]] && pass "no duplicate function definitions" \
  || { fail "functions defined more than once (later def silently wins):"; echo "$dups" | sed 's/^/    /'; }

# ── 7. dead functions (defined, never referenced) ────────────────────────────
hdr "7. dead functions"
dead=""
while read -r fn; do
  fn=${fn%()}
  # a function referenced only on its own definition line is dead
  uses=$(grep -nE "\b${fn}\b" "$STRIPPED" | grep -vcE "^\s*[0-9]+:\s*${fn}\(\)" || true)
  (( uses == 0 )) && dead+="$fn"$'\n'
done < <(grep -oE '^[a-zA-Z_][a-zA-Z0-9_]*\(\)' "$STRIPPED" | sort -u)
[[ -z "$dead" ]] && pass "no dead (unreferenced) functions" \
  || { note "defined but never referenced:"; printf '%s' "$dead" | sed 's/^/    /'; }

# ── 8. safety rails ──────────────────────────────────────────────────────────
hdr "8. safety rails"
grep -q '^set -Eeuo pipefail' "$TARGET" && pass "set -Eeuo pipefail" || fail "missing 'set -Eeuo pipefail'"
grep -qE 'trap .*ERR' "$TARGET"        && pass "ERR trap present"    || note "no ERR trap"
grep -qE 'trap .*(EXIT|INT|TERM)' "$TARGET" && pass "cleanup trap (EXIT/INT/TERM) present" || note "no EXIT/INT/TERM trap"

# ── 9. secret hygiene ────────────────────────────────────────────────────────
hdr "9. secret hygiene"
leaks=$(grep -nEi '(api[_-]?key|token|secret|password)[[:space:]]*=[[:space:]]*["'"'"'][A-Za-z0-9+/_-]{16,}' "$TARGET" \
        | grep -viE '\$|prompt|read |example|<|EOF|null|\*\*\*' || true)
[[ -z "$leaks" ]] && pass "no hardcoded-looking secrets" \
  || { fail "possible hardcoded secrets:"; echo "$leaks" | sed 's/^/    /' | head -5; }
priv=$(grep -nE 'BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY' "$TARGET" || true)
[[ -z "$priv" ]] && pass "no embedded private keys" || { fail "private key material found:"; echo "$priv"; }

# ── 10. Remnawave 3.3.2 compatibility ───────────────────────────────────────
hdr "10. Remnawave 3.3.2 compatibility"
grep -qE '^INSTALLER_VERSION="3\.3\.2-[^"]+"$' "$TARGET" \
  && pass "installer version targets 3.3.2" || fail "INSTALLER_VERSION is not a 3.3.2 build"
grep -qE '^NODE_IMAGE=.*ghcr\.io/remnawave/node:3\.3\.2' "$TARGET" \
  && pass "default RemnaNode image is pinned to 3.3.2" || fail "default RemnaNode image is not 3.3.2"
grep -qE '^XRAY_CORE_VERSION="26\.6\.27"$' "$TARGET" \
  && pass "Xray core is pinned to 26.6.27" || fail "Xray core is not pinned to 26.6.27"
grep -qF '${XRAY_CORE_BIN}:/usr/local/bin/xray:ro' "$TARGET" \
  && pass "pinned Xray is mounted over the stock binary" || fail "missing pinned Xray bind mount"
grep -qF '/usr/local/bin/rw-core version' "$TARGET" \
  && pass "running rw-core version is verified" || fail "running rw-core version is not verified"
keygen_body="$(fnbody panel_get_keygen)"
if grep -qF '.response.secretKey' <<<"$keygen_body" && grep -qF '.response.pubKey' <<<"$keygen_body"; then
  pass "keygen reads 3.x secretKey with legacy pubKey fallback"
else
  fail "panel_get_keygen lacks secretKey/pubKey compatibility fallback"
fi
grep -qF ':/var/log/xray' "$TARGET" \
  && pass "RemnaNode Xray logs are mounted at /var/log/xray" || fail "missing /var/log/xray mount"
grep -qF ':/var/log/supervisor' "$TARGET" \
  && fail "obsolete /var/log/supervisor mount is still present" || pass "obsolete supervisor log mount removed"

# ── 11. misc pitfalls ────────────────────────────────────────────────────────
hdr "11. misc pitfalls"
# heredoc EOFs balance is covered by bash -n; check obvious `== ` inside [ ] (not [[ ]])
sq=$(grep -nE '(^|[^[])\[ [^]]*== ' "$TARGET" || true)
[[ -z "$sq" ]] && pass "no '==' inside single [ ] tests" || { note "'==' in [ ]:"; echo "$sq" | head -3; }
# curl without --max-time (hang risk). Only actual invocations: skip package
# lists, `command -v`, comments, DRY-RUN echo lines. Backslash-continued lines
# are joined first so a timeout on the continuation counts; "${args[@]}"-style
# calls are trusted when the array itself is built with a timeout.
JOINED="$(mktemp)"
awk '/\\$/ { sub(/\\$/, ""); printf "%s ", $0; next } { print }' "$STRIPPED" > "$JOINED"
noto=$(grep -nE '(^|\||&&|\(|\$\()\s*curl (-|")' "$JOINED" \
       | grep -vE 'max-time|connect-timeout|command -v|apt-get|DRY-RUN|\$\{args\[@\]\}|^\s*[0-9]+:\s*#' || true)
rm -f "$JOINED"
if [[ -z "$noto" ]]; then pass "every curl has a timeout"
else note "curl without --max-time/--connect-timeout ($(echo "$noto" | wc -l | tr -d ' ') call(s)):"; echo "$noto" | head -5 | sed 's/^/    /'; fi

# ── summary ──────────────────────────────────────────────────────────────────
hdr "SUMMARY"
printf '  passed: %d   failed: %d   warnings: %d\n' "$PASS" "$FAIL" "$WARN"
(( FAIL == 0 )) && { grn "  RESULT: OK"; exit 0; } || { red "  RESULT: PROBLEMS FOUND"; exit 1; }
