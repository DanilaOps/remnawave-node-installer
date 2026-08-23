# remnawave-node

**Languages:** English · [Русский](README.ru.md)

Self-contained installer for a **Remnawave selfsteal node**. One script, one server:
it provisions the node locally **and** creates the matching config-profile, node, and
host in the Remnawave panel over the HTTP API — no manual clicking in the UI.

## Compatibility

- **Remnawave Panel / Backend: 3.3.2**.
- **Remnawave Node: `ghcr.io/remnawave/node:3.3.2`** (pinned by default).
- `/api/keygen`: native Remnawave 3.x `response.secretKey`, with a fallback to the
  older `response.pubKey` response.
- Installer version: **3.3.2-rw1**.

Unlike wrapper scripts that `curl | bash` several third-party installers, this script
inlines everything it controls (node container, nginx selfsteal, TLS certificate,
Xray Reality config). The only external component is the optional firewall step
(`jestivald/node-accelerator`), which can be disabled with `--skip-firewall`.

## Install from GitHub

The script installs its own dependencies (Docker, jq, openssl, socat, cron), so a
fresh Debian/Ubuntu server needs nothing but `curl`. For reproducible installs,
the download URL is pinned to release tag `v3.3.2-rw1` instead of the moving
`main` branch.

```bash
apt-get update -qq && apt-get install -y -qq curl

# Recommended: download, syntax-check, then run.
curl -fsSLo /root/remnawave-node.sh \
  https://raw.githubusercontent.com/DanilaOps/remnawave-node-installer/v3.3.2-rw1/remnawave-node.sh
chmod 700 /root/remnawave-node.sh
bash -n /root/remnawave-node.sh
sudo bash /root/remnawave-node.sh
```

Non-interactive install. Put the panel token in a **file** (or an env var) so it
never appears in `ps` or shell history:

```bash
umask 077
read -rsp 'Panel API token: ' RW_TOKEN; echo
printf '%s' "$RW_TOKEN" > /root/panel.token
unset RW_TOKEN

sudo bash /root/remnawave-node.sh -y \
  --domain node1.example.com \
  --panel-url https://panel.example.com \
  --panel-token-file /root/panel.token \
  --whitelist 203.0.113.10 \
  --country NL \
  --acme-email admin@example.com
```

> **Token hygiene.** `--panel-token <value>` still works but is **visible in the
> process list** (`ps -eo cmd`) while the installer runs — prefer
> `--panel-token-file`, `REMNAWAVE_PANEL_TOKEN_FILE`, or `REMNAWAVE_PANEL_TOKEN`.
> **Rotate any token or password you paste into a chat, a ticket, or shell history.**

> For a private repository, use a fine-grained PAT restricted to
> **Contents: Read** and download through authenticated `curl` or the GitHub CLI.
> Never embed a GitHub token or panel token in this script or commit one to the repo.
>
> Alternative (clone once, then run):
> ```bash
> git clone https://github.com/DanilaOps/remnawave-node-installer.git remnawave-node
> sudo bash remnawave-node/remnawave-node.sh
> ```

## What it does

1. Full-upgrades the OS (`apt-get full-upgrade`) and enables **automatic security
   updates** (`unattended-upgrades`) — skip with `--skip-update`. May flag a
   reboot-required for a new kernel.
1. Installs Docker (official `get.docker.com`, skipped if present).
2. Generates a Reality x25519 keypair and shortId (via `xray x25519` from the node image).
3. Writes an **nginx selfsteal** and serves a **real decoy website** (see
   [Decoy site](#decoy-site-reality-masking) below), not a placeholder page.
   By default nginx listens on a **unix socket** (`/dev/shm/nginx.sock`, shared
   with the node over `/dev/shm`) with `proxy_protocol` — no loopback TCP port is
   exposed. Use `--tcp` for a `127.0.0.1:<selfsteal-port>` listener instead. A
   default-server with `ssl_reject_handshake` rejects any SNI other than your
   domain (anti-probe).
4. Issues a TLS certificate:
   - `le443` (default): Let's Encrypt TLS-ALPN on port 443, renewed on a dedicated
     port behind a temporary `iptables` redirect (443 stays owned by Xray in prod);
   - `cf-dns`: Cloudflare DNS-01 wildcard `*.<domain>`, renewed via DNS (no port needed).
5. Deploys the **Remnawave node container** (`ghcr.io/remnawave/node`) with
   `NODE_PORT` + `SECRET_KEY` from the panel.
6. Creates in the panel via API (after validating the generated config with
   `xray -test`): **config-profile** (VLESS inbound(s)), **node** (linked to the
   profile), and **host** (subscription entry). Optionally adds the inbound to an
   **Internal Squad** (`--squad-name`/`--squad-uuid`) so users receive it — else it
   warns with the manual step.
7. Runs `node-accelerator` for the firewall (strict nftables), unless skipped.
   The installer is **fetched before any panel resource is created**, so an
   unreachable `node-accelerator` (or dead network) fails early instead of leaving
   an orphan Config Profile / Node / Host behind. The `protect` phase is time-boxed
   (`--crowdsec-timeout`, default 180s) and CrowdSec can be disabled with
   `--skip-crowdsec`.
8. Applies RKN/DPI hardening (unless `--no-hardening`), then verifies containers,
   certificate, the active `:443` probe, and the renewal cron.

If a run is interrupted (e.g. a network stall during geo/firewall fetch), it prints
the last completed stage, live container state, whether `:443` is serving, and the
exact `--resume` command to finish. Stages are recorded under
`/opt/remnawave-node/state/stages`; `--resume` skips the expensive completed ones.
All answers you gave (domain, panel URL, **panel token**, ports, cert mode, …) are
saved to `/opt/remnawave-node/state/inputs.env` (`chmod 600`) once the plan is
confirmed, so resuming is just `sudo bash remnawave-node.sh --resume -y` — nothing
to re-type. Any CLI/env flag you pass on the resume run still overrides the file.

## Requirements

- Fresh Debian/Ubuntu server, run as root.
- DNS **A record** for the selfsteal domain pointing at the server (grey cloud / DNS-only
  if the zone is on Cloudflare — never proxied).
- Remnawave panel URL and an **API token** with permissions for every endpoint the
  installer calls:

  | Permission | Endpoint | Why |
  |---|---|---|
  | **Keygen** | `GET /api/keygen` | `response.secretKey` → the node's `SECRET_KEY` |
  | **Nodes** (create + read) | `POST/GET /api/nodes` | register/verify the node |
  | **Config Profiles** (create + read + update) | `POST/GET/PATCH /api/config-profiles` | Xray config + inbound UUIDs |
  | **Hosts** (create + update) | `POST/PATCH /api/hosts` | subscription host |

  The common gotcha: a token with Nodes/Hosts/Config-Profiles but **no Keygen**
  returns `403` on `GET /api/keygen`. Either enable that scope, or bypass it with
  `--secret-key '<panel SECRET_KEY>'` (get it from the panel CLI:
  `docker exec -it remnawave cli` → *Get SECRET_KEY for a Remnawave Node*, or copy
  the `SECRET_KEY=` line from any working node's `/opt/remnanode/.env`; it is
  panel-global). Quick scope check:

  ```bash
  T='<token>'; P='https://panel.example.com'
  for ep in keygen nodes hosts config-profiles; do
    printf '%-16s -> ' "$ep"
    curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" "$P/api/$ep"
  done   # all four should print 200
  ```
- For `cf-dns`: a Cloudflare API token with `Zone:DNS:Edit` for the zone.

## Usage

Interactive:

```bash
sudo bash remnawave-node.sh
```

Non-interactive (Let's Encrypt on 443):

```bash
sudo bash remnawave-node.sh -y \
  --domain node1.example.com \
  --panel-url https://panel.example.com \
  --panel-token-file /root/panel.token \
  --whitelist 203.0.113.10 \
  --country NL \
  --acme-email admin@example.com
```

Cloudflare wildcard:

```bash
sudo bash remnawave-node.sh -y \
  --domain node1.example.com \
  --panel-url https://panel.example.com \
  --panel-token-file /root/panel.token \
  --whitelist 203.0.113.10 \
  --cert-mode cf-dns \
  --cf-token cf_xxx \
  --acme-email admin@example.com
```

Dry-run (prints every action, changes nothing, needs no root):

```bash
REMNAWAVE_PANEL_TOKEN=dummy bash remnawave-node.sh --dry-run --domain node1.example.com \
  --panel-url https://panel.example.com --whitelist 1.2.3.4
```

> On a clean server `--dry-run` does **not** require `jq`: if `jq` is absent it
> prints a warning and skips only the Xray config JSON preview; every other step
> is still shown. A real install auto-installs `jq`.

Preflight (read-only: OS/DNS/ports/existing containers + panel token scope — no
changes to the server or panel):

```bash
sudo bash remnawave-node.sh --preflight \
  --domain node1.example.com --panel-url https://panel.example.com \
  --panel-token-file /root/panel.token --whitelist 203.0.113.10 \
  --acme-email admin@example.com
```

Resume after an interrupted install (reuses the panel Config Profile / Node / Host
and the existing Reality keys; skips stages already recorded in
`/opt/remnawave-node/state/stages`):

```bash
sudo bash remnawave-node.sh --resume -y \
  --domain node1.example.com --panel-url https://panel.example.com \
  --panel-token-file /root/panel.token --whitelist 203.0.113.10 \
  --acme-email admin@example.com
```

## Options

| Flag | Default | Purpose |
|---|---|---|
| `--domain` | (required) | selfsteal domain, A-record must resolve here |
| `--panel-url` | (required) | Remnawave panel base URL |
| `--panel-token-file` | (required*) | read the panel token from a file (**preferred** — not in `ps`); or env `REMNAWAVE_PANEL_TOKEN_FILE` / `REMNAWAVE_PANEL_TOKEN` |
| `--panel-token` | (required*) | panel API token on the CLI (back-compat; **visible in `ps`**) |
| `--whitelist` | (required) | panel IP(s)/CIDR allowed to reach `NODE_PORT` |
| `--cert-mode` | `le443` | `le443` \| `cf-dns` |
| `--cf-token` | — | Cloudflare token (required for `cf-dns`) |
| `--acme-email` | (required) | Let's Encrypt account email |
| `--template` | `builtin` | decoy site: `builtin` (self-generated, no fetch) \| id `1-11` \| folder name |
| `--no-randomize` | off | keep the template byte-identical (not recommended) |
| `--country` | `NL` | ISO-2 code; node name becomes `<CC>-<seq>` |
| `--node-name` | auto | override the panel node name |
| `--node-port` | `2222` | panel ↔ node control port |
| `--node-image` | `ghcr.io/remnawave/node:3.3.2` | pinned RemnaNode image; override with `REMNANODE_IMAGE` |
| `--mask` | `reality` | `reality` (Xray owns 443, XTLS-Reality) \| `grpc-tls` (nginx owns 443 with a real cert, VLESS+gRPC behind it — CDN/Cloudflare-frontable) |
| `--grpc-port` | `11443` | loopback port of the gRPC inbound (`grpc-tls`) |
| `--grpc-service` | `grpc` | gRPC serviceName; nginx routes `/<name>/Tun` to Xray (`grpc-tls`) |
| `--host-address` | auto | subscription host connect address (default: DOMAIN for `grpc-tls`, public IP for `reality`) |
| `--squad-name` / `--squad-uuid` | — | Internal Squad to enable the inbound in (so users receive it) |
| `--skip-xray-validate` | off | skip `xray -test` validation of the generated config |
| `--socket` / `--tcp` | `--socket` | (reality) nginx fallback via `/dev/shm/nginx.sock` (default) or loopback TCP |
| `--transport` | `tcp` | (reality) `tcp` (Reality+Vision) \| `xhttp` \| `both` (tcp:443 + xhttp) |
| `--xhttp-port` | `8444` | XHTTP inbound port in `both` mode |
| `--no-geo` | off | skip the runetfreedom geosite/geoip download + mount |
| `--no-hardening` | off | skip RKN/DPI hardening (`tcp_rfc1337`, TTL=128, drop unused protocols, SSH banner, fail2ban) |
| `--rotate-keys` | off | (reality) generate a fresh Reality keypair (clients must resync) |
| `--adopt-profile` | off | allow overwriting a **differently-named** config-profile that already owns this install's inbound tag. Default refuses with an error — the installer never replaces a foreign profile's config silently. After node/host creation it also verifies both reference exactly the profile from this install |
| `--selfsteal-port` | `9443` | local nginx HTTPS port in `--tcp` mode |
| `--renew-port` | `8443` | `le443` renewal TLS-ALPN port |
| `--ssh-port` | auto | SSH port (for firewall) |
| `--fingerprint` | `firefox` | client uTLS fingerprint on the created host. `chrome` is a solid fallback; avoid `randomized` — it breaks some Xray clients (macOS: `tls: CurvePreferences includes unsupported curve`) |
| `--tcp-ports` / `--udp-ports` | `80,443,2087` / `443,2087` | firewall service ports |
| `--na-ref` | `v3.8-rw1` | node-accelerator git ref; the bootstrap installer **and** its modules are fetched at this ref |
| `--node-accelerator-tar` | — | **preferred when GitHub/raw access is unstable on the VPS** — a local node-accelerator tarball (`install.sh` + `scripts/`); `protect`/`optimize` then run fully offline. Build it on macOS with `COPYFILE_DISABLE=1 tar --no-xattrs -czf node-accelerator.tar.gz node-accelerator` to avoid `LIBARCHIVE.xattr` warnings on the server |
| `--node-accelerator-dir` | — | use a local node-accelerator checkout (same offline effect as `--tar`, from an unpacked directory) |
| `--node-accelerator-url` | — | **legacy single-file mode**: override the `install.sh` URL only — `protect` may still fetch modules online, so prefer `--tar`/`--dir` for offline installs |
| `--skip-update` | off | skip the OS full-upgrade + automatic security updates |
| `--skip-firewall` | off | do not run node-accelerator |
| `--skip-crowdsec` | off | tell node-accelerator to skip CrowdSec (avoids slow-APT hangs) |
| `--crowdsec-timeout` | `180` | cap the `protect` phase (seconds) so a stuck CrowdSec step can't wedge the install |
| `--optimize-timeout` | `0` | cap the best-effort `optimize` phase (seconds); `0` = unlimited. XanMod/BBR installs are legitimately slow. `optimize` runs **first** (before the node is provisioned) so any kernel reboot happens before there is a live node; it is best-effort and never aborts the rest. A cap SIGKILLs the whole process group on expiry — only set one if you accept a possibly half-configured apt. |
| `--resume` | off | skip already-completed expensive stages (`/opt/remnawave-node/state/stages`) |
| `--refresh-decoy` | off | regenerate the decoy site even on `--resume` |
| `--preflight` | off | read-only checks (OS/DNS/ports/panel), mutate nothing, then exit |
| `-y`, `--non-interactive` | off | no prompts |
| `--dry-run` | off | simulate |

\* One panel-token source is required: `--panel-token-file` (or the matching env
vars) is preferred; `--panel-token` is accepted for back-compat but is visible in
the process list.

## Decoy site (Reality masking)

The whole point of selfsteal is that an active prober who connects to your domain
sees a **believable, ordinary website** — not a blank page. A bare "It works" stub
is a giveaway that the host is a proxy front. So the installer fetches a real
website template and makes it unique per install.

- **Built-in generator (default, `--template builtin`).** A random business
  landing page is generated locally from built-in themes/colors — **no external
  download**, nothing to hash-match against a public repo, unique per install.
  This is the strongest "own decoy" and the default.

- **Or a real template** (`--template <id|name>`). One of the
  [`sni-templates`](https://github.com/DigneZzZ/remnawave-scripts/tree/main/sni-templates)
  sites is downloaded into `/opt/nginx-selfsteal/html` (repo tarball, with a
  `git sparse-checkout` fallback), then byte-mutated per install:

  | id | name | id | name |
  |----|------|----|------|
  | 1 | `10gag` (memes) | 7 | `modmanager` |
  | 2 | `convertit` | 8 | `speedtest` |
  | 3 | `converter` | 9 | `YouTube` |
  | 4 | `downloader` | 10 | `503-1` (error page) |
  | 5 | `filecloud` | 11 | `503-2` (error page) |
  | 6 | `games-site` | | |

  `builtin` (above) is the overall default; this table applies only when you pick a
  fetched template explicitly. Note `503-1`/`503-2` are deliberately bland error pages —
  pick a content site if you want the decoy to look like a live service.

- **Per-install uniqueness (anti-fingerprint), on by default.** The fetched
  template is byte-mutated so it never hash-matches the public copy: randomised
  brand/title/description, a per-install CSS hue rotation, injected byte noise,
  a freshly generated favicon, and cache-busters. Provenance leaks (`*.md`,
  `*.map`, `LICENSE`) are stripped, the `api.ipify.org` phone-home beacon is
  rewritten to a same-origin path, and external Google Fonts are removed
  (off-box fetch, breaks where blocked). Disable with `--no-randomize` (not
  recommended — it leaves the decoy byte-identical to the public template).

- **If the fetch fails**, the installer falls back to the **built-in generator**
  (a believable, unique decoy) and warns — never a bare "It works" stub. Re-run
  with network access if you specifically wanted the fetched template.

### DNS and CDN

- **`reality`** — the selfsteal domain must be **DNS-only / grey-cloud** (never
  Cloudflare-proxied): a proxy terminates TLS and breaks both ACME and Reality.
- **`grpc-tls` direct** (the default for that mask) — a plain **A record** points
  at the node server. Also DNS-only for `le443` issuance.
- **`grpc-tls` behind a CDN** (optional) — because nginx serves genuine TLS/HTTP-2,
  it *can* sit behind Cloudflare, but that needs care:
  - certificate issuance must still succeed — use `--cert-mode cf-dns` (DNS-01),
    or issue while the record is temporarily grey-cloud, then enable the proxy;
  - the CDN must allow gRPC/HTTP-2 to the `/<serviceName>/Tun` path;
  - the host **Address** must be the **domain** (already the `grpc-tls` default),
    not the origin IP.

## Masking models (`--mask`)

Two independent ways to hide the proxy. Pick one per node (they both own `:443`,
so they are mutually exclusive).

- **`reality`** (default) — Xray owns public `:443` and speaks **XTLS-Reality**:
  it borrows a real site's TLS handshake, no certificate of your own is presented
  on the wire. nginx is only the internal fallback/decoy. Best all-round DPI
  resistance; **not** CDN-frontable. Supports `--transport tcp|xhttp|both`.

- **`grpc-tls`** — **nginx** owns public `:443` with a **real Let's Encrypt
  certificate** and serves the decoy site directly; a single `location
  /<serviceName>/Tun` is `grpc_pass`ed to a loopback **VLESS + gRPC** Xray inbound
  (`127.0.0.1:<--grpc-port>`, security `none`). The client link is
  `security=TLS, network=gRPC, alpn=h2,http/1.1` with **no Reality and no Vision
  flow**. Because it is ordinary TLS/HTTP-2 to a genuine site, it can sit **behind
  a CDN / Cloudflare** and survives active probing as a real website. Adapted from
  [NikitaAzmov/GRPC](https://github.com/NikitaAzmov/GRPC).

  ```bash
  sudo bash remnawave-node.sh --mask grpc-tls \
    --grpc-service media.session.poll --grpc-port 11443 …
  ```

  Panel host it creates: `address=<domain>`, `host=<domain>`, `sni=<domain>`,
  `port=443`, `securityLayer=TLS`, `network=gRPC`, `serviceName=<--grpc-service>`,
  `alpn=h2,http/1.1`, `fingerprint=chrome` (default). Change `--grpc-service` and
  the config-profile JSON together.

## RKN / DPI hardening

On by default (`--no-hardening` to skip). Selected safe parts of
[NikitaAzmov/RKN-PROTECT](https://github.com/NikitaAzmov/RKN-PROTECT), additive to
the node-accelerator firewall:

- **`tcp_rfc1337=1`** — stack-level defence against RKN/TSPU RST-injection
  (deliberately *not* an nftables RST-drop, which would sever the panel↔node link).
- **nftables TTL/hoplimit = 128** in `postrouting` (after Docker NAT, own table
  `inet rknnode`) — normalises hop count / masks the OS from TSPU; persisted via a
  small systemd unit.
- **Disable `dccp`/`sctp`/`rds`/`tipc`** kernel modules (attack-surface reduction).
- **SSH banner minimized** — `DebianBanner no` + `Banner none` in `sshd_config`
  (drops the `-Debian/-Ubuntu` OS hint from the SSH greeting; cosmetic — OpenSSH
  still emits its own version).
- **fail2ban SSH jail** — installed if absent; bans brute-forcers on the SSH port
  (5 tries / 10m → 1h). node-accelerator IP-restricts only the panel port, not SSH.

BBR / congestion control is left to node-accelerator; `tcp_timestamps` untouched.

## Reality invariants enforced

- Xray owns public `:443`; nginx is never internet-facing (unix socket, or loopback TCP with `--tcp`).
- Reality `dest` → local nginx (`/dev/shm/nginx.sock` or `127.0.0.1:<port>`), `serverNames` = `<domain>`, `xver: 1` (PROXY protocol).
- Flow `xtls-rprx-vision` is used **only on raw/TCP** Reality; `show: false`.
- Certificate renewal never fights Xray for 443 (redirect port or DNS-01).

### Xray flow rules

Vision flow (`xtls-rprx-vision`) is valid **only** on raw/TCP Reality. It must not
be set on XHTTP or gRPC:

| Mode | Inbound | Flow |
|---|---|---|
| `--transport tcp` | VLESS + Reality + raw | `xtls-rprx-vision` |
| `--transport xhttp` | VLESS + Reality + XHTTP | none |
| `--transport both` | raw:443 **+** xhttp:`<port>` | raw = Vision; xhttp = none |
| `--mask grpc-tls` | VLESS + gRPC behind nginx TLS | none (no Reality) |

### Transport modes (`reality` mask)

`--transport` selects the inbound(s):

- **`tcp`** (default) — VLESS + Reality + Vision on `:443`; works with every
  client (Happ, v2rayng, mihomo/podkop).
- **`xhttp`** — VLESS + Reality over XHTTP on `:443` (no flow); masks as HTTP
  requests to slip past providers that throttle VLESS-TCP.
- **`both`** — `tcp:443` (Vision) **and** `xhttp:<--xhttp-port>` (no flow) on one
  node with a shared key; the panel gets a host for each.

### Geo routing data

By default the installer downloads the [runetfreedom](https://github.com/runetfreedom/russia-v2ray-rules-dat)
`geosite.dat`/`geoip.dat` into the node and mounts them into Xray's asset dir, with
a daily refresh cron — so the `geosite:*`/`geoip:*` routing rules use fresh,
RU-tuned data instead of the image's bundled set. Disable with `--no-geo`.

> Kernel tuning (BBR, TCP buffers) is intentionally **not** done here — the
> firewall step (`node-accelerator optimize`) already installs XanMod + BBRv3.

### Firewall & custom inbound ports

`node-accelerator protect` installs a **strict nftables allowlist**: only the ports
in `--tcp-ports`/`--udp-ports` (plus SSH, `NODE_PORT`, the renewal port) are open —
**everything else is dropped**. If you add an inbound on a non-standard port later
(e.g. a Shadowsocks bridge on `:9999`), you must include that port in
`--tcp-ports`/`--udp-ports`, otherwise the port is silently filtered even though
Xray listens on it (symptom: `connect` times out from outside, port shows
`filtered`). For a trusted upstream peer, whitelist its IP instead of opening the
port to the world. Note: hand-edits to the generated `na_filter.nft` survive a
reboot (via `na-firewall.service`) but **not** a re-run of `node-accelerator
protect` — re-apply them through `--tcp-ports`/`--udp-ports` or the accelerator's
own config.

The generated Xray config is hardened for a smooth client experience: `UseIPv4`
DNS + `UseIPv4` DIRECT egress (avoids broken-IPv6 stalls, e.g. YouTube), sniffing
with `routeOnly: true` (route by SNI, connect to the original IP — no per-connection
re-resolve, keeps QUIC/HTTP3 fast), and routing that blocks `geosite:private`,
`category-ads-all`, private IPs and bittorrent. Reality uses the modern field names
(`target`, `password`), server-only fields (`privateKey`), and drops client-only
ones (`publicKey`, `spiderX`).

Latency tuning: a `policy` with `uplinkOnly:0`/`downlinkOnly:0` tears connections
down immediately on half-close (the default ~1s linger is costly for short-lived
web requests, and doubles on a 2-hop bridge), `connIdle:300` keeps keep-alive
sockets 5 min, and `tcpNoDelay` (Nagle off) is set on the inbounds and the DIRECT
egress for lower interactive latency. Kernel-level BBR/fq/fastopen are handled by
`node-accelerator optimize`.

After install, the panel has the config-profile, node and host wired. State is
saved to `/opt/remnawave-node/state/node.json`.

### Internal Squad (required for users to see the inbound)

Remnawave's access chain is **Config Profile → active Inbounds on Node → Host →
Internal Squad → Users**. The installer automates everything up to the Host, but a
user only receives the inbound once it is enabled in an **Internal Squad**:

- Pass `--squad-name '<name>'` (or `--squad-uuid <uuid>`) and the installer adds the
  new inbound UUID(s) to that squad (union — it never removes existing inbounds).
  Squad API calls are best-effort: on a scope/shape mismatch it warns instead of
  failing the install.
- Otherwise the installer **warns loudly** with the manual step: in the panel open
  *Internal Squads → edit/create a squad → enable the inbound → save*, then attach
  the squad to your subscription/users.

## Management CLI (`remnanode`)

The installer drops a `remnanode` command in `/usr/local/bin` to operate the node
after install (run with no argument for an interactive menu):

```
remnanode status              # containers, mode, socket/cert, active :443 probe, panel UUIDs
remnanode logs [node|nginx] [-f]
remnanode up | down | restart
remnanode template [id|name]  # list, or swap the decoy site (fetch + mutate + reload)
remnanode renew               # force a certificate renewal now
remnanode uninstall           # remove containers + files (panel resources untouched)
remnanode menu                # interactive menu (default)
```

`remnanode status` runs the real active probe — a plain TLS client against your
public `:443` — and reports `HTTP 200` when the decoy is served through the full
Reality→fallback→nginx chain.

## Maintenance

- **logrotate** (`/etc/logrotate.d/remnawave-node`): daily rotation, keep 7,
  compressed, `copytruncate` — for both node and nginx logs.
- **Auto-restart watchdog** (cron, every 5 min): `restart: always` handles crashes;
  the watchdog additionally `docker start`s a container stuck in a non-running
  state that compose won't self-heal.

## Layout on disk

```
/opt/remnanode/            node container (docker-compose.yml, .env — mode 600; /dev/shm mounted in socket mode)
/opt/nginx-selfsteal/      nginx selfsteal (compose, nginx.conf, conf.d, ssl, html, acme-renew.sh)
/opt/remnawave-node/state/ node.json (UUIDs, keys), stages (resume markers), config.env (CLI), watchdog.sh, install-report-*.log (written only when the install finished with warnings)
/opt/remnawave-node/cache/ cached node-accelerator installer (for offline / flaky-network reruns)
/usr/local/bin/remnanode   management CLI
/etc/logrotate.d/remnawave-node
/root/.acme.sh/            acme.sh + certificates
```

## Re-running (idempotent)

Safe to run again on the same server/panel. The installer matches the
config-profile by name, the node by name/address, and the host by remark+address:
existing resources are **updated in place** (no duplicates), and the existing
Reality keys are reused so current subscriptions keep working. A still-valid
certificate (>30 days) is not re-issued.

Three names are prompted separately because the panel validates them differently:

| Prompt / flag     | Applies to        | Allowed characters |
|-------------------|-------------------|--------------------|
| `--node-name`     | panel node        | free (brackets, emoji OK) |
| `--host-remark`   | subscription host | free (emoji OK), ~40-char label |
| `--profile-name`  | config-profile    | letters, numbers, `_`, `-`, space only |

## Troubleshooting (install)

| Symptom | Cause / fix |
|---|---|
| `--dry-run` dies with `Required command not found: jq` | fixed — dry-run no longer needs `jq`; it just skips the config JSON preview. Update to the current script. |
| Token visible in `ps -eo cmd` | use `--panel-token-file` / `REMNAWAVE_PANEL_TOKEN_FILE` / `REMNAWAVE_PANEL_TOKEN` instead of `--panel-token`, then **rotate** the exposed token. |
| `Failed to fetch node-accelerator` / `raw.githubusercontent.com` times out | happens **before** any panel resource is created. Preload with `--node-accelerator-dir <dir>` or `--node-accelerator-tar <file>`, override with `--node-accelerator-url`, or `--skip-firewall`. A previously cached copy under `/opt/remnawave-node/cache` is reused automatically. |
| `kex_exchange_identification: Connection closed by remote host` right after install | usually a temporary OpenSSH per-source penalty / fail2ban cooldown after the firewall changes, **not** a broken node. Wait 30–60 s and retry SSH once or twice before inspecting fail2ban/nftables. |
| `protect` hangs at "CrowdSec APT repository" | the phase is capped by `--crowdsec-timeout` (180s); pass `--skip-crowdsec` to skip CrowdSec entirely. After `protect` the installer **verifies `nft list table inet na_filter` is live and disarms node-accelerator's safety timer** before starting the node — if verification fails it stops (does **not** start `remnanode`) and the stage stays incomplete so `--resume` retries it. |
| `optimize` slow / didn't apply BBR | `optimize` installs the XanMod kernel + BBRv3 — legitimately slow. It runs **first** (before node provisioning) and is **unlimited by default**; it is best-effort and never aborts the rest. If it didn't finish (or you set `--optimize-timeout` and it was killed), just run it by hand: `sudo bash /opt/remnawave-node/cache/node-accelerator-*/install.sh optimize` (after an interrupted apt: `sudo dpkg --configure -a` first). The final **verify prints the real congestion control (bbr/cubic), qdisc, and kernel** — if it shows `cubic`, BBR isn't active yet. A `CrowdSec bouncer not active` line is **expected and harmless** with `--skip-crowdsec`. |
| `geosite.dat` download times out | fixed — geo downloads retry, resume (`curl -C -`), use the `--geo-timeout` budget (600s), and never mount a partial file; a failure is **never fatal** (Xray keeps its bundled geo data) and `--resume` retries geo. |
| Interrupted after panel resources were created | re-run with `--resume` — it reuses the Config Profile / Node / Host and the existing Reality keys, and skips completed stages. No duplicate panel objects. |

## Troubleshooting (`grpc-tls`)

`/health` returns 200 but the gRPC tunnel does not connect:

```bash
ss -lntp | grep ':<grpc-port>'          # Xray gRPC inbound must listen on 127.0.0.1:<grpc-port>
docker logs remnanode --tail 100        # Xray errors
docker logs nginx-selfsteal --tail 100  # nginx error log (grpc_pass failures)
```

Check, in order:

- `serviceName` matches in **all three**: nginx `location /<name>/Tun`, the Xray
  config-profile inbound (`grpcSettings.serviceName`), and the Host/client link.
- Host **Address / SNI / Host** are all the **domain** (not the origin IP).
- Host **ALPN** is `h2,http/1.1`.
- The config-profile is **active on the node** (`remnanode status` → active inbounds).
- The inbound is **enabled for users via an Internal Squad** (see above) — the most
  common "host exists but users get nothing" cause.

## Notes

- `SECRET_KEY` is passed to the node via `.env` (mode 600), not the process list.
- The panel API token is read silently when prompted (not echoed).
- The script fails loudly on any panel API non-2xx response instead of continuing.
- The generated Xray config is validated with `xray -test` before it is pushed to
  the panel (skip with `--skip-xray-validate`).

## License

MIT.
