# Мониторинг August VPN — центральный сервер, ручная установка

Этот каталог — **не Ansible**. Ansible ставит только агент на VPN-ноде
(`node_exporter`, сборщик сокетов, правило firewall) — про это
`ansible/MONITORING.ru.md`. Всё, что здесь, вы ставите на monitoring-сервер
руками, по этой инструкции.

Разделение простое:

| Что | Кто ставит |
|---|---|
| `node_exporter` и сборщик сокетов на каждой ноде | Ansible, в обычной установке ноды |
| правило nftables `9100` только с monitoring-сервера | Ansible (`node_base`) |
| Prometheus, Grafana, Alertmanager, blackbox_exporter | вы, руками |
| capacity exporter, Semaphore exporter | вы, руками |
| recording rules, alert rules, dashboard | лежат здесь, копируете руками |

---

## Что лежит в этом каталоге

```
monitoring/
  prometheus/prometheus.yml.example        конфиг Prometheus, с <ПЛЕЙСХОЛДЕРАМИ>
  prometheus/recording-rules.yml           готовый файл, копируется как есть
  prometheus/alert-rules.yml               готовый файл, копируется как есть
  prometheus/targets/nodes.json.example    file_sd со списком нод
  prometheus/targets/blackbox.json.example file_sd для проб доступности
  alertmanager/alertmanager.yml.example    конфиг Alertmanager, с плейсхолдерами
  blackbox/blackbox.yml                    готовый файл, один модуль tcp_connect
  grafana/august-capacity.json             дашборд, импортируется как есть
  capacity/capacity.yml                    версионированный инвентарь ёмкости
  capacity/README.md                       схема этого файла и все инварианты
  capacity_exporter.py                     экспортёр ёмкости и состояний нод
  semaphore_task_exporter.py               экспортёр длительностей задач Semaphore
  capacity_model.py strict_yaml.py scaling.py   модули, которые они импортируют
  validate_capacity.py                     проверка capacity.yml
  build_dashboard.py                       генератор дашборда
  tests/                                   тесты всего перечисленного
```

Файлы с суффиксом `.example` — шаблоны: копируете, заменяете `<ПЛЕЙСХОЛДЕРЫ>`.
Файлы без него копируются байт в байт: именно их проверяет CI
(`monitoring/tests/test_monitoring_rules.sh`), поэтому редактировать их на
сервере не надо — правьте в Git и копируйте заново.

---

## 1. Что поставить на monitoring server

Нужен отдельный (или совмещённый с контроллером Semaphore) хост: Debian 12/13
или Ubuntu 24.04, 2 vCPU, 4 ГБ RAM, 40 ГБ SSD, systemd, `python3` и
`python3-yaml`.

Версии, на которых всё проверялось. Ставьте эти же — рекординг-правила и
алерты писались под их поведение:

| Компонент | Версия | Откуда |
|---|---|---|
| Prometheus | 3.14.0 | `https://github.com/prometheus/prometheus/releases` |
| Alertmanager | 0.33.0 | `https://github.com/prometheus/alertmanager/releases` |
| blackbox_exporter | 0.28.0 | `https://github.com/prometheus/blackbox_exporter/releases` |
| Grafana OSS | 13.1.3 | `https://dl.grafana.com/oss/release/` |
| node_exporter | 1.12.1 | ставится Ansible'ом на ноды, здесь не нужен |

Скачивайте архив вместе с его `sha256sums.txt` и **проверяйте контрольную
сумму до распаковки** — это единственное, что отличает релиз от чего угодно
другого:

```bash
cd /tmp
curl -fsSLO https://github.com/prometheus/prometheus/releases/download/v3.14.0/prometheus-3.14.0.linux-amd64.tar.gz
curl -fsSLO https://github.com/prometheus/prometheus/releases/download/v3.14.0/sha256sums.txt
sha256sum -c sha256sums.txt --ignore-missing
tar xzf prometheus-3.14.0.linux-amd64.tar.gz
sudo install -m 0755 prometheus-3.14.0.linux-amd64/prometheus /usr/local/bin/
sudo install -m 0755 prometheus-3.14.0.linux-amd64/promtool    /usr/local/bin/
```

То же самое для Alertmanager (`alertmanager`, `amtool`) и blackbox_exporter.
Grafana распаковывается целиком: ей нужны `public/` и `conf/`, а не один
бинарник.

**Ничего из этого не должно слушать публичный адрес.** У Prometheus нет
аутентификации вообще, а Grafana — это один пароль до адресов всех нод парка.
Всё биндится на `127.0.0.1` и смотрится через SSH-туннель:

```bash
ssh -N -L 9090:127.0.0.1:9090 -L 3000:127.0.0.1:3000 deployer@<monitoring server>
```

Служебные учётки — системные, без shell:

```bash
for u in prometheus alertmanager blackbox august-capacity august-semaphore grafana; do
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$u" 2>/dev/null || true
done
```

---

## 2. Куда положить Prometheus config

```bash
sudo mkdir -p /etc/prometheus/rules /etc/prometheus/targets /var/lib/prometheus
sudo cp monitoring/prometheus/prometheus.yml.example /etc/prometheus/prometheus.yml
sudo chown -R prometheus:prometheus /var/lib/prometheus
```

Откройте `/etc/prometheus/prometheus.yml` и замените:

* `<PANEL_HOST>` — хост панели Remnawave (порт `METRICS_PORT`, по умолчанию 3001);
* `<METRICS_USER>` — `METRICS_USER` из `.env` панели.

Пароль в этот файл **не пишется**: он лежит отдельным файлом, потому что
`/api/v1/status/config` отдаёт содержимое конфига любому, кто дотянулся до
Prometheus:

```bash
printf '%s' '<METRICS_PASS>' | sudo tee /etc/prometheus/remnawave-metrics.password >/dev/null
sudo chown prometheus:prometheus /etc/prometheus/remnawave-metrics.password
sudo chmod 0600 /etc/prometheus/remnawave-metrics.password
```

Список нод — `file_sd`, обычный JSON, перечитывается на лету раз в минуту, без
перезапуска Prometheus:

```bash
sudo cp monitoring/prometheus/targets/nodes.json.example    /etc/prometheus/targets/nodes.json
sudo cp monitoring/prometheus/targets/blackbox.json.example /etc/prometheus/targets/blackbox.json
```

В `nodes.json` для каждой ноды укажите её адрес и **лейбл `node` ровно с тем
именем, которым нода называется в парке** (`TR-01`, `DE-02`). Через этот лейбл
соединяется всё остальное; имена берите из `capacity/capacity.yml` и из
inventory Semaphore — они должны совпадать, расхождение видно как
`august_topology_drift`. Необязательный `country` — единственный источник
переменной «Country / location» в дашборде.

Юнит:

```ini
# /etc/systemd/system/prometheus.service
[Unit]
Description=Prometheus
After=network-online.target
Wants=network-online.target

[Service]
User=prometheus
Group=prometheus
ExecStart=/usr/local/bin/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus \
  --storage.tsdb.retention.time=90d \
  --storage.tsdb.retention.size=20GB \
  --web.listen-address=127.0.0.1:9090
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/prometheus

[Install]
WantedBy=multi-user.target
```

---

## 3. Куда положить recording rules

```bash
sudo cp monitoring/prometheus/recording-rules.yml /etc/prometheus/rules/
```

`prometheus.yml` уже ссылается на `/etc/prometheus/rules/recording-rules.yml`.
Файл копируется **без изменений**: 54 правила, восемь групп, и именно на этом
файле CI гоняет `promtool check rules --lint-fatal` и `promtool test rules`.

Что в нём важно понимать: **сервисная ёмкость — это не сумма железа.** Поток
`RU-01 → мост → DE-01` пересекает две машины и попадает в счётчики обеих. Если
их сложить, один гигабит пользователя превратится в два. Поэтому считаются три
разные величины:

| Ряд | Что это |
|---|---|
| `august:service_usage_bps` | уникальный пользовательский трафик — главный KPI |
| `august:physical_usage_bps` | сумма счётчиков нод, транзит посчитан дважды — диагностика |
| `august:bridge_used_bps` | трафик конкретного моста, по source outbound tag |

```
service_usage = clamp_min(physical_usage − bridge_usage_total, 0)
```

Счётчик тега моста измеряет ровно ту величину, которая задваивается. Это не
рассуждение, а проверенный факт: `monitoring/tests/test_service_accounting.sh`
подаёт синтетический поток 1 Гбит/с через `promtool test rules` и требует
service = 1, physical = 2, bridge = 1.

---

## 4. Куда положить alert rules

```bash
sudo cp monitoring/prometheus/alert-rules.yml /etc/prometheus/rules/
sudo systemctl restart prometheus
```

29 алертов, три класса в лейбле `class`:

* `class="data"` — мониторинг не видит. Эти читаются первыми: любая цифра
  ёмкости стоит ровно столько, сколько стоит тишина в этом классе;
* `class="health"` — что-то сломано или недоступно;
* `class="capacity"` — что-то заполняется. RED держится 10 минут прежде чем
  стать уведомлением, чтобы пятиминутный пик не будил человека.

Два правила проходят через все: алерт ёмкости никогда не срабатывает для
неактивной ноды, а намеренно выключенная нода не даёт алерта вообще —
`august_node_administratively_disabled` остаётся метрикой для дашборда.

**Что этот стек намеренно НЕ шлёт.** У флота уже есть отдельная система
алертов: падение сервера по любой причине, добавление/редактирование/удаление
нод, изменения пользователей, torrent blocker, CRM и общий critical-канал.
Дублировать их здесь — значит приучить людей игнорировать один из двух
источников. Поэтому `NodeOffline`, `NodeUnhealthy`, `NodeUnknownToPanel`,
`ScrapeTargetDown`, `TopologyDrift`, `NodeIdentity*`, `PoolSingleNodeRemaining`
и `CapacityUnrated` больше **не алерты** — соответствующие метрики
публикуются и видны на дашборде, но Alertmanager по ним молчит.

**Что уходит в Telegram.** Только `severity="critical"`, и это ровно пять
правил: CPU > 90% (5m), RAM > 95% (5m) и свободная полоса < 200 Мбит/с (5m)
для ноды, пула и моста. Всё остальное — warning и info — остаётся в Prometheus
и Grafana и телефон не трогает.

**Пороги полосы абсолютные, не проценты.** 20% от канала 5 Гбит/с — это
гигабит ещё свободен, а 20% от 1 Гбит/с — уже авария; один процент не может
означать и то и другое. Проценты остаются на дашборде рядом с абсолютной
цифрой: GREEN ≥ 500 Мбит/с свободно, YELLOW 200–500, RED < 200. Там, где
ёмкость не измерена или стала верхней оценкой, показывается N/A, а не RED, и
алерта не будет вовсе.

---

## 5. Как подключить Grafana dashboard

Grafana ставится из своего архива (нужна вся распакованная директория):

```bash
sudo mkdir -p /etc/grafana/provisioning/datasources /etc/grafana/provisioning/dashboards \
             /var/lib/grafana/dashboards
sudo cp monitoring/grafana/august-capacity.json /var/lib/grafana/dashboards/
```

Источник данных и провайдер дашбордов:

```yaml
# /etc/grafana/provisioning/datasources/prometheus.yml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    uid: august-prometheus        # дашборд ссылается именно на этот uid
    access: proxy
    url: http://127.0.0.1:9090
    isDefault: true
```

```yaml
# /etc/grafana/provisioning/dashboards/august.yml
apiVersion: 1
providers:
  - name: august
    folder: August
    type: file
    allowUiUpdates: false
    options:
      path: /var/lib/grafana/dashboards
```

`uid: august-prometheus` менять нельзя — все 104 запроса дашборда ссылаются на
него. Если источник данных назвать иначе, панели будут пустыми.

В `grafana.ini` укажите `http_addr = 127.0.0.1`, `http_port = 3000` и
`[security] admin_user`. Пароль администратора Grafana применяет **только при
первом запуске**, когда создаёт пользователя: поменять его потом можно лишь
так —

```bash
sudo systemctl stop grafana
sudo -u grafana /usr/local/bin/grafana cli \
  --homepath=/usr/share/grafana --config=/etc/grafana/grafana.ini \
  admin reset-admin-password '<НОВЫЙ ПАРОЛЬ>'
sudo systemctl start grafana
```

Правка `admin_password` в `grafana.ini` с последующим рестартом **не меняет
ничего**: старый пароль продолжит работать, новый — нет.

Дашборд — 56 панелей, 9 рядов: Global capacity, Pool status, Pool capacity
charts, Nodes, Bridges, Connections, Quotas, Scaling and recommendation,
Infrastructure and data quality. Пять переменных (`environment`, `pool`,
`country`, `node`, `bridge`) реально применяются в запросах.

Дашборд — **сгенерированный** файл. Правьте `monitoring/build_dashboard.py` и
перегенерируйте (`python3 monitoring/build_dashboard.py`); ручная правка JSON
будет потеряна и поймана тестом.

---

## 6. Как запустить capacity exporter

Читает `GET /api/nodes` панели и `capacity.yml`, публикует ёмкость, состояния
нод, идентичность и расхождения топологии. Он ничего не включает и никогда не
пишет в панель.

```bash
sudo mkdir -p /opt/august-monitoring/capacity-exporter /var/lib/august-monitoring/capacity
sudo cp monitoring/strict_yaml.py monitoring/capacity_model.py \
        monitoring/capacity_exporter.py monitoring/validate_capacity.py \
        /opt/august-monitoring/capacity-exporter/
sudo cp monitoring/capacity/capacity.yml /etc/august-monitoring/capacity.yml
sudo chown -R august-capacity:august-capacity /var/lib/august-monitoring/capacity
```

`strict_yaml.py` и `capacity_model.py` обязательны: экспортёр их импортирует и
без них не стартует.

Перед копированием **обязательно** проверьте инвентарь — иначе цифра ёмкости,
на которой стоят алерты, окажется той, которую никто не проверял:

```bash
python3 monitoring/validate_capacity.py monitoring/capacity/capacity.yml
```

Токен панели — через `EnvironmentFile`, а не аргументом командной строки:
аргументы видны в `/proc` любому локальному пользователю.

```bash
# /etc/august-monitoring/capacity-exporter.env   (chmod 0600)
REMNAWAVE_PANEL_URL=https://panel.your-domain.tld
REMNAWAVE_PANEL_TOKEN=<токен панели>
```

```ini
# /etc/systemd/system/august-capacity-exporter.service
[Unit]
Description=August capacity exporter
After=network-online.target

[Service]
User=august-capacity
Group=august-capacity
EnvironmentFile=/etc/august-monitoring/capacity-exporter.env
ExecStart=/usr/bin/python3 /opt/august-monitoring/capacity-exporter/capacity_exporter.py \
  --capacity /etc/august-monitoring/capacity.yml \
  --state /var/lib/august-monitoring/capacity/first-seen.sqlite \
  --targets /etc/prometheus/targets/nodes.json \
  --listen-address 127.0.0.1 --listen-port 9301
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/august-monitoring/capacity

[Install]
WantedBy=multi-user.target
```

Точный список опций — `python3 capacity_exporter.py --help`.

Как он ведёт себя, когда что-то недоступно, — это не мелочь, а часть контракта:

* **панель не отвечает** — отдаёт последний удачный ответ и его возраст
  (`august_capacity_panel_state_age_seconds`); после 600 секунд поднимает
  `august_capacity_panel_state_stale` и состояния нод читаются как
  неизвестные, а не как здоровые;
* **`capacity.yml` испорчен** — продолжает отдавать последний валидный
  инвентарь и говорит об этом вслух:
  `august_capacity_inventory_valid 0`,
  `august_capacity_inventory_last_good_in_use 1`. Нулей вместо ёмкости не
  бывает.

---

## 7. Как запустить Semaphore exporter

Публикует **только длительности и статусы задач**. Читать `/output` и логи
задач он не может: список запрещённых путей проверяется перед каждым запросом,
и тест падает, если запрет обойти.

Semaphore слушает `127.0.0.1:3000`, поэтому экспортёр работает только на той же
машине, где работает Semaphore. Если monitoring-сервер отдельный — уберите job
`august_semaphore` из `prometheus.yml`, и всё остальное продолжит работать.

```bash
sudo mkdir -p /opt/august-monitoring/semaphore-exporter
sudo cp monitoring/semaphore_task_exporter.py /opt/august-monitoring/semaphore-exporter/
```

```bash
# /etc/august-monitoring/semaphore-exporter.env   (chmod 0600)
SEMAPHORE_URL=http://127.0.0.1:3000
SEMAPHORE_API_TOKEN=<токен только на чтение>
```

```ini
# /etc/systemd/system/august-semaphore-exporter.service
[Unit]
Description=August Semaphore task duration exporter
After=network-online.target

[Service]
User=august-semaphore
Group=august-semaphore
EnvironmentFile=/etc/august-monitoring/semaphore-exporter.env
ExecStart=/usr/bin/python3 /opt/august-monitoring/semaphore-exporter/semaphore_task_exporter.py \
  --projects 1 --window-days 90 \
  --listen-address 127.0.0.1 --listen-port 9302
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

---

## 8. Какие secrets нужны

Все живут **на monitoring-сервере**, ни один не попадает в Git и ни один не
нужен Ansible.

| Секрет | Куда положить | Права | Откуда взять |
|---|---|---|---|
| `METRICS_PASS` панели | `/etc/prometheus/remnawave-metrics.password` | 0600, `prometheus` | `.env` Remnawave |
| `METRICS_USER` панели | значение в `prometheus.yml` | 0644 | `.env` Remnawave |
| Токен панели (`GET /api/nodes`) | `/etc/august-monitoring/capacity-exporter.env` | 0600 | панель Remnawave |
| API-токен Semaphore, только чтение | `/etc/august-monitoring/semaphore-exporter.env` | 0600 | Semaphore → User → API Tokens |
| Telegram bot token | `/etc/alertmanager/telegram.token` | 0600, `alertmanager` | @BotFather |
| Пароль admin Grafana | база Grafana, через `reset-admin-password` | — | придумать, ≥ 16 символов |

Правило одно и оно техническое: **пароль — это путь к файлу, а не значение.**
Так он не попадает ни в `/api/v1/status/config`, ни в `/proc`.

---

## 9. Какие endpoints и порты нужны

На monitoring-сервере — всё на loopback:

| Служба | Адрес |
|---|---|
| Prometheus | `127.0.0.1:9090` |
| Alertmanager | `127.0.0.1:9093` |
| blackbox_exporter | `127.0.0.1:9115` |
| capacity exporter | `127.0.0.1:9301` |
| Semaphore exporter | `127.0.0.1:9302` |
| Grafana | `127.0.0.1:3000` |

Исходящие соединения, которые нужны monitoring-серверу:

| Куда | Порт | Зачем |
|---|---|---|
| каждая VPN-нода | TCP 9100 | scrape `node_exporter` |
| каждая VPN-нода | TCP 443 (и другие пользовательские) | пробы blackbox |
| панель Remnawave | TCP `METRICS_PORT` (3001) | метрики панели |
| панель Remnawave | TCP 443 | `GET /api/nodes` для capacity exporter |
| `api.telegram.org` | TCP 443 | уведомления |

Со стороны ноды порт 9100 открыт **только** для адресов из
`monitoring_scrape_cidrs`; это делает Ansible, и `0.0.0.0/0` там запрещён
preflight'ом. Публичный адрес monitoring-сервера должен попасть в
`monitoring_scrape_cidrs` в `/etc/remnawave/fleet.yml` **до** установки нод.

`METRICS_PORT` панели должен быть доступен monitoring-серверу и никому больше —
это настройка панели, не этого репозитория.

---

## 10. Как проверить, что всё работает

Сначала — то, что можно проверить до установки, прямо из репозитория:

```bash
python3 monitoring/validate_capacity.py monitoring/capacity/capacity.yml
bash    monitoring/tests/test_monitoring_rules.sh      # promtool + amtool
bash    monitoring/tests/test_service_accounting.sh    # контракт учёта
python3 -m unittest discover -s monitoring/tests -t . -p 'test_*.py'
```

Потом — на сервере, по порядку:

```bash
promtool check config /etc/prometheus/prometheus.yml
amtool check-config   /etc/alertmanager/alertmanager.yml
systemctl status prometheus alertmanager blackbox_exporter grafana \
                 august-capacity-exporter august-semaphore-exporter
ss -Hltn | grep -E ':(9090|9093|9115|9301|9302|3000)\b'   # только 127.0.0.1
```

Затем через туннель:

1. **Prometheus → Status → Targets** — все `august_node_exporter` в `UP`.
   Нода в `DOWN` — либо не установлена Ansible'ом, либо её firewall не пускает
   этот адрес.
2. **Prometheus → Status → Rules** — 8 групп `august_*` recording и 4 группы
   алертов, ни одной в состоянии error.
3. `curl -s 127.0.0.1:9301/metrics | grep august_capacity_inventory_valid` → `1`.
4. `curl -s 127.0.0.1:9090/api/v1/query?query=august:service_usage_bps` — есть
   значение. Пусто — значит лейблы метрик панели называются иначе, см. ниже.
5. **Grafana → August → capacity and health**: ряд 1 показывает сервисную и
   физическую цифры, и они различаются ровно на мостовую; ряд 9 тихий.
6. Проверьте маршрутизацию, не создавая нагрузки:

   ```bash
   amtool config routes test --config.file /etc/alertmanager/alertmanager.yml severity=critical
   amtool config routes test --config.file /etc/alertmanager/alertmanager.yml severity=warning
   ```

   Первая команда должна ответить `telegram-critical`, вторая —
   `dashboard-only`. Ронять ноду для проверки здесь нечем: по её падению этот
   стек намеренно не алертит, это присылает существующая система.

**Что не подтверждено на живой системе.** Лейблы метрик Remnawave
(`node_uuid`, `node_name`, `tag`) взяты из документации, а не с работающего
эндпоинта. Проверьте одной командой:

```bash
curl -su "$METRICS_USER:$METRICS_PASS" \
  http://<panel>:3001/metrics | grep -m3 remnawave_node_inbound_download_bytes
```

Если они называются иначе — меняются только имена лейблов в
`prometheus.yml` (`metric_relabel_configs`) и в recording rules. Пока они не
подтверждены, `august:service_usage_bps` будет пустым и сработают
`AugustRequiredSeriesMissing` и `AugustTrafficCounterStale` — то есть
отсутствие данных **видно**, а не подменяется нулём. Это проектное поведение.
