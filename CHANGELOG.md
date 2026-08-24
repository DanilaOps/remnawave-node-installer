# Changelog

## 3.3.2-rw2

- Pin the runtime Xray core to `26.6.27` independently of the core bundled in the
  RemnaNode image.
- Download Xray from the official XTLS release, verify its SHA-256 checksum, and
  bind-mount it over `/usr/local/bin/xray` so the pin survives container recreation.
- Generate Reality keys and validate panel configs with the same pinned core.
- Fail verification when the running `/usr/local/bin/rw-core` is not `26.6.27`.
- Add a dedicated Russian step-by-step installation and resume guide.

## 3.3.2-rw1

- Pin the default Remnawave Node image to `ghcr.io/remnawave/node:3.3.2`.
- Read `response.secretKey` from the Remnawave 3.x `/api/keygen` response, while
  retaining a `response.pubKey` fallback for older panels.
- Persist Xray logs from `/var/log/xray` instead of the obsolete
  `/var/log/supervisor` path.
- Update Russian and English installation instructions for tagged GitHub releases.
- Update the panel Rescue CLI command to `docker exec -it remnawave cli`.
- Expand `.gitignore` to keep runtime tokens, keys, certificates, state, and
  generated archives out of Git.
- Add static compatibility assertions and a GitHub Actions QA workflow for the
  Remnawave node installer.
- Make the mirror QA harness independent of the host distribution's package
  manager so it can run reliably on Ubuntu CI.
