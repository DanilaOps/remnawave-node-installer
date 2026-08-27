# План доведения Ansible-установщика до продакшена

Статус на 2026-08-27. Ветка `feature/ansible-automation` (не запушена).
Коммиты: `4249451` — незакоммиченные правки bash-инсталлятора + QA-workflow + .gitignore;
`d3bcbb6` — вся работа Codex по Ansible (97 файлов).

Принцип: сначала паритет с bash-скриптом, потом операционные плейбуки, потом
реестр нод и DNS, и только затем тест на живой ноде. Terraform не используется,
VPS создаются руками, IP попадает в реестр вручную.

---

## Этап 1. Паритет с bash-инсталлятором

Порядок внутри этапа — по влиянию на маскировку и безопасность.

### 1.1. Decoy-шаблоны: скачивание + рандомизация (приоритет №1)

**В bash:** `fetch_template` (codeload tarball, fallback на git sparse-checkout),
`randomize_template` (удаление provenance-файлов, случайный brand/title/description,
hue-rotate CSS, удаление google-fonts, нейтрализация beacon `api.ipify.org`,
подмена favicon, cache-busting `?v=`, байтовый шум в css/js), `setup_decoy_content`
с громким предупреждением при откате на stub.

**В Ansible:** только статичный `index.html.j2`. Все ноды отдают
байт-в-байт одинаковую страницу — это хуже, чем в bash: по хешу страницы
парк нод связывается в один кластер.

**Делаем:**
- переменные `selfsteal_template` (папка в `sni-templates`), `selfsteal_template_repo`,
  `selfsteal_randomize` (default `true`);
- скачивание архива на **контроллер** (`get_url` + `unarchive` + кэш по ref), а не на ноду;
- мутатор — отдельный Python-модуль/filter в `filter_plugins`, не цепочка `sed` в `shell`;
- **seed рандомизации = детерминированный** (`hash(node_id + template)`), иначе каждый
  прогон меняет сайт и ломает идемпотентность. Ротация — только по явной переменной;
- развёртывание файлов + маркер `state/selfsteal-template.json` (шаблон, ref, seed);
- пересборка только при смене шаблона/seed или `selfsteal_refresh_content=true`;
- fallback на встроенный `index.html.j2` — с `warn` в отчёте, как в bash.

**Тесты:** unit на мутатор (детерминированность при одном seed, различие при разных,
валидный HTML), `render_templates.yml`, проверка в `node_verify`, что публичная
страница не равна дефолтному stub.

### 1.2. Front-gate (`--front-ip`)

**В bash:** `apply_front_gate` — таблица `inet mirror_gate` (priority -5): `lo` accept,
443 established accept, 443 от списка фронтов accept, остальной новый 443 — drop;
+ `mirror-gate.service` для persist.

**В Ansible:** отсутствует.

**Делаем:** опциональный блок в `node_base/tasks/firewall.yml`, **своя таблица**
(не смешивать с `remnawave_filter`), переменная `front_ips: []`, обязательный `nft -c`,
включение в существующий rollback-таймер (иначе кривой front-gate отрежет 443 навсегда),
persist через тот же `remnawave-firewall.service`.

**Проверки в `node_verify`:** 443 отвечает с адреса фронта и не отвечает с чужого
(использовать тот же механизм, что уже сделан для `NODE_PORT`).

### 1.3. RKN/DPI hardening — доделать

**Уже есть в `node_base/tasks/hardening.yml`:** sysctl (`tcp_rfc1337`, syncookies,
redirects, `rp_filter`, BBR+fq), отключение dccp/sctp/rds/tipc, fail2ban, sshd-конфиг.

**Нет:**
- нормализация TTL/hoplimit=128 в postrouting mangle (в bash — таблица `inet rknnode`);
- подавление SSH-баннера (`DebianBanner no`, `Banner none`);
- `icmp_echo_ignore_broadcasts`, `icmp_ignore_bogus_error_responses`, `accept_source_route=0`.

**Делаем:** флаг `node_rkn_hardening_enabled`, отдельная nft-таблица с `nft -c` и
systemd-unit, доп. ключи sysctl, проверка баннера в `node_verify`.

**Риск:** TTL=128 ставится в postrouting **после** Docker NAT. Проверить на Molecule и
на живой ноде, что не ломается трафик контейнеров и что правило не дублируется при
повторном прогоне (в bash для этого create-then-delete идиома).

### 1.4. node-accelerator / CrowdSec — переносим эффект, а не вызов

**В bash:** `na_prepare`/`na_run` скачивают внешний `install.sh` (тарболл, кэш, offline-режимы),
запускают `protect` (таблица `na_filter`, CrowdSec, safety-timer) и `optimize`, затем
`verify_na_firewall_active`, `sync_na_node_port_allowlist`, `disarm_na_safety_timer`.

**Архитектурное решение:** внешний bash-инсталлятор из Ansible **не вызываем** —
это запрещено ТЗ и создаёт двух владельцев firewall. Своя таблица `remnawave_filter`
с `nft -c` и таймером откатa уже функционально заменяет `na_filter` и safety-timer.

**Делаем:**
- `node_base/tasks/crowdsec.yml`: пакет CrowdSec + `crowdsec-firewall-bouncer-nftables`,
  коллекции, whitelist для `management_cidrs` и `remnawave_panel_cidrs`
  (иначе бан контроллера/панели), флаг `node_crowdsec_enabled: false` по умолчанию —
  включаем после теста;
- сверить sysctl-tuning из `optimize` с текущим `node_sysctl` и добавить недостающее
  (`somaxconn`, `netdev_max_backlog`, `nf_conntrack_max`, `fs.file-max`, лимиты nofile);
- зафиксировать в `ansible/README.md`, что `na_filter` больше не используется, чтобы
  на ноде не оказалось двух наборов правил после миграции с bash.

### 1.5. Torrent Blocker (панельный node-plugin)

**В bash:** `setup_torrent_blocker` — находит/создаёт node-plugin, **копирует текущий
`pluginConfig` ноды перед переключением**, мержит `.torrentBlocker`
(`enabled`, `blockDuration`, `ignoreLists.ip/userId`, `includeRuleTags`), PATCH,
привязка к ноде, затем перечитывает `activePluginUuid` и падает при расхождении.

**В Ansible:** `remnawave_node/tasks/plugins.yml` умеет generic-CRUD node-plugin, но:
- PATCH перезаписывает `pluginConfig` целиком → теряются `webhookUrl`, `rulePlacement`
  и прочие поля, которых нет в объявлении;
- нет копирования текущего профиля ноды перед подменой;
- нет пресета torrent-blocker и валидации его полей;
- жёсткое требование ровно одного плагина.

**Делаем:** структура `torrent_blocker` (enabled, block_duration, ignore_ips,
ignore_user_ids, rule_tags) → рендер в `plugin_config`; merge-семантика поверх
существующего конфига; копирование текущего профиля при первом создании; assert на
типы и на `kernel >= 5.7` (уже есть); тесты в mock-панели, включая повторный прогон
без изменений и сохранение чужих полей.

### 1.6. XHTTP / gRPC-TLS — реализованы, нужна обвязка

**Факт (проверено):** `remnawave_panel/templates/xray-config.json.j2` уже поддерживает
`raw`, `xhttp` (packet-up, path, extra) и `grpc-tls` (listen 127.0.0.1, security none),
`nginx.conf.j2` уже делает `grpc_pass` на loopback-инбаунд, socket-режим есть.

**Чего нет:** примеров в inventory; валидации портов (в bash — `xhttp_port_reserved`:
запрет коллизий с 80/443/NODE_PORT/selfsteal/SSH/45876); проверок в `node_verify`
(xhttp-path отвечает, gRPC-listener только на loopback, 443 отдаёт валидный
сертификат); рендер-тестов для этих сетей; описания режима `both`.

**Делаем:** assert-блок в `remnawave_panel/tasks/main.yml`, примеры `inbound_specs` и
`host_specs` для трёх режимов, ветки в `node_verify`, три сценария в `render_templates.yml`.

### 1.7. august-routing-updater → роль

**Сейчас:** отдельный Python (`august_routing_updater.py`, `august_routing_bootstrap.py`),
systemd service+timer, `.env`, живёт вне репозитория установщика. По README: тянет
JSONSUB-профили RoscomVPN (HAPP/INCY), проверяет `geoip.dat`/`geosite.dat`, добавляет
HTTP-заголовок `routing` по User-Agent, сохраняет чужие Response Rules, при ошибке
ничего не меняет.

**Вопрос перед реализацией:** он работает с **панелью** (Response Rules) — значит
таймер должен крутиться на хосте панели или на контроллере, а не на каждой ноде.
Нужно подтвердить, где он сейчас запущен.

**Делаем:** деплой скрипта + unit + timer + `.env` (секреты через vault) шаблонами;
идемпотентность (не трогать чужие правила); `--check`-совместимость; отдельный тег,
чтобы не гонять при каждом прогоне ноды.

### 1.8. install-mirror.sh (SNI-mirror front) → отдельная роль

**В bash:** отдельный интерактивный установщик: nginx `stream` + `ssl_preread`,
свой SNI → backend:443 сырым TCP, неизвестный/пустой SNI → reset на закрытый порт,
опциональный прозрачный форвард (open relay) только по явному согласию с
egress-защитой от приватных сетей, транзакционный `include` с валидацией и откатом,
per-IP лимит соединений.

**Делаем:** роль `sni_mirror` + `playbooks/install_mirror.yml` + группа inventory
`sni_mirror_fronts`. Open relay по умолчанию выключен.

**Связка:** фронт знает `backend_ip`/`sni`, бэкенд пускает 443 только с `front_ips`
(п. 1.2). Обе переменные обязаны выводиться из одной записи реестра (п. 3), иначе
при замене фронта нода станет недоступной.

---

## Этап 2. Операционные плейбуки

Сейчас есть только `install_node.yml`. Добавляем:

| Плейбук | Назначение |
|---|---|
| `bootstrap.yml` | свежая VPS: root+пароль → `deployer` с ключом и NOPASSWD sudo, python3, `known_hosts`, отключение парольного входа. Снимает первый ручной шаг |
| `update_node.yml` | обновление образа RemnaNode/Xray/geodata, `serial: 1`, с проверкой после каждой ноды |
| `rotate_keys.yml` | явная и раздельная ротация Reality-ключей, `SECRET_KEY`, bridge-секрета |
| `replace_node.yml` | замена при блокировке: новая нода → установка → перенос `host_specs` → старая в Degraded/decommission |
| `decommission.yml` | снять Hosts, удалить Node из панели, погасить сервисы на ноде |
| `healthcheck.yml` | только `node_verify`, для cron/CI по всему парку |
| `uninstall.yml` | паритет с `cmd_uninstall` из bash-CLI |

`bootstrap.yml` делаем первым в этапе — без него любой тест начинается с ручного SSH.

---

## Этап 3. Реестр нод вместо Terraform

Один файл `nodes.yml` — источник истины. На ноду задаём только то, что действительно
уникально: `node_id`, страна, провайдер, регион, публичный IP, домен, роль,
front/bridge-связи. Всё остальное **выводится**: `node_name` (счётчик по стране —
логика `panel_next_sequence` из bash, но номер берём из реестра, а не из панели, чтобы
имя не зависело от порядка запуска), `profile_name`, тег инбаунда, `host_specs`,
`selfsteal_virtual_hosts`, пути.

Реализация: генератор inventory (скрипт) либо `constructed`-плагин. Сейчас на ноду
копируется ~40 строк YAML, из которых уникальны 6 — это главный источник будущих
опечаток и расхождений между нодами.

---

## Этап 4. DNS

**reg.ru API есть:** база `https://api.reg.ru/api/regru2`, методы группы `zone/*`
(`zone/add_alias` — A-запись, `zone/add_aaaa`, `zone/add_txt`, `zone/remove_record`,
`zone/get_resource_records`), авторизация логином и паролем аккаунта (либо отдельным
API-паролем). Требуется включить доступ к API в личном кабинете и внести IP
контроллера в белый список. Точные имена методов и лимиты сверю с их документацией
на этапе реализации; пароль — только в vault, все задачи с `no_log: true`.

**Делаем:** роль `dns` с абстракцией провайдера (`dns_provider: regru|cloudflare|none`)
и одной идемпотентной задачей «A-запись домена = `node_public_ip`»
(`get_resource_records` → сравнение → add/remove), запускаемой **до** preflight.

**Рекомендация:** делегировать зоны на NS Cloudflare (одноразовое действие в кабинете
reg.ru, домены остаются у reg.ru). Плюсы: нормальный API с токенами вместо пароля
аккаунта, `cf-dns` ACME (сертификат без открытия 80 и wildcard), низкий TTL для
быстрой замены нод. Тогда `regru`-часть нужна только как одноразовая утилита.

---

## Этап 5. Тест на живой ноде

1. Зелёные `yamllint` / `ansible-lint` / unit / mock-панель / Molecule (сейчас CI не гонялся).
2. Тестовая VPS + тестовая панель по `TESTING.ru.md` (15 разделов: preflight,
   восстановление после прерванной установки, два строгих прогона с `changed=0`,
   VPN E2E, bridge, проверка логов на утечку секретов).
3. Правки по результатам → прод.

До этого шага ни одна строка Ansible не проверялась на реальной системе — статические
тесты не ловят ни расхождения с API панели, ни поведение firewall и ACME.

---

## Нужны решения от тебя

1. **august-routing-updater** — где сейчас крутится (панель, отдельный хост, нода) и
   должен ли он раскатываться этим же прогоном?
2. **CrowdSec** — нужен на нодах или отказываемся (своя nft-таблица уже есть)?
3. **Front-gate + SNI-mirror** — это прод-схема или эксперимент? Если прод — сколько
   фронтов на бэкенд?
4. **Панель** — одна на всё или несколько?
5. **DNS** — делегируем зоны на Cloudflare или пишем полноценную regru-роль?
6. **Запуск** — с ноутбука (`ansible-playbook` + vault-пароль), GitHub Actions или
   Semaphore/AWX?
