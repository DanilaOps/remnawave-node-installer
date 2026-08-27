# План доведения Ansible-установщика до продакшена

Обновлено 2026-08-27. Ветка `feature/ansible-automation` (не запушена).

## Вводные

- Terraform не используется, VPS создаются руками, IP вносится в inventory вручную.
- Панель Remnawave — одна. Общий Xray-профиль — `Default August`, в нём routing.
- Все домены на reg.ru.
- Основной протокол — VLESS + TCP/raw + Reality + Vision. Критично: сертификат на домен
  и правдоподобный сайт-заглушка, за которым идёт маскировка.
- SNI-mirror и front-gate в прод-схему не входят → бэклог.
- Запуск с устройства сейчас, Semaphore UI следующим этапом. Абсолютных путей в проекте
  нет, поэтому переезд — это только конфигурация.
- Модель безопасности ноды: nftables со своей изолированной таблицей и таймером откатa,
  `management_cidrs`, fail2ban и минимум открытых портов. Ничего поверх этого не ставим.

---

## Сделано

**Сертификаты без простоя.** Certbot переведён с `--standalone` на `--webroot`: HTTP-01
отвечает из каталога, который nginx уже отдаёт на `/.well-known/acme-challenge/`.
Standalone остаётся только для самой первой установки, когда nginx ещё не запущен и
прерывать нечего. Таймер обновления больше не останавливает nginx и перезагружает его
только когда сертификат реально изменился.

**ACME staging/production.** `certificate_acme_environment` выбирает CA. Переключение
между окружениями определяется по issuer сертификата и вызывает чистый перевыпуск, а
установка падает, если поставленный сертификат не соответствует запрошенному окружению —
staging-сертификат не может незаметно попасть в прод.

**Привязка Hosts к `Default August`.** Панельная роль больше не создаёт профиль на каждую
ноду. Она читает общий профиль, вливает в него инбаунд этой ноды по тегу и записывает
обратно: чужие инбаунды сохраняют свои UUID и свои Reality-ключи, `routing`, `dns`,
`outbounds` и `policy` переносятся без изменений. Reality-ключ ноды ищется по её
собственному тегу, поэтому нода не может подобрать ключ соседа. Node активирует только
свой инбаунд, каждый Host публикуется на UUID общего профиля и UUID своего инбаунда, и
после реконсиляции Hosts перечитываются с проверкой этой привязки. Отсутствующий общий
профиль не создаётся молча — прогон останавливается с объяснением.

**Идемпотентность.** Второй прогон панельной роли давал `changed=2` и до этого не
проверялся ни разу. Причины: тестовый плейбук перезаписывал `.env`, который роль пишет
сама; `activeInbounds` и `nodes` панель возвращает объектами, а сравнивались они со
строками UUID; `port` уходил в payload строкой. Исправлено, второй прогон даёт
`changed=0` и в обычном, и в bridge-сценарии.

**Bootstrap.** Роль `node_bootstrap` и `playbooks/bootstrap.yml`: доверие host key,
python3 через `raw`, создание `deployer` с ключом контроллера и sudo без пароля, затем
отдельный play, который заходит уже этим аккаунтом и проверяет, что escalation работает.
sshd не трогает — им владеет `node_base`.

**Минимум переменных на ноду.** На новую ноду в inventory нужны три факта:
`ansible_host`, `node_id`, и домен (если он не выводится из зоны). `node_name`,
`node_country`, `node_public_ip`, `selfsteal_domain`, тег инбаунда, `host_specs` и
`selfsteal_virtual_hosts` выводятся в `group_vars`. Отдельный файл-реестр `nodes.yml`
больше не нужен: `hosts.yml` и есть реестр.

**Реальные адреса убраны из git.** `inventories/test/`, `inventories/local/`,
`inventories/production/hosts.yml` и `production/group_vars/all/panel.yml` в `.gitignore`,
в git лежат `*.yml.example`. Пример не может называться `*.example.yml`: Ansible грузит
любой `.yml` из `group_vars`, и такой файл определял бы реальные переменные — на это есть
регрессионный тест.

**Тесты.** Общий профиль в mock-панели засеян чужим инбаундом и routing-правилами, тест
проверяет слияние, сохранность чужого ключа, привязку Host и Node и `changed=0` на втором
прогоне. Добавлены юнит-тесты новых фильтров и рендер всех трёх транспортов (raw, xhttp,
grpc-tls). `yamllint`, `ansible-lint` (профиль production), `validate_structure`,
`render_templates`, три панельных сценария — зелёные.

---

## Осталось

### 1. Decoy-шаблоны: скачивание и рандомизация (приоритет №1)

В bash: `fetch_template` (codeload tarball, fallback на git sparse-checkout) и
`randomize_template` (удаление provenance, случайный brand/title/description, hue-rotate,
удаление google-fonts, нейтрализация beacon `api.ipify.org`, favicon, cache-busting,
байтовый шум). В Ansible — только статичный `index.html.j2`: все ноды отдают
байт-в-байт одинаковую страницу, по её хешу парк связывается в один кластер.

Делаем: `selfsteal_template`/`selfsteal_template_repo`/`selfsteal_template_ref`/
`selfsteal_randomize`; скачивание на контроллер с кэшем по ref; мутатор отдельным
Python-модулем, а не цепочкой `sed`; **seed детерминированный** (`hash(node_id + template
+ ref)`), иначе каждый прогон меняет сайт и ломает идемпотентность; маркер
`state/selfsteal-template.json`; fallback на встроенный шаблон с предупреждением.

### 2. RKN/DPI hardening — доделать

Есть: sysctl (`tcp_rfc1337`, syncookies, redirects, `rp_filter`, BBR+fq), отключение
dccp/sctp/rds/tipc, fail2ban, sshd-политика с проверкой через `sshd -T`.

Нет: нормализация TTL/hoplimit=128 в postrouting mangle (в bash — таблица `inet rknnode`),
подавление SSH-баннера (`DebianBanner no`, `Banner none`), `icmp_echo_ignore_broadcasts`,
`icmp_ignore_bogus_error_responses`, `accept_source_route=0`.

Риск: TTL=128 ставится после Docker NAT — проверить на Molecule и на живой ноде, что не
ломается трафик контейнеров и правило не дублируется при повторном прогоне.

### 3. Torrent Blocker

В bash `setup_torrent_blocker` копирует текущий `pluginConfig` перед переключением и
мержит `.torrentBlocker`. В Ansible generic-CRUD node-plugin есть, но PATCH перезаписывает
`pluginConfig` целиком (теряются `webhookUrl`, `rulePlacement`) и нет копирования текущего
профиля. Делаем структуру `torrent_blocker` с merge-семантикой и тестом в mock-панели.

### 4. XHTTP / gRPC-TLS — обвязка

Шаблоны уже поддерживают `raw`, `xhttp` и `grpc-tls`, рендер всех трёх покрыт тестом.
Осталось: валидация портов (в bash `xhttp_port_reserved` запрещает коллизии с
80/443/NODE_PORT/selfsteal/SSH/45876), ветки в `node_verify`, примеры в inventory,
описание режима `both`.

### 5. Sysctl и лимиты из `optimize`

Сверить с текущим `node_sysctl` и добавить недостающее: `somaxconn`,
`netdev_max_backlog`, `nf_conntrack_max`, `fs.file-max`, `nofile` для docker.

### 6. Операционные плейбуки

| Плейбук | Назначение |
|---|---|
| `update_node.yml` | обновление образа RemnaNode/Xray/geodata, `serial: 1`, проверка после каждой ноды |
| `rotate_keys.yml` | явная и раздельная ротация Reality-ключей, `SECRET_KEY`, bridge-секрета |
| `replace_node.yml` | замена при блокировке: новая нода → установка → перенос `host_specs` → старая в decommission |
| `decommission.yml` | снять Hosts, удалить Node из панели, погасить сервисы |
| `healthcheck.yml` | только `node_verify`, регулярный прогон по парку |
| `uninstall.yml` | паритет с `cmd_uninstall` из bash-CLI |

`replace_node.yml` уже опирается на готовый механизм: `inbound_prune_tags` убирает из
общего профиля инбаунд, который нода больше не обслуживает.

### 7. DNS: reg.ru

API: `https://api.reg.ru/api/regru2`, методы `zone/*` — `zone/add_alias` (A-запись),
`zone/add_aaaa`, `zone/add_txt`, `zone/remove_record`, `zone/get_resource_records`.
Авторизация логином и паролем аккаунта (либо отдельным API-паролем); нужно включить API в
личном кабинете и внести IP контроллера в белый список. Имена методов и лимиты сверяем с
документацией при реализации.

Делаем роль `dns` (`dns_provider: regru|none`) с одной идемпотентной задачей «A-запись
домена = `node_public_ip`», запуск **до** preflight — сейчас preflight падает, если записи
нет. Пароль только в vault, задачи с `no_log: true`. Сертификаты остаются на `http01`,
для ACME reg.ru не нужен.

### 8. august-routing-updater — отдельный плейбук

Updater общается только с API панели (Response Rules, заголовок `routing` по User-Agent),
тянет JSONSUB-профили RoscomVPN для HAPP и INCY, крутится на хосте subscription page. К
нодам отношения не имеет и в `install_node.yml` не попадает — иначе N нод станут N
ежедневными писателями в один ресурс панели. Делаем роль `routing_updater` +
`playbooks/routing_updater.yml` + группу `subscription_page`, с сохранением текущего
hardening unit'а и токеном из vault.

### 9. Тест на живой ноде

Molecule и панельные сценарии зелёные, но ни одна строка ещё не проверялась на реальной
системе. Порядок: CI зелёный → тестовая VPS и тестовая панель по `TESTING.ru.md` с
`certificate_acme_environment: staging` → правки → прод.

---

## Бэклог

- **Front-gate (`--front-ip`)** — таблица `inet mirror_gate`, 443 только с адресов фронта.
- **Роль `sni_mirror`** (`install-mirror.sh`) — nginx stream + `ssl_preread`, свой SNI →
  backend:443, неизвестный SNI → reset, open relay только по явному флагу. Обе связки
  (`front_ips` на бэкенде и `backend_ip`/`sni` на фронте) обязаны выводиться из одной
  записи inventory, иначе при замене фронта нода станет недоступной.
- **`node-accelerator` как внешний вызов** — не портируем. Своя таблица
  `remnawave_filter` с `nft -c` и таймером откатa функционально заменяет `na_filter` и
  safety-timer; запуск чужого bash-инсталлятора из Ansible запрещён ТЗ и создаёт двух
  владельцев firewall. На нодах, миграированных с bash, `na_filter` нужно снять, иначе
  будет два набора правил.
