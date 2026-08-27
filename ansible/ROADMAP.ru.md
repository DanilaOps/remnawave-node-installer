# План доведения Ansible-установщика до продакшена

Обновлено 2026-08-27 после уточнений. Ветка `feature/ansible-automation` (не запушена).

## Вводные

- Terraform не используется, VPS создаются руками, IP вносится в реестр вручную.
- Панель Remnawave — **одна**.
- Все домены на **reg.ru**.
- Основной протокол — **VLESS + TCP/raw + Reality + Vision**. Критично: выпущенный
  сертификат на домен и правдоподобный сайт-заглушка, за которым идёт маскировка.
- SNI-mirror и front-gate в прод-схему пока не входят → бэклог.
- Запуск с устройства (`ansible-playbook` + vault-пароль).

---

## Этап 1. То, за чем прячемся: сертификат и заглушка

### 1.1. Decoy-шаблоны: скачивание + рандомизация (приоритет №1)

**В bash:** `fetch_template` (codeload tarball, fallback на git sparse-checkout),
`randomize_template` (удаление provenance-файлов, случайный brand/title/description,
hue-rotate CSS, удаление google-fonts, нейтрализация beacon `api.ipify.org`, подмена
favicon, cache-busting `?v=`, байтовый шум в css/js), `setup_decoy_content` с громким
предупреждением при откате на stub.

**В Ansible:** только статичный `index.html.j2`. Все ноды отдают байт-в-байт
одинаковую страницу — по хешу страницы весь парк связывается в один кластер, а сама
страница не похожа на настоящий сайт. Для схемы, где маскировка — основа, это главный
недостаток текущего кода.

**Делаем:**
- переменные `selfsteal_template` (папка в `sni-templates`), `selfsteal_template_repo`,
  `selfsteal_template_ref`, `selfsteal_randomize` (default `true`);
- скачивание архива на **контроллер** (`get_url` + `unarchive` + кэш по ref), не на ноду;
- мутатор — Python-модуль/filter в `filter_plugins`, не цепочка `sed` в `shell`;
- **seed рандомизации детерминированный** (`hash(node_id + template + ref)`), иначе
  каждый прогон меняет сайт и ломает идемпотентность. Смена — только явной переменной;
- маркер `state/selfsteal-template.json` (шаблон, ref, seed) → пересборка только при
  смене входных данных или `selfsteal_refresh_content=true`;
- fallback на встроенный `index.html.j2` с предупреждением в отчёте, как в bash.

**Тесты:** unit на мутатор (детерминированность при одном seed, различие при разных,
валидность HTML), `render_templates.yml`, проверка в `node_verify`, что публичная
страница не равна встроенному stub.

### 1.2. Аудит сертификатной цепочки

Прочитал `roles/remnawave_node/tasks/certificate.yml`. Логика в целом верная
(проверка срока через `x509_certificate_info`, `block`/`always` с восстановлением
nginx, `--keep-until-expiring`), но есть три конкретных проблемы:

1. **Используется `certbot --standalone` с остановкой nginx**, хотя вся обвязка для
   webroot уже есть: в `nginx.conf.j2` отдаётся
   `location ^~ /.well-known/acme-challenge/`, каталог `acme-webroot` создаётся.
   → Переходим на `--webroot -w`: нет остановки nginx, нет окна, в котором сайт-заглушка
   не отвечает (а именно в этот момент нода наиболее заметна), проще таймер обновления.
2. **Нет режима ACME staging.** При отладке на живой VPS легко упереться в лимиты
   Let's Encrypt (5 неудач в час, 50 сертификатов в неделю на домен). → Переменная
   `certificate_acme_directory`, в `inventories/test` по умолчанию staging.
3. **`certificate_mode: cloudflare_dns` бесполезен**, пока зоны на reg.ru. Оставляем
   как есть, но в документации фиксируем: рабочий режим — `http01`. Полноценный DNS-01
   для reg.ru потребует не certbot (плагина под reg.ru нет), а acme.sh (`dns_regru`)
   или manual-hook. Нужен только под wildcard — сейчас не требуется.

### 1.3. RKN/DPI hardening — доделать

**Уже есть:** sysctl (`tcp_rfc1337`, syncookies, redirects, `rp_filter`, BBR+fq),
отключение dccp/sctp/rds/tipc, fail2ban, sshd-конфиг.

**Нет:** нормализация TTL/hoplimit=128 в postrouting mangle (в bash — таблица
`inet rknnode`); подавление SSH-баннера (`DebianBanner no`, `Banner none`);
`icmp_echo_ignore_broadcasts`, `icmp_ignore_bogus_error_responses`,
`accept_source_route=0`.

**Делаем:** флаг `node_rkn_hardening_enabled`, отдельная nft-таблица с `nft -c` и
systemd-unit, доп. ключи sysctl, проверка баннера в `node_verify`.

**Риск:** TTL=128 ставится в postrouting **после** Docker NAT. Проверить на Molecule и
на живой ноде, что не ломается трафик контейнеров и что правило не дублируется при
повторном прогоне (в bash для этого create-then-delete идиома).

### 1.4. Torrent Blocker (панельный node-plugin)

**В bash:** `setup_torrent_blocker` находит/создаёт node-plugin, **копирует текущий
`pluginConfig` ноды перед переключением**, мержит `.torrentBlocker`
(`enabled`, `blockDuration`, `ignoreLists.ip/userId`, `includeRuleTags`), PATCH,
привязка к ноде, затем перечитывает `activePluginUuid` и падает при расхождении.

**В Ansible:** generic-CRUD node-plugin есть, но PATCH перезаписывает `pluginConfig`
целиком → теряются `webhookUrl`, `rulePlacement` и прочие поля, которых нет в
объявлении; нет копирования текущего профиля; нет пресета и валидации; жёсткое
требование ровно одного плагина.

**Делаем:** структура `torrent_blocker` → рендер в `plugin_config`; merge поверх
существующего конфига; копирование текущего профиля при первом создании; assert на
типы и `kernel >= 5.7` (уже есть); тесты в mock-панели на повторный прогон и сохранение
чужих полей.

### 1.5. XHTTP / gRPC-TLS — реализованы, нужна обвязка

**Факт (проверено):** `xray-config.json.j2` поддерживает `raw`, `xhttp` (packet-up,
path, extra) и `grpc-tls` (listen 127.0.0.1, security none); `nginx.conf.j2` делает
`grpc_pass`; socket-режим есть.

**Нет:** валидации портов (в bash `xhttp_port_reserved` запрещает коллизии с
80/443/NODE_PORT/selfsteal/SSH/45876); проверок в `node_verify`; примеров в inventory;
рендер-тестов. Основной протокол — raw/Reality, поэтому это дешёвая страховка, а не
приоритет: делаем после 1.1–1.4.

### 1.6. Sysctl/лимиты из `optimize`

Сверить с текущим `node_sysctl` и добавить недостающее: `somaxconn`,
`netdev_max_backlog`, `nf_conntrack_max`, `fs.file-max`, `nofile` для docker-сервиса.

---

## Этап 2. Операционные плейбуки

Сейчас есть только `install_node.yml`.

| Плейбук | Назначение |
|---|---|
| `bootstrap.yml` | свежая VPS: root+пароль → `deployer` с ключом и NOPASSWD sudo, python3, `known_hosts`, отключение парольного входа. **Делаем первым** — без него любой тест начинается с ручного SSH |
| `update_node.yml` | обновление образа RemnaNode/Xray/geodata, `serial: 1`, проверка после каждой ноды |
| `rotate_keys.yml` | явная и раздельная ротация Reality-ключей, `SECRET_KEY`, bridge-секрета |
| `replace_node.yml` | замена при блокировке: новая нода → установка → перенос `host_specs` → старая в decommission |
| `decommission.yml` | снять Hosts, удалить Node из панели, погасить сервисы |
| `healthcheck.yml` | только `node_verify`, для регулярного прогона по парку |
| `uninstall.yml` | паритет с `cmd_uninstall` из bash-CLI |

---

## Этап 3. Реестр нод вместо Terraform

Один файл `nodes.yml` — источник истины. На ноду задаём только уникальное: `node_id`,
страна, провайдер, регион, публичный IP, домен, роль. Выводим: `node_name` (счётчик по
стране — логика `panel_next_sequence` из bash; панель одна, но номер всё равно берём из
реестра, чтобы имя не зависело от порядка запуска), `profile_name`, тег инбаунда,
`host_specs`, `selfsteal_virtual_hosts`.

Панель одна → `remnawave_panel_url`/`remnawave_panel_token` переезжают в
`group_vars/all`, из `remnawave_nodes.yml` уходят.

Сейчас на ноду копируется ~40 строк YAML, из которых уникальны 6.

---

## Этап 4. DNS: reg.ru

API: база `https://api.reg.ru/api/regru2`, методы группы `zone/*` —
`zone/add_alias` (A-запись), `zone/add_aaaa`, `zone/add_txt`, `zone/remove_record`,
`zone/get_resource_records`. Авторизация логином и паролем аккаунта (либо отдельным
API-паролем); нужно включить доступ к API в личном кабинете и внести IP контроллера в
белый список. Точные имена методов и лимиты сверяем с документацией при реализации.

**Делаем:** роль `dns` с абстракцией `dns_provider: regru|none` и одной идемпотентной
задачей «A-запись домена = `node_public_ip`» (`get_resource_records` → сравнение →
`add_alias`/`remove_record`), запуск **до** preflight (сейчас preflight падает, если
записи нет). Пароль только в vault, все задачи `no_log: true`.

Сертификаты остаются на `http01` — reg.ru для ACME не нужен.

---

## Этап 5. august-routing-updater — отдельный плейбук, не часть установки ноды

Разобрал: updater общается **только с API панели** (Response Rules / заголовок
`routing` по User-Agent), тянет JSONSUB-профили RoscomVPN для HAPP и INCY
(`hydraponique/roscomvpn-routing`), проверяет структуру и доступность
`geoip.dat`/`geosite.dat`, сохраняет чужие Response Rules, при ошибке не меняет ничего.
Таймер — раз в сутки 04:20 UTC с разбросом 2 часа. Крутится на хосте subscription page.

**Вывод:** к нодам он отношения не имеет и в `install_node.yml` попадать не должен —
иначе N нод станут N ежедневными писателями в один и тот же ресурс панели.

**Делаем:** роль `routing_updater` + `playbooks/routing_updater.yml`, группа inventory
`subscription_page` (один хост). Шаблонами: `/opt/august-routing-updater/*.py`, unit,
timer, `/etc/august-routing-updater.env`, токен в отдельном файле `0600` из vault.
Сохранить текущий hardening unit'а (`ProtectSystem=strict`, `NoNewPrivileges`,
`UMask=0077`, `ReadOnlyPaths`). Прогон `--check`-режима скрипта в `node_verify`-стиле:
после установки один раз запустить без `--apply` и убедиться, что профили валидны.

---

## Этап 6. Тест на живой ноде

1. Зелёные `yamllint` / `ansible-lint` / unit / mock-панель / Molecule — сейчас CI ни
   разу не гонялся.
2. Тестовая VPS + тестовая панель по `TESTING.ru.md` (15 разделов: preflight,
   восстановление после прерванной установки, два строгих прогона с `changed=0`,
   VPN E2E, bridge, проверка логов на утечку секретов). ACME — staging (п. 1.2).
3. Правки по результатам → прод.

До этого шага ни одна строка Ansible не проверялась на реальной системе.

---

## Бэклог (не входит в текущий объём)

- **Front-gate (`--front-ip`)** — таблица `inet mirror_gate`, 443 только с адресов фронта.
- **Роль `sni_mirror`** (`install-mirror.sh`) — nginx stream + `ssl_preread`, свой SNI →
  backend:443, неизвестный SNI → reset, open relay только по явному флагу. Обе связки
  (`front_ips` на бэкенде и `backend_ip`/`sni` на фронте) обязаны выводиться из одной
  записи реестра, иначе при замене фронта нода станет недоступной.
- **CrowdSec** — см. решение ниже: не ставим по умолчанию.
- **`node-accelerator` как внешний вызов** — не портируем. Своя таблица
  `remnawave_filter` с `nft -c` и таймером откатa функционально заменяет `na_filter` и
  safety-timer; запуск чужого bash-инсталлятора из Ansible запрещён ТЗ и создаёт двух
  владельцев firewall. В README зафиксировать, что на нодах, миграированных с bash,
  `na_filter` нужно снять, иначе будет два набора правил.
