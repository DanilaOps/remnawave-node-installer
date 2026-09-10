#!/usr/bin/env bash
# Validate the rendered nginx configuration the way the node will load it.
#
# Preference order: the pinned nginx image (identical to production), a local
# nginx binary, then crossplane as a syntax-only fallback. The script says which
# path it took, because only the first two check directive semantics.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
work="$(mktemp -d)"
image="${REMNAWAVE_NGINX_IMAGE:-nginx:1.29.3-alpine}"

cleanup() { rm -rf "$work"; }
trap cleanup EXIT

# render_templates.yml writes where it is told; give it a directory inside this
# test's own workspace so two runs cannot collide over one file in /tmp, and so
# the existing cleanup takes the rendered files with it.
render="$work/render"
mkdir -p "$work/tls" "$work/selfsteal/html/primary" "$work/acme" "$work/logs" "$render"

openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 1 \
  -keyout "$work/tls/privkey.pem" -out "$work/tls/fullchain.pem" \
  -subj "/CN=node-test-01.example.com" >/dev/null 2>&1

ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  -i localhost, -c local "$root/tests/render_templates.yml" \
  -e certificate_live_dir="$work/tls" \
  -e remnawave_nginx_user=root \
  -e render_output_dir="$render" \
  >/dev/null

cp "$render/remnawave-nginx-test.conf" "$work/nginx.conf"

# The controller's published UI is a second nginx configuration, and it is the one
# that faces the internet. Validate it the same way, wrapped in the minimum http
# context it needs, so directive placement is checked and not just the templating.
{
  printf 'events {}\n'
  printf 'http {\n'
  cat "$render/remnawave-upgrade-map.conf"
  cat "$render/remnawave-ui-restricted.conf"
  printf '}\n'
} > "$work/controller-ui.conf"
{
  printf 'events {}\n'
  printf 'http {\n'
  cat "$render/remnawave-ui-restricted.conf"
  printf '}\n'
} > "$work/controller-ui-nomap.conf"
# Two wrappers on purpose: nginx needs the $connection_upgrade map or it fails
# on an unknown variable, while crossplane rejects the map's empty-string key -
# valid nginx, absent from crossplane's directive list. Each validator gets the
# form it can check; the map itself is four lines and is asserted in the unit
# tests.
cp -r "$render/remnawave-decoy-test/." "$work/selfsteal/html/primary/"

# The rendered config points at container paths for content and logs.
sed -i "s#/var/www/selfsteal#$work/selfsteal#g; s#/var/www/acme#$work/acme#g; s#/var/log/nginx#$work/logs#g" \
  "$work/nginx.conf"

# The controller UI configuration writes its logs into the work directory too.
sed -i "s#/var/log/nginx#$work/logs#g" "$work/controller-ui.conf" "$work/controller-ui-nomap.conf"

validate_with_crossplane() { # validate_with_crossplane <config>
  python3 - "$1" <<'PY'
import json
import sys

import crossplane

# single=True: validate this file only. Following include directives would
# require the image's /etc/nginx/mime.types, whose content is not nginx
# directives and is rejected by a strict directive check.
payload = crossplane.parse(sys.argv[1], catch_errors=True, strict=True, single=True)
errors = payload.get("errors", [])
if errors:
    print(json.dumps(errors, indent=2), file=sys.stderr)
    raise SystemExit("rendered nginx configuration is not valid")
directives = sum(len(config.get("parsed", [])) for config in payload["config"])
print(f"crossplane parsed {directives} top-level directives with no errors")
PY
}

for config in "$work/nginx.conf" "$work/controller-ui.conf"; do
  label="$(basename "$config")"
  if docker info >/dev/null 2>&1; then
    echo "validating $label with the pinned image ($image)"
    # The workspace stays read-only so the container cannot touch the rendered
    # files, but "nginx -t" opens error_log and access_log for writing before it
    # reports success. A tmpfs over the log directory alone is the smallest
    # writable surface that satisfies it: no host path becomes writable, and the
    # workspace is left exactly as this script built it. Nothing else needs to be
    # writable - the pid file and the proxy/client temp paths live under the
    # image's own writable layer, not under $work, and "nginx -t" does not create
    # them anyway.
    docker run --rm \
      --volume "$config:/etc/nginx/nginx.conf:ro" \
      --volume "$work:$work:ro" \
      --tmpfs "$work/logs" \
      "$image" nginx -t
  elif command -v nginx >/dev/null 2>&1; then
    echo "validating $label with the local nginx binary"
    nginx -t -c "$config" -p "$work"
  elif python3 -c "import crossplane" >/dev/null 2>&1; then
    echo "no nginx available: falling back to crossplane for $label"
    case "$config" in
      *controller-ui.conf) validate_with_crossplane "$work/controller-ui-nomap.conf" ;;
      *) validate_with_crossplane "$config" ;;
    esac
  else
    echo "no nginx, docker or crossplane available - cannot validate" >&2
    exit 1
  fi
done
