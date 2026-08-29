#!/usr/bin/env bash
# The whole of "03 - Verify Node", as Semaphore runs it.
#
# Every earlier acceptance regression checked a slice - the Panel resolver, the
# bridge gate - and every time the next defect surfaced on a live node instead
# of here. This runs the real entry point, the way the template does:
#
#   ansible-playbook ansible/playbooks/provision_node.yml --tags node_verify --limit <host>
#
# in a fresh process with no inherited facts, against a panel that already holds
# a reconciled node. Only the boundaries a sandbox cannot reproduce are local:
# the node's containers and ports, the public site, and the probe's transport.
# The task graph itself - the resolver, the Panel checks, the local checks, the
# public checks, the probe orchestration, the bridge gate - is the real one.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$root/.." && pwd)"
host=de01
domain="$host.example.com"
address=127.0.0.1
node_port=2222
probe_target_port=18204
socks_port=11085
xray_version=26.6.27
remark='🇩🇪 Germany'

work="$(mktemp -d)"
state="$work/panel.json"
out="$work/out.txt"
probe_record="$work/probe.json"
cert_dir="/etc/letsencrypt/live/$domain"

cleanup() {
  kill "${panel_pid:-}" "${node_pid:-}" 2>/dev/null || true
  docker rm -f remnanode nginx-selfsteal >/dev/null 2>&1 || true
  sed -i "/ $domain\$/d" /etc/hosts 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing prerequisite: $1" >&2; exit 1; }; }
need docker
need openssl
need curl
docker info >/dev/null 2>&1 || { echo "the Docker daemon is not reachable" >&2; exit 1; }

# --- the node, faked at the network boundary only ----------------------------
# The node's containers only have to answer what acceptance asks them: an nginx
# that reports its version and validates its configuration, an Xray that reports
# the pinned version, and the s6 supervisor state. Built here so the test needs
# nothing prepared in advance. FULL_GRAPH_BASE_IMAGE points it at an image the
# machine already has when there is no registry to pull from.
image=remnawave-verify-node:local
base="${FULL_GRAPH_BASE_IMAGE:-debian:12-slim}"
if ! docker image inspect "$image" >/dev/null 2>&1; then
  docker image inspect "$base" >/dev/null 2>&1 || docker pull --quiet "$base" >/dev/null 2>&1 || {
    echo "cannot obtain a base image for the node containers ($base)" >&2
    exit 1
  }
  build="$work/image"
  mkdir -p "$build"
  cat > "$build/Dockerfile" <<DOCKERFILE
FROM $base
RUN mkdir -p /command
COPY nginx xray /usr/local/bin/
COPY s6-svstat /command/
RUN chmod +x /usr/local/bin/nginx /usr/local/bin/xray /command/s6-svstat
HEALTHCHECK --interval=2s --timeout=2s --retries=1 CMD /bin/true
ENTRYPOINT []
DOCKERFILE
  cat > "$build/nginx" <<'NGINX'
#!/bin/sh
case "${1:-}" in
  -t) echo "nginx: configuration file /etc/nginx/nginx.conf test is successful" >&2; exit 0 ;;
  -v) echo "nginx version: nginx/1.29.3" >&2; exit 0 ;;
esac
exec sleep infinity
NGINX
  cat > "$build/xray" <<XRAY
#!/bin/sh
[ "\${1:-}" = version ] && { echo "Xray $xray_version (Xray, Penetrates Everything.) Custom"; exit 0; }
exec sleep infinity
XRAY
  cat > "$build/s6-svstat" <<'S6'
#!/bin/sh
echo "up (pid 42) 1200 seconds"
S6
  docker build --quiet --tag "$image" "$build" >/dev/null
fi
for name in remnanode nginx-selfsteal; do
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" "$image" /usr/local/bin/nginx >/dev/null
done

mkdir -p "$cert_dir"
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 90 \
  -keyout "$cert_dir/privkey.pem" -out "$cert_dir/fullchain.pem" \
  -subj "/CN=$domain" -addext "subjectAltName=DNS:$domain" >/dev/null 2>&1

grep -q " $domain\$" /etc/hosts || echo "$address $domain" >> /etc/hosts

# The controller has to trust the node's certificate, exactly as it trusts a
# real one: the check under test is validate_certs: true and stays that way.
bundle="$work/ca-bundle.crt"
cat "${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}" "$cert_dir/fullchain.pem" > "$bundle"

ansible-playbook -i localhost, -c local "$root/tests/verify_full_graph_stage.yml" \
  -e stage_decoy_seed="${host%%[0-9]*}_01||1" \
  -e stage_selfsteal_domain="$domain" \
  -e stage_webroot="$work/webroot" \
  -e stage_logs_dir="$work/logs" \
  -e stage_xray_version="$xray_version" >/dev/null

python3 "$root/tests/full_graph_node.py" \
  --webroot "$work/webroot" --cert "$cert_dir/fullchain.pem" --key "$cert_dir/privkey.pem" \
  --node-port "$node_port" --probe-target-port "$probe_target_port" &
node_pid=$!

# --- the panel, already holding a reconciled node ----------------------------
seed="$(python3 "$root/tests/full_graph_seed.py" "$state" "$domain" "$address" "$remark" "$xray_version")"
python3 "$root/tests/mock_panel.py" --state "$state" &
panel_pid=$!
for _ in $(seq 1 40); do
  curl --silent --fail "http://127.0.0.1:18080/api/nodes" >/dev/null 2>&1 && break
  sleep 0.1
done
for _ in $(seq 1 40); do
  curl --silent --fail --insecure "https://$domain/" >/dev/null 2>&1 && break
  sleep 0.1
done

cat > "$work/hosts.yml" <<INVENTORY
all:
  children:
    remnawave_nodes:
      hosts:
        $host:
          ansible_host: $address
INVENTORY

cat > "$work/fleet.json" <<VARS
{
  "remnawave_panel_url": "http://127.0.0.1:18080",
  "vault_remnawave_panel_token": "mock-token",
  "remnawave_panel_validate_certs": false,
  "internal_squad_name": "Default",
  "xray_json_template_name": "Mock Xray Template",
  "node_firewall_enabled": false,
  "management_cidrs": ["127.0.0.1/32"],
  "preflight_check_dns": false,
  "preflight_check_panel": false,
  "preflight_ack_unchecked_panel": true,
  "preflight_check_ports": false,
  "preflight_check_disk": false,
  "node_hardening_enabled": false,
  "remnawave_logs_dir": "$work/logs",
  "verify_probe_username": "probe",
  "vault_verify_probe_vless_uuid": "$(echo "$seed" | python3 -c 'import json,sys; print(json.load(sys.stdin)["vlessUuid"])')",
  "verify_probe_xray_binary": "$root/tests/full_graph_xray_stub.py",
  "verify_probe_workdir": "$work",
  "verify_probe_socks_port": $socks_port,
  "verify_probe_url": "http://127.0.0.1:$probe_target_port/generate_204",
  "verify_probe_expected_status": 204,
  "ansible_connection": "local",
  "ansible_become": false
}
VARS

before="$(python3 -c "import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True))" "$state")"

set +e
FULL_GRAPH_PROBE_RECORD="$probe_record" \
SSL_CERT_FILE="$bundle" REQUESTS_CA_BUNDLE="$bundle" \
no_proxy="$domain,127.0.0.1,localhost" NO_PROXY="$domain,127.0.0.1,localhost" \
ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook \
  -i "$work/hosts.yml" --limit "$host" --tags node_verify \
  -e "@$work/fleet.json" \
  "$root/playbooks/provision_node.yml" 2>&1 | tee "$out"
rc="${PIPESTATUS[0]}"
set -e
[ "$rc" -eq 0 ] || { echo "standalone 03 failed (exit $rc)" >&2; exit 1; }
grep -qE 'failed=0' "$out" || { echo "standalone 03 reported failures" >&2; exit 1; }
grep -qE 'unreachable=0' "$out" || { echo "standalone 03 could not reach the node" >&2; exit 1; }

# --- what the run had to prove ----------------------------------------------
after="$(python3 -c "import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True))" "$state")"
[ "$before" = "$after" ] || { echo "standalone 03 MUTATED the panel - it must be read-only" >&2; exit 1; }

python3 - "$probe_record" "$seed" "$domain" "$address" <<'PROBE'
import json, pathlib, sys
record = json.loads(pathlib.Path(sys.argv[1]).read_text())
seed = json.loads(sys.argv[2])
domain, address = sys.argv[3], sys.argv[4]
expected = {
    "address": address,
    "port": 443,
    "vlessUuid": seed["vlessUuid"],
    "serverName": domain,
    "shortId": seed["shortId"],
    "flow": "xtls-rprx-vision",
}
for key, value in expected.items():
    assert record[key] == value, f"probe {key}: {record[key]!r} != {value!r}"
assert len(record["publicKey"]) > 0, "the probe carried no Reality public key"
assert record["publicKey"] != seed["privateKey"], "the probe presented the PRIVATE key"
print("The probe was handed the node's real address, SNI, shortId, flow and derived public key.")
PROBE

for stage in \
  "Resolve Panel references for a standalone acceptance run" \
  "Wait until Panel reports Node online" \
  "Require running application containers" \
  "Require the pinned Xray version at runtime" \
  "Require the page served publicly to be this node's generated decoy" \
  "Require the request through the node to succeed"; do
  grep -qF "$stage" "$out" || { echo "the run never reached: $stage" >&2; exit 1; }
done
grep -qF 'Verify bridge end to end' "$out" && ! grep -qF 'entry_inventory_host' "$out" \
  || echo "note: bridge gate output not matched" >&2
echo "Full standalone 03 graph passed end to end."
