# remnawave-node

**Языки:** [English](README.md) · Русский

Самодостаточный установщик **selfsteal-ноды Remnawave**. Один скрипт, один сервер:
поднимает ноду локально **и** создаёт соответствующие config-profile, node и host
в панели Remnawave через HTTP API — без ручных действий в UI.

## Совместимость

- **Remnawave Panel / Backend: 3.3.2**.
- **Remnawave Node: `ghcr.io/remnawave/node:3.3.2`** (закреплён по умолчанию).
- `/api/keygen`: основной формат 3.x `response.secretKey`; для старых панелей
  сохранён fallback на `response.pubKey`.
- Версия установщика: **3.3.2-rw1**.

В отличие от скриптов-обёрток, которые `curl | bash` тянут несколько сторонних
установщиков, этот скрипт инлайнит всё, что контролирует (контейнер ноды, nginx
selfsteal, TLS-сертификат, Xray Reality-конфиг). Единственный внешний компонент —
необязательный шаг firewall (`jestivald/node-accelerator`), отключается через
`--skip-firewall`.

## Установка из GitHub

Скрипт сам ставит зависимости (Docker, jq, openssl, socat, cron), поэтому чистому
серверу Debian/Ubuntu нужен только `curl`. Для воспроизводимых установок ссылка
закреплена на tag/release (`v3.3.2-rw1`), а не на меняющуюся ветку `main`.

```bash
apt-get update -qq && apt-get install -y -qq curl

# Рекомендуемый вариант: скачать, проверить синтаксис, затем запустить.
curl -fsSLo /root/remnawave-node.sh \
  https://raw.githubusercontent.com/DanilaOps/remnawave-node-installer/v3.3.2-rw1/remnawave-node.sh
chmod 700 /root/remnawave-node.sh
bash -n /root/remnawave-node.sh
sudo bash /root/remnawave-node.sh
```

Неинтерактивный запуск. Токен панели передавай **файлом** (или через env), чтобы
он не светился в `ps` и истории shell:

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

> **Гигиена токена.** `--panel-token <значение>` работает, но **виден в списке
> процессов** (`ps -eo cmd`) во время установки — предпочитайте
> `--panel-token-file`, `REMNAWAVE_PANEL_TOKEN_FILE` или `REMNAWAVE_PANEL_TOKEN`.
> **Ротируйте любой токен/пароль, вставленный в чат, тикет или историю shell.**

> Для приватного репозитория используй fine-grained PAT только с правом
> **Contents: Read** и скачивай файл авторизованным `curl`/GitHub CLI. Не встраивай
> GitHub-токен или токен панели в сам скрипт и не публикуй их в репозитории.
>
> Альтернатива — клонировать репозиторий один раз:
> ```bash
> git clone https://github.com/DanilaOps/remnawave-node-installer.git remnawave-node
> sudo bash remnawave-node/remnawave-node.sh
> ```

## Что делает

1. Полностью обновляет ОС (`apt-get full-upgrade`) и включает **автоматические
   обновления безопасности** (`unattended-upgrades`) — отключить `--skip-update`.
   Может пометить, что нужен ребут (новое ядро).
1. Ставит Docker (официальный `get.docker.com`, пропускает если уже есть).
2. Генерирует пару ключей Reality x25519 и shortId (через `xray x25519` из образа ноды).
3. Пишет **nginx selfsteal** и отдаёт **настоящий сайт-заглушку** (см.
   [Сайт-заглушка](#сайт-заглушка-маскировка-reality) ниже), а не placeholder.
   По умолчанию nginx слушает **unix-сокет** (`/dev/shm/nginx.sock`, шарится с
   нодой через `/dev/shm`) с `proxy_protocol` — TCP-порт на loopback не торчит.
   `--tcp` переключает на `127.0.0.1:<selfsteal-port>`. Default-server с
   `ssl_reject_handshake` рубит любой SNI кроме твоего домена (анти-проб).
4. Выпускает TLS-сертификат:
   - `le443` (по умолчанию): Let's Encrypt TLS-ALPN на порту 443, продление на
     отдельном порту за временным `iptables`-редиректом (в проде 443 держит Xray);
   - `cf-dns`: Cloudflare DNS-01 wildcard `*.<домен>`, продление через DNS (порт не нужен).
5. Разворачивает **контейнер ноды Remnawave** (`ghcr.io/remnawave/node`) с
   `NODE_PORT` + `SECRET_KEY` от панели.
6. Создаёт в панели через API (предварительно проверив конфиг через `xray -test`):
   **config-profile** (VLESS inbound(ы)), **node** (привязанную к профилю) и
   **host** (запись подписки). Опционально добавляет inbound в **Internal Squad**
   (`--squad-name`/`--squad-uuid`), чтобы его увидели пользователи — иначе выводит
   громкое предупреждение с ручным шагом.
7. Запускает `node-accelerator` для firewall (strict nftables), если не пропущено.
   Установщик **скачивается до создания ресурсов панели**, поэтому недоступный
   `node-accelerator` (или мёртвая сеть) падает рано, не оставляя осиротевших
   Config Profile / Node / Host. Фаза `protect` ограничена по времени
   (`--crowdsec-timeout`, по умолчанию 180с), а CrowdSec отключается через
   `--skip-crowdsec`.
8. Применяет RKN/DPI-хардинг (если не `--no-hardening`), затем проверяет контейнеры,
   сертификат, активную пробу `:443` и cron автопродления.

Если запуск прерван (например, зависла сеть на скачивании geo/firewall), выводится
последняя завершённая стадия, состояние контейнеров, отдаёт ли `:443` заглушку и
точная команда `--resume` для завершения. Стадии пишутся в
`/opt/remnawave-node/state/stages`; `--resume` пропускает уже выполненные дорогие.
Все введённые ответы (домен, URL панели, **токен панели**, порты, режим серта, …)
после подтверждения плана сохраняются в `/opt/remnawave-node/state/inputs.env`
(`chmod 600`), поэтому продолжить — просто `sudo bash remnawave-node.sh --resume -y`,
ничего не вводить заново. Любой флаг CLI/env на повторном запуске перекрывает файл.

## Требования

- Чистый сервер Debian/Ubuntu, запуск от root.
- **A-запись** selfsteal-домена на IP сервера (серое облако / DNS-only, если зона в
  Cloudflare — никогда не проксировать).
- URL панели Remnawave и **API-токен** с правами на все эндпоинты, которые вызывает
  установщик:

  | Право | Эндпоинт | Зачем |
  |---|---|---|
  | **Keygen** | `GET /api/keygen` | `response.secretKey` → `SECRET_KEY` ноды |
  | **Nodes** (create + read) | `POST/GET /api/nodes` | регистрация/проверка ноды |
  | **Config Profiles** (create + read + update) | `POST/GET/PATCH /api/config-profiles` | Xray-конфиг + UUID инбаундов |
  | **Hosts** (create + update) | `POST/PATCH /api/hosts` | host для подписки |

  Частая ошибка: у токена есть Nodes/Hosts/Config-Profiles, но **нет Keygen** —
  тогда `GET /api/keygen` отдаёт `403`. Либо включи это право, либо обойди через
  `--secret-key '<SECRET_KEY панели>'` (взять из CLI панели:
  `docker exec -it remnawave cli` → *Get SECRET_KEY for a Remnawave Node*, или
  скопировать строку `SECRET_KEY=` из `.env` любой рабочей ноды в
  `/opt/remnanode/.env`; значение панель-глобальное). Быстрая проверка scope:

  ```bash
  T='<токен>'; P='https://panel.example.com'
  for ep in keygen nodes hosts config-profiles; do
    printf '%-16s -> ' "$ep"
    curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" "$P/api/$ep"
  done   # все четыре должны вернуть 200
  ```
- Для `cf-dns`: Cloudflare API-токен с `Zone:DNS:Edit` для нужной зоны.

## Использование

Интерактивно:

```bash
sudo bash remnawave-node.sh
```

Неинтерактивно (Let's Encrypt на 443):

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

Dry-run (печатает все действия, ничего не меняет, root не нужен):

```bash
REMNAWAVE_PANEL_TOKEN=dummy bash remnawave-node.sh --dry-run --domain node1.example.com \
  --panel-url https://panel.example.com --whitelist 1.2.3.4
```

> На чистом сервере `--dry-run` **не требует** `jq`: если `jq` нет — печатается
> предупреждение и пропускается только превью JSON конфига Xray; остальные шаги
> показываются. Реальная установка ставит `jq` сама.

Preflight (только чтение: ОС/DNS/порты/существующие контейнеры + доступ токена к
панели — сервер и панель не меняются):

```bash
sudo bash remnawave-node.sh --preflight \
  --domain node1.example.com --panel-url https://panel.example.com \
  --panel-token-file /root/panel.token --whitelist 203.0.113.10 \
  --acme-email admin@example.com
```

Resume после прерванной установки (переиспользует Config Profile / Node / Host
панели и существующие ключи Reality; пропускает стадии из
`/opt/remnawave-node/state/stages`):

```bash
sudo bash remnawave-node.sh --resume -y \
  --domain node1.example.com --panel-url https://panel.example.com \
  --panel-token-file /root/panel.token --whitelist 203.0.113.10 \
  --acme-email admin@example.com
```

## Опции

| Флаг | По умолчанию | Назначение |
|---|---|---|
| `--domain` | (обязательно) | selfsteal-домен, A-запись должна вести сюда |
| `--panel-url` | (обязательно) | базовый URL панели Remnawave |
| `--panel-token-file` | (обязательно*) | читать токен из файла (**предпочтительно** — не виден в `ps`); или env `REMNAWAVE_PANEL_TOKEN_FILE` / `REMNAWAVE_PANEL_TOKEN` |
| `--panel-token` | (обязательно*) | API-токен панели в командной строке (совместимость; **виден в `ps`**) |
| `--whitelist` | (обязательно) | IP/CIDR панели с доступом к `NODE_PORT` |
| `--front-ip` | — | каскад-бэкенд: закрыть tcp/443 на всех, кроме исходящих IP SNI-зеркала (nft-таблица `mirror_gate`); пусто = 443 открыт всем. Loopback и established остаются разрешены |
| `--cert-mode` | `le443` | `le443` \| `cf-dns` |
| `--cf-token` | — | токен Cloudflare (обязателен для `cf-dns`) |
| `--acme-email` | (обязательно) | email аккаунта Let's Encrypt |
| `--template` | `builtin` | сайт-заглушка: `builtin` (свой генератор, без скачивания) \| id `1-11` \| имя папки |
| `--no-randomize` | off | не мутировать шаблон (не рекомендуется) |
| `--randomize` | on | форс-включить мутацию шаблона (инверсия `--no-randomize`; на `--resume` перекрывает сохранённое) |
| `--country` | `NL` | ISO-2 код; имя ноды становится `<CC>-<seq>` |
| `--node-name` | авто | переопределить имя ноды в панели |
| `--host-remark` | авто | label хоста в подписке |
| `--profile-name` | авто | имя config-profile (буквы/цифры/`_`/`-`/пробел) |
| `--secret-key` | — | SECRET_KEY ноды (обход `GET /api/keygen`) |
| `--node-port` | `2222` | control-порт панель ↔ нода |
| `--node-public-ip` | авто | публичный IP ноды (нужен за NAT, когда автоопределение выдаёт приватный адрес) |
| `--node-image` | `ghcr.io/remnawave/node:3.3.2` | образ RemnaNode (закреплённая версия; или env `REMNANODE_IMAGE`) |
| `--mask` | `reality` | `reality` (443 у Xray, XTLS-Reality) \| `grpc-tls` (443 у nginx с реальным сертом, VLESS+gRPC за ним — работает через CDN/Cloudflare) |
| `--grpc-port` | `11443` | loopback-порт gRPC-инбаунда (`grpc-tls`) |
| `--grpc-service` | `grpc` | gRPC serviceName; nginx роутит `/<name>/Tun` в Xray (`grpc-tls`) |
| `--host-address` | авто | адрес подключения хоста (дефолт: DOMAIN для `grpc-tls`, публичный IP для `reality`) |
| `--squad-name` / `--squad-uuid` | — | Internal Squad, куда включить inbound (чтобы его получили пользователи) |
| `--bridge` | off | режим каскадного моста: эта нода — **выход**, поднять SS-inbound и в конце напечатать конфиг входной ноды (см. раздел «Каскадный мост») |
| `--bridge-entry-ip` | — | IP входной ноды; добавляется в whitelist (SS-порт не открывается всему интернету) |
| `--bridge-ss-port` | `9999` | порт SS-inbound моста |
| `--bridge-user` | — | имя юзера панели; его `ssPassword` = секрет моста (3-36 символов, буквы/цифры/`_`/`-`) |
| `--entry-domain` | — | selfsteal-домен входной ноды (только для печатаемого конфига) |
| `--skip-xray-validate` | off | пропустить проверку конфига через `xray -test` |
| `--socket` / `--tcp` | `--socket` | (reality) fallback через `/dev/shm/nginx.sock` (дефолт) или loopback TCP |
| `--transport` | `tcp` | (reality) `tcp` (Reality+Vision) \| `xhttp` \| `both` (tcp:443 + xhttp) |
| `--xhttp-port` | `8444` | порт XHTTP-инбаунда в режиме `both` |
| `--no-geo` | off | не качать/монтировать geosite/geoip runetfreedom |
| `--no-hardening` | off | не применять RKN/DPI-хардинг (`tcp_rfc1337`, TTL=128, отключение протоколов, SSH-баннер, fail2ban) |
| `--hardening` | on | форс-включить хардинг (инверсия `--no-hardening`; на `--resume` перекрывает сохранённое) |
| `--rotate-keys` | off | (reality) сгенерировать новую пару ключей Reality (клиентам нужен ресинк) |
| `--no-rotate-keys` | on | оставить существующую пару ключей (инверсия `--rotate-keys`; на `--resume` перекрывает сохранённое) |
| `--adopt-profile` | off | разрешить перезапись config-profile **с другим именем**, который уже владеет inbound-тегом этой установки. По умолчанию — отказ с ошибкой: инсталлятор никогда молча не заменяет чужой профиль. После создания node/host дополнительно проверяется, что оба ссылаются именно на профиль этой установки |
| `--selfsteal-port` | `9443` | локальный HTTPS-порт nginx в режиме `--tcp` |
| `--renew-port` | `8443` | порт TLS-ALPN для продления `le443` |
| `--ssh-port` | авто | SSH-порт (для firewall) |
| `--fingerprint` | `firefox` | client uTLS fingerprint на созданном host. `chrome` — надёжный fallback; `randomized` не использовать — ломает часть Xray-клиентов (macOS: `tls: CurvePreferences includes unsupported curve`) |
| `--tcp-ports` / `--udp-ports` | `80,443,2087` / `443,2087` | сервисные порты firewall |
| `--na-ref` | `v3.8-rw1` | git-ref node-accelerator; и bootstrap-установщик, **и** его модули берутся с этого ref |
| `--node-accelerator-tar` | — | **рекомендуется, когда доступ к GitHub/raw на VPS нестабилен** — локальный tarball node-accelerator (`install.sh` + `scripts/`); тогда `protect`/`optimize` идут полностью offline. На macOS собирайте так: `COPYFILE_DISABLE=1 tar --no-xattrs -czf node-accelerator.tar.gz node-accelerator` — иначе на сервере будут warning'и `LIBARCHIVE.xattr` |
| `--node-accelerator-dir` | — | локальный распакованный чекаут node-accelerator (тот же offline-эффект, что и `--tar`) |
| `--node-accelerator-url` | — | **legacy single-file режим**: переопределяет только URL `install.sh` — `protect` может всё ещё качать модули онлайн, для offline берите `--tar`/`--dir` |
| `--skip-update` | off | не обновлять ОС и не включать авто-обновления безопасности |
| `--skip-firewall` | off | не запускать node-accelerator |
| `--firewall` | on | форс-включить node-accelerator (инверсия `--skip-firewall`; на `--resume` перекрывает сохранённое) |
| `--skip-crowdsec` | off | сказать node-accelerator пропустить CrowdSec (обходит зависания APT) |
| `--crowdsec-timeout` | `180` | лимит фазы `protect` (сек), чтобы зависший CrowdSec не заблокировал установку |
| `--optimize-timeout` | `0` | лимит best-effort фазы `optimize` (сек); `0` = без лимита. Установка XanMod/BBR законно долгая. `optimize` идёт **первым** (до провижена ноды), чтобы возможный ребут ядра случился, пока живой ноды ещё нет; best-effort, остальное не прерывает. Лимит по истечении SIGKILL'ит всю process-group — ставь его, только если готов к возможному недонастроенному apt. |
| `--resume` | off | пропустить уже выполненные дорогие стадии (`/opt/remnawave-node/state/stages`) |
| `--refresh-decoy` | off | перегенерировать заглушку даже при `--resume` |
| `--preflight` | off | только чтение (ОС/DNS/порты/панель), ничего не менять, затем выход |
| `-y`, `--non-interactive` | off | без вопросов |
| `--dry-run` | off | симуляция |

\* Нужен один источник токена: предпочтителен `--panel-token-file` (или env),
`--panel-token` принимается для совместимости, но виден в списке процессов.

## Сайт-заглушка (маскировка Reality)

Смысл selfsteal: активный пробер, зайдя на твой домен, видит **правдоподобный
обычный сайт**, а не пустую страницу. Голая «It works» — палево, что за хостом
прокси-фронт. Поэтому установщик качает настоящий шаблон сайта и делает его
уникальным на каждую установку.

- **Встроенный генератор (дефолт, `--template builtin`).** Случайный
  бизнес-лендинг генерится локально из встроенных тем/цветов — **без внешних
  скачиваний**, не совпадает по хэшу ни с одним публичным репо, уникален на
  установку. Самая сильная «своя заглушка», по умолчанию.

- **Или настоящий шаблон** (`--template <id|name>`). Один из сайтов
  [`sni-templates`](https://github.com/DigneZzZ/remnawave-scripts/tree/main/sni-templates)
  скачивается в `/opt/nginx-selfsteal/html` (tarball, фолбэк `git sparse-checkout`)
  и мутируется побайтово:

  | id | имя | id | имя |
  |----|-----|----|-----|
  | 1 | `10gag` (мемы) | 7 | `modmanager` |
  | 2 | `convertit` | 8 | `speedtest` |
  | 3 | `converter` | 9 | `YouTube` |
  | 4 | `downloader` | 10 | `503-1` (страница ошибки) |
  | 5 | `filecloud` | 11 | `503-2` (страница ошибки) |
  | 6 | `games-site` | | |

  Общий дефолт — `builtin` (выше); эта таблица нужна только если явно выбираешь
  скачиваемый шаблон. `503-1`/`503-2` — намеренно бланковые страницы ошибок;
  для «живого» вида бери контентный сайт.

- **Уникальность per-install (анти-фингерпринт), включено по умолчанию.**
  Скачанный шаблон мутируется побайтово, чтобы не совпадать по хэшу с публичной
  копией: рандомные бренд/title/description, сдвиг оттенка CSS, впрыск байтового
  шума, свежий favicon, cache-busters. Provenance-утечки (`*.md`, `*.map`,
  `LICENSE`) срезаются, phone-home на `api.ipify.org` переписывается на
  same-origin путь, внешние Google Fonts удаляются (запрос за пределы бокса,
  ломается где заблокировано). Отключить: `--no-randomize` (не рекомендуется —
  заглушка останется байт-в-байт как публичный шаблон).

- **Если скачать не удалось**, установщик откатывается на **встроенный генератор**
  (правдоподобная уникальная заглушка) и предупреждает — никакого голого stub.
  Перезапусти с доступом в сеть, если нужен был именно скачиваемый шаблон.

### DNS и CDN

- **`reality`** — домен selfsteal обязан быть **DNS-only / серым облаком** (не под
  Cloudflare-proxy): прокси терминирует TLS и ломает и ACME, и Reality.
- **`grpc-tls` напрямую** (дефолт для этого mask) — обычная **A-запись** на сервер
  ноды. Для выпуска `le443` тоже DNS-only.
- **`grpc-tls` за CDN** (опционально) — nginx отдаёт настоящий TLS/HTTP-2, поэтому
  *может* стоять за Cloudflare, но с оговорками:
  - выпуск сертификата должен пройти — используй `--cert-mode cf-dns` (DNS-01) или
    выпусти при временно сером облаке, затем включи proxy;
  - CDN должен пропускать gRPC/HTTP-2 на путь `/<serviceName>/Tun`;
  - **Address** хоста должен быть **доменом** (уже дефолт для `grpc-tls`), не origin-IP.

## Модели маскировки (`--mask`)

Два независимых способа спрятать прокси. На ноду выбирается один (оба держат
`:443`, поэтому взаимоисключающие).

- **`reality`** (дефолт) — публичный `:443` держит Xray и говорит по
  **XTLS-Reality**: заимствует TLS-хендшейк реального сайта, свой сертификат в
  канал не отдаётся. nginx — только внутренний fallback/заглушка. Лучшая общая
  стойкость к DPI; **не** работает через CDN. Поддерживает `--transport
  tcp|xhttp|both`.

- **`grpc-tls`** — публичный `:443` держит **nginx** с **реальным сертификатом
  Let's Encrypt** и сам отдаёт сайт-заглушку; единственный `location
  /<serviceName>/Tun` через `grpc_pass` идёт на loopback-инбаунд **VLESS + gRPC**
  (`127.0.0.1:<--grpc-port>`, security `none`). Ссылка клиента:
  `security=TLS, network=gRPC, alpn=h2,http/1.1`, **без Reality и без Vision-flow**.
  Так как это обычный TLS/HTTP-2 к настоящему сайту, режим работает **за CDN /
  Cloudflare** и переживает active probing как реальный сайт. Взято из
  [NikitaAzmov/GRPC](https://github.com/NikitaAzmov/GRPC).

  ```bash
  sudo bash remnawave-node.sh --mask grpc-tls \
    --grpc-service media.session.poll --grpc-port 11443 …
  ```

  Хост в панели: `address=<домен>`, `host=<домен>`, `sni=<домен>`, `port=443`,
  `securityLayer=TLS`, `network=gRPC`, `serviceName=<--grpc-service>`,
  `alpn=h2,http/1.1`, `fingerprint=chrome` (дефолт). Меняешь `--grpc-service` —
  меняй и JSON конфиг-профиля.

## RKN / DPI хардинг

Включён по умолчанию (`--no-hardening` — выключить). Отобранные безопасные части
[NikitaAzmov/RKN-PROTECT](https://github.com/NikitaAzmov/RKN-PROTECT), дополняют
firewall node-accelerator:

- **`tcp_rfc1337=1`** — защита от RST-инъекций РКН/ТСПУ на уровне стека
  (намеренно *не* RST-drop в nftables — тот рвёт связь панель↔нода).
- **nftables TTL/hoplimit = 128** в `postrouting` (после Docker NAT, отдельная
  таблица `inet rknnode`) — нормализует число хопов / прячет ОС от ТСПУ;
  сохраняется через маленький systemd-юнит.
- **Отключение модулей `dccp`/`sctp`/`rds`/`tipc`** (сужение поверхности атаки).
- **Минимизация SSH-баннера** — `DebianBanner no` + `Banner none` в `sshd_config`
  (убирает намёк на ОС `-Debian/-Ubuntu` из приветствия SSH; косметика — версия
  OpenSSH всё равно отдаётся).
- **fail2ban jail для SSH** — ставится, если отсутствует; банит брутфорс на
  SSH-порту (5 попыток / 10м → 1ч). node-accelerator ограничивает по IP только
  порт панели, но не SSH.

BBR / congestion control остаётся за node-accelerator; `tcp_timestamps` не трогаем.

## Инварианты Reality (принудительно)

- Публичный `:443` держит Xray; nginx не смотрит в интернет (unix-сокет, либо loopback TCP с `--tcp`).
- Reality `dest` → локальный nginx (`/dev/shm/nginx.sock` или `127.0.0.1:<порт>`), `serverNames` = `<домен>`, `xver: 1` (PROXY).
- Flow `xtls-rprx-vision` — **только на raw/TCP** Reality; `show: false`.
- Продление сертификата никогда не борется с Xray за 443 (порт-редирект или DNS-01).

### Правила flow в Xray

Vision-flow (`xtls-rprx-vision`) допустим **только** на raw/TCP Reality. На XHTTP и
gRPC его быть не должно:

| Режим | Inbound | Flow |
|---|---|---|
| `--transport tcp` | VLESS + Reality + raw | `xtls-rprx-vision` |
| `--transport xhttp` | VLESS + Reality + XHTTP | нет |
| `--transport both` | raw:443 **+** xhttp:`<порт>` | raw = Vision; xhttp = нет |
| `--mask grpc-tls` | VLESS + gRPC за nginx TLS | нет (без Reality) |

### Режимы транспорта (mask `reality`)

`--transport` выбирает инбаунд(ы):

- **`tcp`** (дефолт) — VLESS + Reality + Vision на `:443`; работает со всеми
  клиентами (Happ, v2rayng, mihomo/podkop).
- **`xhttp`** — VLESS + Reality поверх XHTTP на `:443` (без flow), режим
  `packet-up`, путь `/api/v1/update`; маскируется под HTTP API-запросы для обхода
  провайдеров, режущих VLESS-TCP. Путь и `xhttpExtraParams` автоматически
  одинаковы в inbound и в Host, поэтому попадают в подписку без ручной правки.
- **`both`** — `tcp:443` (Vision) **и** `xhttp:<--xhttp-port>` (без flow) на одной
  ноде с общим ключом; в панели создаётся host на каждый. XHTTP-порт не может
  совпадать с 80, 443, SSH, API ноды, selfsteal, renewal-портом или Beszel.

### Уникальные теги Xray

Каждый Config Profile получает namespace из `Node name`: например, `FI-01` →
`FI01`. Поэтому инбаунды создаются как `FI01-REALITY`, `FI01-XHTTP` или
`FI01-GRPC`, а общие outbounds больше не используют конфликтующие имена
`DIRECT`/`BLOCK`: они создаются как `FI01-DIRECT` и `FI01-BLOCK`. Все routing
rules автоматически ссылаются на эти же теги. В каскадном entry-конфиге теги
аналогично начинаются с `ENTRY-FI01-…`.

Имя ноды должно быть уникальным в панели; установщик предлагает следующий номер
для страны и дополнительно отказывается молча перезаписывать профиль, владельцем
inbound-тега которого является другой Config Profile.

**Коллизии после нормализации.** Namespace получается удалением из `Node name`
всех символов кроме букв/цифр и переводом в верхний регистр. Поэтому имена,
отличающиеся только пунктуацией/пробелами, сворачиваются в один и тот же тег:
`FI-01`, `FI_01`, `FI 01` и `fi.01` — все дают `FI01-REALITY`. Молча чужой профиль
это не захватит (установщик остановится на конфликте глобально-уникального
inbound-тега), но вторая такая нода не поставится без вмешательства.

Против этого namespace дополняется коротким детерминированным хэшем исходного
`Node name`, и такие имена начинают различаться (`FI01-ED5809-…`, `FI01-C47B32-…`,
`FI01-CE1DEB-…`, `FI01-A1FE02-…`). Для **чистой установки это включено по
умолчанию** (глобально-уникальные теги). Хэш меняет теги, поэтому на уже
развёрнутой ноде его включать нельзя (сменятся inbound UUID → пересбор
Hosts/Squad/подписок) — и установщик это учитывает: при `--resume` со старым
`inputs.env`, где ключа `NAMESPACE_HASH` ещё не было, хэш **принудительно
выключается**, так что существующие ноды не переразмечаются. На новом state хэш
пересчитывается из сохранённого `Node name` и остаётся прежним. Флаги:
`--namespace-hash` / `--no-namespace-hash` переопределяют поведение явно.

### Geo-данные для routing

По умолчанию установщик качает [runetfreedom](https://github.com/runetfreedom/russia-v2ray-rules-dat)
`geosite.dat`/`geoip.dat` в ноду и монтирует их в asset-каталог Xray, с
ежедневным cron-обновлением — чтобы правила `geosite:*`/`geoip:*` работали на
свежих RU-заточенных данных, а не на встроенных в образ. Отключить: `--no-geo`.

> Тюнинг ядра (BBR, TCP-буферы) здесь намеренно **не** делается — firewall-шаг
> (`node-accelerator optimize`) уже ставит XanMod + BBRv3.

### Firewall и нестандартные порты inbound

`node-accelerator protect` ставит **строгий allowlist nftables**: открыты только
порты из `--tcp-ports`/`--udp-ports` (плюс SSH, `NODE_PORT`, порт продления) —
**всё остальное дропается**. Если позже добавляешь inbound на нестандартном порту
(например Shadowsocks-мост на `:9999`), обязательно добавь этот порт в
`--tcp-ports`/`--udp-ports`, иначе он молча фильтруется, хотя Xray на нём слушает
(симптом: `connect` таймаутит снаружи, порт `filtered`). Для доверенного
upstream-пира whitelist-ни его IP вместо открытия порта миру. Правки
сгенерированного `na_filter.nft` переживают reboot (через `na-firewall.service`),
но **не** переживают повторный `node-accelerator protect` — переноси их в
`--tcp-ports`/`--udp-ports` или в конфиг accelerator.

Генерируемый Xray-конфиг заточен под гладкую работу клиента: DNS `UseIPv4` +
DIRECT-egress `UseIPv4` (без залипаний на битом IPv6, напр. YouTube), sniffing с
`routeOnly: true` (роутинг по SNI, коннект по исходному IP — без пере-резолва на
каждый коннект), и routing, блокирующий `geosite:private`,
`category-ads-all`, приватные IP, bittorrent и **QUIC/HTTP3 (udp:443)**. Блок QUIC
заставляет браузер откатиться на TLS-over-TCP — транспорт, который Reality/Vision
несёт лучше всего (QUIC внутри туннеля тормозит из-за двойного congestion control и
хуже поддаётся sniffing по SNI). То же правило есть в каскадном entry-конфиге.
Reality использует новые имена полей
(`target`, `password`), только серверные поля (`privateKey`) и выкидывает
клиентские (`publicKey`, `spiderX`).

Тюнинг задержки: `policy` c `uplinkOnly:0`/`downlinkOnly:0` рвёт соединение сразу
при half-close (дефолтный ~1s linger дорог для короткоживущих веб-запросов и
удваивается на 2-хоп мосту), `connIdle:300` держит keep-alive сокеты 5 мин, а
`tcpNoDelay` (выкл Nagle) выставлен на inbound-ах и DIRECT-egress для меньшей
интерактивной задержки. BBR/fq/fastopen на уровне ядра делает
`node-accelerator optimize`.

После установки в панели есть config-profile, нода и host. Состояние сохраняется
в `/opt/remnawave-node/state/node.json`.

### Internal Squad (нужен, чтобы пользователи увидели inbound)

Цепочка доступа в Remnawave: **Config Profile → активные Inbound на ноде → Host →
Internal Squad → Пользователи**. Установщик автоматизирует всё до Host, но inbound
доходит до пользователя только после включения в **Internal Squad**:

- Передай `--squad-name '<имя>'` (или `--squad-uuid <uuid>`) — установщик добавит
  UUID inbound(ов) в этот squad (объединение — существующие не удаляются). Вызовы
  API squad — best-effort: при несовпадении scope/формы выводится предупреждение,
  установка не падает.
- Иначе установщик **громко предупреждает** с ручным шагом: в панели открой
  *Internal Squads → редактировать/создать squad → включить inbound → сохранить*,
  затем привяжи squad к подписке/пользователям.

## Каскадный мост (`--bridge`) — эта нода как выход

Режим для **каскада** RU→заграница. Эта нода становится **выходной (exit)**: помимо
обычного VLESS+Reality на `:443` установщик поднимает **Shadowsocks-inbound** на
`:9999` (по умолчанию), который принимает трафик от **входной (entry)** ноды.

Что делает установщик при `--bridge`:

1. Спрашивает **IP входной ноды** и добавляет его в firewall-whitelist. SS-порт при
   этом **НЕ** открывается всему интернету — только этот IP через `na_filter`.
2. Спрашивает **имя пользователя** панели. Проверяет уникальность
   (`GET /api/users/by-username/<имя>`): если юзер есть — берёт его `ssPassword`
   как секрет моста; если нет — генерит новый секрет и создаёт юзера
   (безлимит, срок ~99 лет, `ACTIVE`) в том же Internal Squad, что и нода.
3. Добавляет SS-inbound в конфиг-профиль и привязывает его **только к ноде** (не к
   Host — SS это релей нода↔нода, не запись подписки).
4. В конце печатает **готовый Xray-конфиг входной ноды**: свежая пара Reality +
   split-туннель (RU/CDN/dev → DIRECT, AI + всё остальное → SS на этот выход).
   Сохраняется в `/opt/remnawave-node/state/entry-node.json` (chmod 600).

```bash
sudo bash remnawave-node.sh \
  --domain ads.example.com --panel-url https://p.example.com --panel-token-file /root/.rw \
  --acme-email a@b.com --country DE --whitelist <IP_ПАНЕЛИ> \
  --bridge --bridge-entry-ip <IP_ВХОДНОЙ_НОДЫ> \
  --bridge-user cascade-de --entry-domain cache.entry.ru
```

Секрет моста (SS-пароль) — общий для обеих нод. Он берётся из `ssPassword` юзера
панели, так что одна нода и одна учётка описывают весь канал. `--dry-run` показывает
SS-inbound в превью конфига и планируемые шаги, ничего не меняя.

## Management CLI (`remnanode`)

Установщик кладёт команду `remnanode` в `/usr/local/bin` для обслуживания ноды
после установки (без аргумента — интерактивное меню):

```
remnanode status              # контейнеры, режим, сокет/серт, активная проба :443, UUID панели
remnanode logs [node|nginx] [-f]
remnanode up | down | restart
remnanode template [id|name]  # список или смена заглушки (fetch + мутация + reload)
remnanode renew               # форс-продление сертификата
remnanode uninstall           # снести контейнеры + файлы (ресурсы панели не трогаются)
remnanode menu                # интерактивное меню (по умолчанию)
```

`remnanode status` делает реальную активную пробу — обычный TLS-клиент на публичный
`:443` — и показывает `HTTP 200`, когда заглушка отдаётся через всю цепочку
Reality→fallback→nginx.

## Обслуживание

- **logrotate** (`/etc/logrotate.d/remnawave-node`): ежедневная ротация, хранить 7,
  сжатие, `copytruncate` — для логов ноды и nginx.
- **Watchdog авто-рестарта** (cron, каждые 5 мин): `restart: always` покрывает
  краши; watchdog дополнительно `docker start`-ит контейнер, застрявший в
  non-running состоянии, которое compose сам не чинит.

## Раскладка на диске

```
/opt/remnanode/            контейнер ноды (docker-compose.yml, .env — mode 600; /dev/shm в socket-режиме)
/opt/nginx-selfsteal/      nginx selfsteal (compose, nginx.conf, conf.d, ssl, html, acme-renew.sh)
/opt/remnawave-node/state/ node.json (UUID, ключи), stages (маркеры resume), config.env (CLI), watchdog.sh, install-report-*.log (пишется только если установка завершилась с warning'ами)
/opt/remnawave-node/cache/ кэш установщика node-accelerator (для offline / повторов при плохой сети)
/usr/local/bin/remnanode   management CLI
/etc/logrotate.d/remnawave-node
/root/.acme.sh/            acme.sh + сертификаты
```

## Повторный запуск (идемпотентно)

Безопасно запускать снова на том же сервере/панели. Установщик находит config-profile
по имени, ноду по имени/адресу, host по remark+адресу: существующие ресурсы
**обновляются на месте** (без дублей), а имеющиеся Reality-ключи переиспользуются,
чтобы текущие подписки продолжали работать. Ещё валидный сертификат (>30 дней) не
перевыпускается.

Три имени спрашиваются отдельно, потому что панель валидирует их по-разному:

| Промпт / флаг     | К чему относится  | Разрешённые символы |
|-------------------|-------------------|---------------------|
| `--node-name`     | нода в панели     | свободно (скобки, эмодзи ок) |
| `--host-remark`   | host подписки     | свободно (эмодзи ок), label ~40 симв. |
| `--profile-name`  | config-profile    | только буквы, цифры, `_`, `-`, пробел |

## Диагностика (установка)

| Симптом | Причина / решение |
|---|---|
| `--dry-run` падает с `Required command not found: jq` | исправлено — dry-run больше не требует `jq`, лишь пропускает превью JSON конфига. Обновите скрипт. |
| Токен виден в `ps -eo cmd` | используйте `--panel-token-file` / `REMNAWAVE_PANEL_TOKEN_FILE` / `REMNAWAVE_PANEL_TOKEN` вместо `--panel-token`, затем **ротируйте** засвеченный токен. |
| `Failed to fetch node-accelerator` / таймаут `raw.githubusercontent.com` | происходит **до** создания ресурсов панели. Предзагрузите `--node-accelerator-dir <dir>` или `--node-accelerator-tar <file>`, переопределите `--node-accelerator-url`, либо `--skip-firewall`. Ранее закэшированная копия в `/opt/remnawave-node/cache` переиспользуется автоматически. |
| `kex_exchange_identification: Connection closed by remote host` сразу после установки | обычно временный per-source penalty OpenSSH / cooldown fail2ban после изменений firewall, **не** сломанная нода. Подождите 30–60 с и повторите SSH один-два раза, прежде чем разбирать fail2ban/nftables. |
| `protect` виснет на «CrowdSec APT repository» | фаза ограничена `--crowdsec-timeout` (180с); `--skip-crowdsec` отключает CrowdSec полностью. После `protect` установщик **проверяет, что `nft list table inet na_filter` реально активна, и снимает safety-таймер** node-accelerator перед запуском ноды — если таблица не активна, установка останавливается (нода `remnanode` **не** стартует), а этап остаётся невыполненным, чтобы `--resume` его повторил. Дополнительно IP панели best-effort дублируются в сет `na_nodeport_wl_*` (не фатально: панель и так проходит по общему whitelist). |
| `curl: (7) Failed to connect … :443`, а в `ss -ltnp` есть только `:2222` | Это ещё не Reality/XHTTP: нода не получила профиль от панели, поэтому Xray не создал inbound. Проверьте существование Node в панели и что исходящий IP панели есть в whitelist: `nft list set inet na_filter whitelist_v4` (общий whitelist стоит выше правил node-порта и уже даёт панели доступ к `:2222`; пустой `na_nodeport_wl_*` — норма). Установщик дополнительно передаёт IP панели как `NODE_PORT_PEERS`, чтобы отдельный node-port-сет тоже был заполнен. |
| В отчёте установки есть UUID Node, но `GET /api/nodes/<uuid>` возвращает `Node not found` | Node была удалена уже после успешного `POST`; профиль и Hosts могут при этом остаться. Нода на сервере не может определить автора удаления — проверьте audit/логи панели. После устранения доступа панели к `:2222` безопасно запустите `--resume --adopt-profile`: Node будет создана снова, а существующие Hosts обновятся без дублей. |
| `--resume` с `--transport both` завершается на `compute_inbounds` | Исправлено: когда `XHTTP_PORT` уже есть в сохранённом `TCP_PORTS`, прежняя проверка возвращала ложный код ошибки. Обновите скрипт и повторите `--resume`. |
| Reality на `:443` работает, но XHTTP не подключается | Проверьте итоговую строку `Xray inbound listener(s) present`: при `both` в ней обязаны быть `:443` и `:<xhttp-port>`. Затем в панели проверьте XHTTP-Host: тот же Node, `h2`, путь `/api/v1/update` и `xhttpExtraParams`; обновлённый установщик создаёт это автоматически. |
| `optimize` долгий / не включил BBR | `optimize` ставит ядро XanMod + BBRv3 — законно долго. Идёт **первым** (до провижена ноды), **без лимита по умолчанию**, best-effort — остальное не прерывает. Если не доехал (или ты задал `--optimize-timeout` и его убили) — запусти вручную: `sudo bash /opt/remnawave-node/cache/node-accelerator-*/install.sh optimize` (после прерванного apt сначала `sudo dpkg --configure -a`). Финальный **verify печатает реальный congestion control (bbr/cubic), qdisc и ядро** — если `cubic`, BBR ещё не активен. Строка `CrowdSec bouncer not active` при `--skip-crowdsec` **ожидаема и безвредна**. |
| Таймаут скачивания `geosite.dat` | исправлено — geo качается с ретраями, докачкой (`curl -C -`), бюджетом `--geo-timeout` (600с) и никогда не монтирует частичный файл; сбой **никогда не фатален** (Xray оставляет встроенные geo-данные), `--resume` повторит geo. |
| Прервано после создания ресурсов панели | перезапустите с `--resume` — переиспользует Config Profile / Node / Host и ключи Reality, пропускает выполненные стадии. Дублей в панели не будет. |

Финальная проверка намеренно разделяет два уровня: ответ заглушки на `:443` доказывает
цепочку Reality → fallback → nginx, а проверка **каждого** выбранного inbound ловит
частичный сбой режима `both`, когда `:443` уже доступен, но XHTTP ещё не применился.

## Диагностика (`grpc-tls`)

`/health` отдаёт 200, но gRPC-туннель не подключается:

```bash
ss -lntp | grep ':<grpc-port>'          # gRPC-инбаунд Xray должен слушать 127.0.0.1:<grpc-port>
docker logs remnanode --tail 100        # ошибки Xray
docker logs nginx-selfsteal --tail 100  # error-лог nginx (сбои grpc_pass)
```

Проверь по порядку:

- `serviceName` совпадает **во всех трёх местах**: nginx `location /<name>/Tun`,
  inbound конфиг-профиля (`grpcSettings.serviceName`), Host/ссылка клиента.
- Host **Address / SNI / Host** — это **домен** (не origin-IP).
- Host **ALPN** = `h2,http/1.1`.
- Config-profile **активен на ноде** (`remnanode status` → активные inbound).
- Inbound **включён для пользователей через Internal Squad** (см. выше) — самая
  частая причина «host есть, а пользователям ничего».

## Заметки

- `SECRET_KEY` передаётся ноде через `.env` (mode 600), не через список процессов.
- API-токен панели читается молча при вводе (не эхоится).
- Скрипт падает громко на любом не-2xx ответе API панели, а не продолжает.
- Генерируемый Xray-конфиг проверяется через `xray -test` до отправки в панель
  (пропустить: `--skip-xray-validate`).

## Лицензия

MIT.
