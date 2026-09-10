# Мониторинг August VPN — агент на ноде

Ansible управляет **только агентской частью мониторинга на VPN-нодах**.
Центральный monitoring-сервер — Prometheus, Grafana, Alertmanager,
blackbox_exporter, capacity exporter и Semaphore exporter — ставится руками, по
инструкции `monitoring/README.ru.md`. Здесь описано ровно то, что делает
Ansible.

Граница проходит так:

| Что | Кто |
|---|---|
| `node_exporter` на каждой ноде | Ansible |
| сборщик установленных TCP-сокетов, его `.service` и `.timer` | Ansible |
| textfile collector, каталог и права | Ansible |
| правило nftables: `9100` только с monitoring-сервера | Ansible (`node_base`) |
| verify: экспортёр отвечает там, где должен, и нигде больше | Ansible |
| снятие агента (`node_monitoring_state: absent`) | Ansible |
| Prometheus, Grafana, Alertmanager, blackbox, оба экспортёра | вы, руками |
| recording rules, alert rules, dashboard, capacity inventory | лежат в `monitoring/`, копируются руками |

Что Ansible на ноде **не** трогает: Xray, Config Profiles, Hosts, squads,
маршрутизацию, мосты и клиентские конфиги. Мониторинг добавляет ровно две вещи —
одно правило firewall с ограничением по источнику и один слушающий процесс.

---

## Как включить

Одна строка в `/etc/remnawave/fleet.yml` на контроллере, плюс адрес
monitoring-сервера:

```yaml
node_monitoring_enabled: true
monitoring_scrape_cidrs:
  - 198.51.100.20/32        # публичный адрес monitoring-сервера
```

Дальше — обычная установка ноды, отдельного плейбука для мониторинга нет:

```bash
ansible-playbook ansible/playbooks/provision_node.yml --limit tr01
```

Новая нода после этого готова к scrape без единого ручного действия на ней:
`node_exporter` установлен, запущен, слушает `:9100` и открыт только для
адресов из `monitoring_scrape_cidrs`. Остаётся добавить её в
`/etc/prometheus/targets/nodes.json` на monitoring-сервере (`file_sd`
перечитывается на лету, Prometheus перезапускать не нужно).

Сухой прогон перед этим — как всегда:

```bash
ansible-playbook ansible/playbooks/provision_node.yml --limit tr01 --check --diff
```

---

## Что именно ставится на ноду

| Компонент | Где |
|---|---|
| `node_exporter` 1.12.1 | `/usr/local/bin/node_exporter`, юнит `node_exporter.service` |
| textfile collector | `/var/lib/node_exporter/textfile` |
| сборщик сокетов | `/usr/local/bin/august-node-sessions` |
| его запуск | `august-node-sessions.service` + `august-node-sessions.timer` |

Релиз `node_exporter` скачивается по закреплённой версии и **проверяется по
контрольной сумме** до распаковки: либо по хэшу, закреплённому в
`node_exporter_sha256`, либо по опубликованному релизом `sha256sums.txt`.
Третьего пути нет — непроверенный архив не распаковывается.

### Метрики сборщика сокетов

Имена честные. Это **установленные TCP-сокеты**, а не «уникальные пользователи
VPN» и не «сессии Xray»: из числа сокетов число людей не выводится, и метрика
названа тем, чем является.

```
august_node_vpn_ports_declared                        1, если VPN-порты известны
august_node_vpn_established_sockets{port,family}      family = ipv4 | ipv6
august_node_vpn_established_sockets_total
august_node_sessions_collected_timestamp_seconds      когда файл был записан
august_node_sessions_excluded_port{port}              что не считается, и это видно
```

Считаются **только VPN-порты**, а не все публичные. Список берётся из
`node_vpn_tcp_ports`, который выводится из `inbound_specs` этой ноды — портов
объявленных inbound'ов, кроме UDP:

```yaml
node_vpn_tcp_ports: >-
  {{ inbound_specs | default([])
     | selectattr('port', 'defined')
     | rejectattr('network', 'equalto', 'udp')
     | map(attribute='port') | map('int') | unique | list }}
```

Именно поэтому 80 не попадает в счёт: это ACME-челлендж и маскирующий сайт, а
HTTP-запрос — не VPN-сокет. Никогда не считаются:

* `remnawave_node_port` (2222) — там сидит соединение самой панели;
* `ansible_port` (22) — там сидит оператор;
* `node_monitoring_exporter_port` (9100) — там сидит Prometheus;
* 80 — ACME и декой-сайт.

Каждый исключённый порт публикуется как `august_node_sessions_excluded_port`,
чтобы исключение было видно, а не подразумевалось.

Если порты вывести не удалось, сборщик публикует
`august_node_vpn_ports_declared 0` и не считает ничего — вместо того чтобы
посчитать не те порты. Дашборд показывает «unknown», а не ноль.

---

## Firewall

`node_base` дописывает **одно** правило в свою таблицу `inet remnawave_filter`:

```
ip saddr <monitoring_scrape_cidrs> tcp dport 9100 accept
```

Существующие правила не меняются. Модель firewall в `node_base` уже построена
на правилах с ограничением по источнику, и мониторинг ложится в неё как ещё
одно такое; переписывать ничего не потребовалось.

Почему не «просто открыть 9100»:

* у `node_exporter` нет ни аутентификации, ни TLS;
* `/metrics` — это карта ноды: интерфейсы, адреса, точки монтирования, uptime.

Поэтому controller-side preflight (шаблон `01`) **отказывает** в четырёх
случаях, до того как VPS будет тронута:

| Ситуация | Результат |
|---|---|
| `monitoring_scrape_cidrs: []` при включённом мониторинге | отказ |
| `0.0.0.0/0` в списке | отказ |
| `::/0` в списке | отказ |
| `9100` в `node_public_tcp_ports` или `node_public_udp_ports` | отказ |

Пустой список — это ошибка, а не «разрешить всем». Проверяется сценариями 9–13
в `ansible/tests/preflight_guards.yml`.

Смена firewall идёт по обычному пути `node_base`: перед применением взводится
таймер отката (`node_firewall_rollback_seconds`, 120 с), после применения
проверяется новое SSH-соединение, и только тогда таймер снимается. Если правило
отрежет управление — нода вернётся сама.

Если у нод есть приватная сеть — укажите в `monitoring_scrape_cidrs` приватный
адрес monitoring-сервера и поставьте `node_monitoring_bind_address` на приватный
адрес ноды. Тогда экспортёр вообще не будет слушать публичный интерфейс. На
обычной VPS без приватной сети правило nftables — единственный контроль, и это
нормально, пока список источников узкий.

---

## Verify

После установки роль проверяет сама себя и падает, если что-то не так:

* экспортёр отвечает на `127.0.0.1:9100/metrics`;
* в ответе есть `august_node_vpn_established_sockets_total`,
  `august_node_vpn_ports_declared` и `node_cpu_seconds_total` — то есть
  textfile collector действительно подхватил файл;
* **ни один** исключённый порт не попал в счёт сокетов;
* порт экспортёра в итоговом ruleset ограничен по источнику, а не открыт.

---

## Идемпотентность

Второй прогон подряд даёт `changed=0`. Это проверяется не на глаз, а сценарием
Molecule на Debian 12, Debian 13 и Ubuntu 24.04:

```bash
cd ansible/roles/node_monitoring && molecule test
```

Сценарий ставит агент на чистый контейнер, прогоняет converge второй раз и
падает, если хоть одна задача сообщила об изменении.

---

## Снятие агента

Через Ansible, не руками по SSH:

```bash
ansible-playbook ansible/playbooks/provision_node.yml --limit tr01 \
  -e node_monitoring_state=absent
```

Удаляются: юниты (остановлены, отключены, удалены, `daemon_reload`), сам
`node_exporter`, каталог релиза, сборщик сокетов, textfile-каталог и служебная
учётная запись экспортёра.

Правило firewall принадлежит `node_base` и здесь **не трогается намеренно**:
роль, которая лезет в чужую цепочку правил, — это то, как откат необязательного
дополнения уносит с собой SSH. Снять правило:

```yaml
# /etc/remnawave/fleet.yml
node_monitoring_enabled: false
```

```bash
ansible-playbook ansible/playbooks/install_node.yml --limit tr01 --tags firewall
```

— снова с таймером отката.

---

## Настройки

Всё не-секретное — в `/etc/remnawave/fleet.yml`. Секретов агенту не нужно
вообще: `node_exporter` защищён правилом firewall, а не паролем.

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `node_monitoring_enabled` | `false` | включает агент на ноде |
| `monitoring_scrape_cidrs` | `[]` | кто может дотянуться до `:9100` |
| `node_monitoring_state` | `present` | `absent` снимает агент |
| `node_monitoring_exporter_port` | `9100` | порт экспортёра |
| `node_monitoring_bind_address` | — | приватный адрес, если он есть |
| `node_monitoring_sessions_enabled` | `true` | сборщик сокетов |
| `node_monitoring_sessions_interval` | — | как часто он считает |
| `node_exporter_version` | `1.12.1` | закреплённая версия |
| `node_exporter_sha256` | `""` | хэш релиза; пусто — берётся `sha256sums.txt` |

---

## Ёмкость ноды: один раз в inventory

Ёмкость новой ноды указывается **один раз**, в Ansible inventory, и сама
попадает в monitoring — руками в `monitoring/capacity/capacity.yml` больше
ничего не дописывается.

Вручную:

```yaml
tr02:
  ansible_host: 1.2.3.4
  capacity_download_mbps: 1000
  capacity_upload_mbps: 1000
  capacity_certain: true
```

Автоматически (installer измеряет сам):

```yaml
tr02:
  ansible_host: 1.2.3.4
  capacity_auto_test: true
```

В auto-режиме нода на установке запускает iperf3 до нескольких российских
городов в обе стороны по три раза: берётся медиана по городу и максимум между
городами, применяется запас 90 % и округление вниз. Если хотя бы два города
ответили в каждую сторону — нода получает `capacity_certain` и измеренная
величина становится источником истины (генерируемый per-node файл, переживает
следующий sync). Иначе нода остаётся `unmeasured`, ничего не выдумывается.

**Список серверов — встроенный default.** Роль `node_capacity_test` уже содержит
актуальный набор из
[itdoginfo/russian-iperf3-servers](https://github.com/itdoginfo/russian-iperf3-servers)
(Москва, СПб, Нижний Новгород, Челябинск, Тюмень — primary + fallback у каждого).
Серверы прописаны в роли и опрашиваются напрямую; никакой `wget | bash` при
установке не выполняется. Публичные iperf3-серверы со временем меняются — это
именно default, а не гарантия. Свой набор задаётся без правки роли, в
`fleet.yml` или group_vars:

```yaml
# один сервер:
capacity_test_host_moscow: iperf.my-server.example
# или весь список:
capacity_test_endpoints:
  - city: Moscow
    hosts:
      - {host: iperf-a.example, ports: "{{ capacity_test_ports }}"}
      - {host: iperf-b.example, ports: "{{ capacity_test_ports }}"}
```

Порт — конфигурируемый диапазон `5201–5209` (`capacity_test_ports`). Недоступный
primary → fallback; недоступны оба → город пропускается, остальные считаются.
`capacity_auto_test` вместе с ручной величиной на одной ноде запрещены —
источник истины один. Померить ноду отдельно (в том числе до production) можно
через `ansible/playbooks/measure_capacity.yml`.

---

## Дальше

Ноды готовы к scrape. Что делать на monitoring-сервере — Prometheus, правила,
дашборд, экспортёры, секреты, проверка — описано в **`monitoring/README.ru.md`**.
