# Полное тестирование Ansible-установщика на тестовом сервере

Эта инструкция предназначена для приёмочного испытания проекта на отдельной VPS и тестовых объектах Remnawave Panel. Она проверяет установку с чистого сервера, восстановление после ошибки на финальной проверке, Panel API, Docker, RemnaNode, Xray, nginx, TLS, firewall, Hosts, Config Profile, Internal Squad, обновление подписки, настоящий VPN-туннель и идемпотентность. Диагностика ТСПУ и создание VPS в этот тест не входят.

Все изменения конфигурации ноды должны выполняться Ansible. Команды диагностики ниже запускаются с Controller через ad-hoc Ansible; вручную устанавливать пакеты, исправлять файлы или перезаписывать конфигурацию на сервере нельзя. Если тест выявил ошибку, исправьте роль или inventory и повторите тот же playbook.

## 1. Что потребуется

Для испытания нужны отдельная VPS, тестовый домен, доступ к тестовой или безопасно изолированной Remnawave Panel 3.3.2, тестовый пользователь с подпиской и Linux Controller; WSL2 также подходит при наличии рабочей сети и OpenSSH. Поддерживаются Debian 12, Debian 13 и Ubuntu 24.04 на `x86_64` или `aarch64`. Предпочтительный доступ к VPS — отдельный пользователь с SSH-ключом и беспарольным `sudo`; также поддерживается явно включаемый вход `root + password`. На VPS изначально должен быть Python 3. Контроллеру нужны Python 3.11–3.13, OpenSSH и доступ к репозиторию, а при парольной аутентификации — пакет `sshpass`.

Заранее определите четыре адреса: публичный IP новой ноды, внешний адрес Controller, исходящий адрес Remnawave Panel и адрес независимого probe. Последний должен находиться вне `remnawave_panel_cidrs`; иначе отрицательная проверка `NODE_PORT` не имеет смысла. Если Controller и Panel выходят в интернет с разных IP, Controller можно использовать как независимый probe через `localhost`.

Используйте уникальные тестовые имена, например `DE-TEST-01`, `de_test_01`, `DE_TEST_01_REALITY` и `node-test-01.example.com`. Не направляйте тест на существующие production-профиль, Host, Node или Squad. Роль не удаляет созданные объекты автоматически.

## 2. DNS и сетевой доступ

Создайте A-запись selfsteal-домена на публичный IP VPS. Домен должен разрешаться напрямую, без Cloudflare Proxy, CDN или другого TLS-терминатора. Для HTTP-01 порт `80/tcp` должен быть доступен из интернета. Публичный `443/tcp` принимает Xray Reality, а обычное TLS-соединение с правильным SNI должно попадать на nginx selfsteal через локальный порт `9443`.

На provider firewall или security group разрешите следующую схему:

| Порт | Источник | Назначение |
|---|---|---|
| `22/tcp` | только `management_cidrs` | Ansible SSH |
| `80/tcp` | интернет | ACME HTTP-01 и HTTP redirect |
| `443/tcp` | интернет | VLESS Reality и selfsteal fallback |
| `2222/tcp` или выбранный `NODE_PORT` | только фактический исходящий IP Panel | Panel → RemnaNode |
| bridge TCP/UDP | только IP entry-ноды | только для bridge-теста |

Не открывайте наружу `9443` и `61000`. Первый должен слушать только loopback, второй является внутренним API Xray. Если Panel находится за NAT, в `remnawave_panel_cidrs` должен быть указан адрес, который VPS видит источником соединения, а не внутренний адрес контейнера или DNS-адрес панели.

До запуска проверьте DNS с Controller:

```bash
getent ahostsv4 node-test-01.example.com
dig +short A node-test-01.example.com
```

Обе команды должны вернуть IP тестовой VPS. Не продолжайте, если запись ещё не распространилась.

## 3. Подготовка Controller

Клонируйте репозиторий и выполняйте команды из каталога `ansible`:

```bash
cd remnawave-node-installer/ansible
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
ansible-galaxy collection install -r collections/requirements.yml
set -o pipefail
```

Если используется `root + password`, установите `sshpass` только на Controller:

```bash
sudo apt-get update
sudo apt-get install -y sshpass
```

Получите SSH host key новой VPS из доверенного канала провайдера. Сравните его fingerprint с результатом `ssh-keyscan`, после чего добавьте ключ в `known_hosts`. Не отключайте `host_key_checking` для итогового теста:

```bash
ssh-keyscan -t ed25519 203.0.113.10 > /tmp/remnawave-test-hostkey
ssh-keygen -lf /tmp/remnawave-test-hostkey
# После сравнения fingerprint с данными провайдера:
ssh-keyscan -H -t ed25519 203.0.113.10 >> ~/.ssh/known_hosts
```

Сначала прогоните локальные проверки проекта:

```bash
yamllint -c .yamllint.yml .
python tests/validate_structure.py
python -m unittest discover -s tests -p 'test_*.py'
ansible-playbook -i inventories/staging/hosts.yml playbooks/install_node.yml --syntax-check
ansible-playbook -i localhost, -c local tests/render_templates.yml
bash tests/test_panel_idempotency.sh
bash tests/test_panel_bridge_idempotency.sh
bash tests/test_panel_errors.sh
ansible-lint --offline
```

Если на Controller доступен Docker daemon, дополнительно выполните Molecule:

```bash
(cd roles/node_base && molecule test)
(cd roles/remnawave_node && molecule test)
```

Переходите к VPS только после успешных локальных проверок.

## 4. Отдельный test inventory

`inventories/test/` целиком в `.gitignore`: в нём реальные IP, домены и URL панели, которым нельзя попадать в публичный репозиторий. Для тестовых прогонов ставьте `certificate_acme_environment: staging`, иначе повторные попытки упрутся в лимиты Let's Encrypt.

Не подставляйте реальные значения в staging-пример. Создайте отдельный inventory:

```bash
mkdir -p inventories/test/group_vars/all
cp inventories/staging/hosts.yml inventories/test/hosts.yml
cp inventories/staging/group_vars/all/panel.yml inventories/test/group_vars/all/panel.yml
cp inventories/staging/group_vars/remnawave_nodes.yml inventories/test/group_vars/remnawave_nodes.yml
cp inventories/staging/group_vars/all/vault.yml.example inventories/test/group_vars/all/vault.yml
ansible-vault encrypt inventories/test/group_vars/all/vault.yml
ansible-vault edit inventories/test/group_vars/all/vault.yml
```

Файл `vault.yml` исключён из Git. Заполните в нём настоящий API token тестовой Panel и, если выбран парольный SSH, `vault_node_root_password`. Токену нужны права чтения и изменения Nodes, Config Profiles, Hosts, Internal Squads и Users, а также доступ к keygen. Cloudflare token нужен только для `cloudflare_dns`; в этом случае добавьте в открытые переменные ссылку `cloudflare_token: "{{ vault_cloudflare_token }}"`. `vault_bridge_secret` можно оставить пустым, тогда роль один раз сгенерирует пароль нового bridge-пользователя и далее будет переиспользовать его из Panel. Если пароль задаётся заранее, явно свяжите его через `bridge_secret: "{{ vault_bridge_secret }}"`.

Минимальный `inventories/test/hosts.yml` для direct EU-ноды:

```yaml
---
all:
  children:
    remnawave_nodes:
      hosts:
        de-test-01:
          ansible_host: 203.0.113.10
          ansible_user: deployer
          ansible_ssh_private_key_file: /secure/path/remnawave-test
          node_id: de_test_01
          node_name: DE-TEST-01
          node_public_ip: 203.0.113.10
          node_country: DE
          node_role: direct
          provider: test-provider
          region: de-fra
          zone: fra-1
          selfsteal_domain: node-test-01.example.com
          profile_name: DE-TEST-01
          internal_squad_name: Ansible Test
```

Для VPS, на которой требуется сохранить `root + password`, замените SSH-параметры этого host следующим блоком. Сам пароль находится только в зашифрованном Vault и не передаётся в командной строке:

```yaml
ansible_user: root
ansible_password: "{{ vault_node_root_password }}"
ansible_become: false
node_ssh_allow_root_password: true
```

По умолчанию `node_ssh_allow_root_password` равен `false`. При включении роль проверяет итог через `sshd -T`, а nftables разрешает порт 22 только адресам из `management_cidrs`. Не задавайте `0.0.0.0/0` ради удобства: при изменении внешнего IP Controller сначала обновите CIDR у провайдера и в inventory.

Проверьте и замените значения в `inventories/test/group_vars/remnawave_nodes.yml`. Для direct-теста файл должен содержать как минимум следующий набор:

```yaml
---
ansible_user: deployer
ansible_port: 22

management_cidrs:
  - 198.51.100.10/32
remnawave_panel_cidrs:
  - 198.51.100.20/32

remnawave_panel_url: https://panel-test.example.com
remnawave_panel_token: "{{ vault_remnawave_panel_token }}"
remnawave_node_image: ghcr.io/remnawave/node:3.3.2
remnawave_node_port: 2222

xray_version: 26.6.27
xray_checksums:
  x86_64: b3e5902d06d6282fe53cfa2fc426058b9aeaa429b2c812e20887cd47f26d08bf
  aarch64: 13a251379bea366c2cf10363ad71e75734193d401f26f518bf0c25e5c8f8c931

inbound_specs:
  - tag: DE_TEST_01_REALITY
    port: 443
    network: raw

host_specs:
  - remark: Germany Test
    address: node-test-01.example.com
    inbound_tag: DE_TEST_01_REALITY
    port: 443
    sni: node-test-01.example.com
    fingerprint: firefox
    security_layer: DEFAULT
    is_hidden: false
    is_disabled: false
    tags: [EU, TEST, DIRECT]

selfsteal_virtual_hosts:
  - name: primary
    server_names: [node-test-01.example.com]
    root: /opt/remnawave-node/selfsteal/html/primary
    title: Test Infrastructure

certificate_mode: http01
acme_email: ops@example.com

verify_require_tunnel_probe: true
verify_tunnel_probe_command:
  - /usr/local/bin/remnawave-vpn-probe
  - DE-TEST-01
  - "203.0.113.10"

verify_require_node_port_denied_probe: true
verify_node_port_untrusted_probe_host: localhost

bridge_spec:
  enabled: false

node_plugins: []
```

Все tags должны состоять только из uppercase-букв, цифр, `_` и `:`. Не переносите API token, `SECRET_KEY`, Reality private key или bridge password в открытый inventory. Обычный запуск сам получает `SECRET_KEY` при первой установке и переиспользует его из `/opt/remnawave-node/remnanode/.env`. Переменная `remnawave_rotate_node_secret_key` должна оставаться `false`.

Inventory является источником истины. Имена Node, Host и Config Profile меняйте в inventory и применяйте Ansible; правка только в Panel создаёт расхождение, которое следующий запуск может вернуть назад или остановить как неоднозначное.

Проверьте inventory и доступ к VPS:

```bash
export TEST_INVENTORY=inventories/test/hosts.yml
export TEST_NODE=de-test-01

ansible-inventory -i "$TEST_INVENTORY" --graph --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -m ansible.builtin.ping --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command -a 'id' --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" \
  -m ansible.builtin.setup -a 'filter=ansible_distribution*' --ask-vault-pass
```

Ожидаются успешный `ping`, `uid=0` в команде с `become` и поддерживаемая ОС. Если Python или sudo не готовы, исправьте cloud-init или образ VPS; не устанавливайте их вручную в обход процесса создания сервера.

Зафиксируйте исходное состояние сервера read-only командами:

```bash
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.stat -a 'path=/opt/remnawave-node/compose.yml' --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command -a 'ss -H -lntup' --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command -a 'df -h /' --ask-vault-pass
```

На чистой VPS управляемый Compose-файл должен отсутствовать, обязательные порты должны быть свободны, а на корневом разделе должно оставаться не менее 2 GiB. Если `/opt/remnawave-node` уже содержит чужую установку или на `80/443/2222` работают неизвестные сервисы, не продолжайте и не удаляйте их автоматически.

## 5. Настоящий VPN probe

`node_verify` намеренно не считает открытый `443` доказательством работы VPN. Команда из `verify_tunnel_probe_command` должна на Controller поднять клиент с конфигурацией именно тестового Host, выполнить HTTPS-запрос через туннель и завершиться с кодом `0` только тогда, когда внешний адрес совпал с IP тестовой ноды. Она не должна печатать UUID пользователя, subscription URL, Reality keys или полный клиентский конфиг.

Универсального probe в проекте нет, потому что формат подписки и используемый клиент зависят от вашей эксплуатации. Практический вариант — отдельный test user в Panel, безопасно сохранённый subscription URL и wrapper над штатным mihomo/Xray-клиентом. Контракт wrapper должен быть таким:

```text
/usr/local/bin/remnawave-vpn-probe <node-name> <expected-egress-ip>
exit 0: подписка обновлена, выбран нужный Host, туннель поднят, HTTPS работает, egress IP совпал
exit != 0: любое из условий не выполнено
```

До появления wrapper разрешено выполнить подготовительную установку с временно отключённой tunnel-проверкой, но этот результат нельзя считать приёмкой. Финальный запуск обязательно проводится с `verify_require_tunnel_probe: true` и реальной командой.

Значение `verify_node_port_untrusted_probe_host: localhost` корректно только если исходящий IP Controller не входит в `remnawave_panel_cidrs`. Если Controller и Panel имеют общий NAT, добавьте в inventory отдельный Linux host и укажите его inventory name, например `untrusted-probe`.

## 6. Preflight

Выполните syntax-check с test inventory, затем preflight. Для preflight не добавляйте `--check`: read-only команды Ansible могут быть пропущены в check mode, из-за чего проверка DNS и listeners станет недостоверной.

```bash
ansible-playbook -i "$TEST_INVENTORY" playbooks/install_node.yml \
  --limit "$TEST_NODE" --syntax-check --ask-vault-pass

ansible-playbook -i "$TEST_INVENTORY" playbooks/install_node.yml \
  --limit "$TEST_NODE" --tags preflight --ask-vault-pass
```

Preflight должен подтвердить ОС, архитектуру, свободное место, DNS, Panel API, CIDR, NTP и отсутствие конфликтующих listeners. Любая ошибка является блокером. Не отключайте `preflight_check_dns`, `preflight_check_panel` или `preflight_check_ports`, чтобы протолкнуть тест дальше.

## 7. Первая установка и восстановление после ошибки

Создайте локальный каталог для логов. Он исключён маской `*.log`, но всё равно не публикуйте его как открытый CI artifact:

```bash
mkdir -p .cache/test-run
```

Если настоящий VPN probe уже готов, выполните обычный полный запуск. Если probe пока не готов, безопасно проверьте восстановление после ошибки: передайте `/bin/false` как probe. Установка дойдёт до финальной E2E-проверки и должна завершиться ошибкой именно на задаче `Execute valid VLESS tunnel probe`, не откатывая созданную ноду.

```bash
ANSIBLE_NOCOLOR=1 ANSIBLE_SHOW_CUSTOM_STATS=true \
ansible-playbook -i "$TEST_INVENTORY" playbooks/install_node.yml \
  --limit "$TEST_NODE" --ask-vault-pass \
  -e '{"verify_tunnel_probe_command":["/bin/false"]}' \
  | tee .cache/test-run/first-install.log
```

Если запуск упал раньше tunnel probe, устраните причину только в Ansible-коде, inventory, DNS, provider firewall или Panel. Ручная установка компонента на VPS запрещена. Повторяйте ту же команду после исправления.

После ожидаемой ошибки в Panel должны существовать Config Profile, inbound, Node, Host и Internal Squad. Node должна быть online. Добавьте отдельного test user в этот Squad, обновите его подписку в клиенте и убедитесь, что Host `Germany Test` появился. Если пользователь уже состоял в заранее выбранном Squad, достаточно обновить подписку.

Настройте настоящий `remnawave-vpn-probe`, удалите временное переопределение `/bin/false` и выполните полный строгий запуск:

```bash
ANSIBLE_NOCOLOR=1 ANSIBLE_SHOW_CUSTOM_STATS=true \
ansible-playbook -i "$TEST_INVENTORY" playbooks/install_node.yml \
  --limit "$TEST_NODE" --ask-vault-pass \
  | tee .cache/test-run/strict-run.log
```

Успешный результат должен содержать `deployment_status: ready`. Проверка должна пройти без ручного изменения файлов или сервисов на VPS.

## 8. Проверки после установки

Получите состояние контейнеров и Compose через Ansible:

```bash
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m community.docker.docker_container_info -a 'name=remnanode' --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m community.docker.docker_container_info -a 'name=nginx-selfsteal' --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command \
  -a 'docker compose -f /opt/remnawave-node/compose.yml ps' --ask-vault-pass
```

Оба контейнера должны быть running и healthy. Затем проверьте Xray, nginx и listeners:

```bash
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command -a 'docker exec remnanode xray version' --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command \
  -a 'docker exec remnanode /command/s6-svstat /run/service/xray' --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command -a 'docker exec nginx-selfsteal nginx -t' --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command -a 'ss -H -lntup' --ask-vault-pass
```

Xray должен показать закреплённую версию, `s6-svstat` должен начинаться с `up`, а `nginx -t` завершиться успешно. На публичных адресах ожидаются `80`, `443` и `NODE_PORT`; `9443` должен быть только на `127.0.0.1`, а `61000` не должен быть публичным.

Проверьте firewall и журналы:

```bash
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command \
  -a 'nft list table inet remnawave_filter' --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command \
  -a 'tail -n 200 /var/log/remnanode/current' --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command \
  -a 'docker logs --tail 200 remnanode' --ask-vault-pass
```

В nftables должны присутствовать management CIDR для SSH, Panel CIDR для `NODE_PORT`, публичные `80/443` и отсутствие общего разрешения на `NODE_PORT`. Файл `/var/log/remnanode/current` должен существовать и содержать Xray output. В свежих логах не должно быть `fatal`, `panic`, `configuration error` или `failed to start`.

Проверьте права на `.env`, TLS и timers:

```bash
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.stat \
  -a 'path=/opt/remnawave-node/remnanode/.env checksum_algorithm=sha256' --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command \
  -a 'openssl x509 -in /etc/letsencrypt/live/node-test-01.example.com/fullchain.pem -noout -serial -startdate -enddate' \
  --ask-vault-pass
ansible -i "$TEST_INVENTORY" "$TEST_NODE" -b \
  -m ansible.builtin.command \
  -a 'systemctl list-timers --all remnawave-*' --ask-vault-pass
```

Файл `.env` должен принадлежать `root:root` и иметь mode `0600`. Сохраните только его SHA-256 checksum, serial и даты сертификата; содержимое `.env` и приватный ключ не выводите. Должны быть включены certificate-renew и disk-check timers.

С Controller проверьте публичный selfsteal:

```bash
curl -fsS -o /tmp/remnawave-selfsteal.html https://node-test-01.example.com/
test -s /tmp/remnawave-selfsteal.html

openssl s_client \
  -connect 203.0.113.10:443 \
  -servername node-test-01.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates
```

Обычный HTTPS-клиент без VLESS-аутентификации должен получить настоящий selfsteal-сайт и валидный сертификат указанного домена. Это подтверждает Reality fallback, а не только наличие открытого порта.

## 9. Panel, подписка и VPN

В Panel сопоставьте результат со следующей цепочкой:

```text
Node DE-TEST-01 online и xrayUptime > 0
  → активен Config Profile DE-TEST-01
  → активен inbound DE_TEST_01_REALITY
  → Host Germany Test указывает именно на этот inbound и тестовую Node
  → Internal Squad Ansible Test содержит inbound
  → test user состоит в этом Squad
```

Эти связи проверяет `node_verify`; интерфейс Panel нужен как независимое визуальное подтверждение. Не считайте видимый Host доказательством работоспособности Node.

Обновите подписку на тестовом клиенте после создания Host. До обновления старый клиентский конфиг не содержит новую ноду. Выберите именно `Germany Test`, установите соединение и выполните через VPN минимум HTTPS-запрос и DNS-запрос. Внешний адрес должен совпасть с IP тестовой VPS:

```bash
curl -4 --fail https://ifconfig.co/ip
curl --fail https://example.com/ -o /dev/null
```

Команды выполняются в окружении, трафик которого действительно направлен через тестовый клиент. Проверка на обычном Controller без поднятого туннеля ничего не доказывает. Финальный `remnawave-vpn-probe` должен автоматически повторять тот же сценарий и завершаться ошибкой при несовпадении egress IP.

Panel online подтверждает разрешённое соединение Panel → Node. С независимого probe соединение к `NODE_PORT` должно завершаться отказом или timeout:

```bash
nc -vz -w 5 203.0.113.10 2222
```

Команда должна завершиться неуспешно. Эта проверка уже входит в строгий `node_verify`; ручной запуск приведён только для независимого подтверждения.

## 10. Идемпотентность

Перед повторным playbook сохраните checksum `.env`, serial сертификата и поля `StartedAt`/`RestartCount` обоих контейнеров из предыдущих команд. Затем запустите тот же строгий playbook без изменений:

```bash
ANSIBLE_NOCOLOR=1 ANSIBLE_SHOW_CUSTOM_STATS=true \
ansible-playbook -i "$TEST_INVENTORY" playbooks/install_node.yml \
  --limit "$TEST_NODE" --ask-vault-pass \
  | tee .cache/test-run/idempotency-run.log
```

Итоговый recap должен содержать `changed=0`. Повторно получите stat `.env`, сертификат и container info. Критерии:

- checksum `.env` не изменился, следовательно `SECRET_KEY` не ротировался;
- serial и даты сертификата не изменились;
- Reality private key, short IDs и bridge password не изменились;
- контейнеры не пересоздавались и `StartedAt` сохранился;
- в Panel не появились дубликаты Node, Host, Profile или Squad;
- `subscription_refresh_required` имеет значение `false`;
- VPN probe снова прошёл.

Любой необъяснимый `changed` является дефектом. Не маскируйте его через `changed_when: false`, пока не установлена реальная причина изменения.

## 11. Безопасное изменение конфигурации

После успешной идемпотентности можно проверить точечное применение несекретного изменения. Добавьте к тестовому Host uppercase-tag `CANARY`, выполните playbook и убедитесь, что изменился Host в Panel, но не `.env`, сертификат и контейнеры. После изменения Host клиенту потребуется обновить подписку. Затем удалите `CANARY`, повторите playbook и ещё раз проверьте идемпотентность.

Для проверки nginx handler измените `title` selfsteal-сайта и на один запуск задайте `selfsteal_refresh_content: true`. Должны измениться только HTML и reload nginx; RemnaNode, `SECRET_KEY`, Reality keys и сертификат не должны меняться. После проверки верните исходный title ещё одним запуском с `selfsteal_refresh_content: true`, затем установите флаг обратно в `false`.

Не используйте для теста ротацию `SECRET_KEY`, Reality key или bridge secret. Это отдельные разрушительные операции, влияющие на рабочие подключения.

## 12. Bridge-тест

Bridge проверяйте только после успешной direct-ноды. Exit-нода должна получить прежнюю логическую bridge identity, а её bridge-порт должен быть разрешён только с IP entry-ноды. Пример переменных:

```yaml
bridge_spec:
  enabled: true
  id: RU_TEST_TO_DE_TEST_01
  entry_address: 198.51.100.30
  entry_inventory_host: ru-entry-test
  user: bridge_ru_test_de_test_01
  tag: BRIDGE_RU_TEST_DE_TEST_01
  port: 8388
  method: 2022-blake3-aes-128-gcm

verify_bridge_probe_command:
  - /usr/local/bin/remnawave-bridge-probe
  - RU_TEST_TO_DE_TEST_01
  - "203.0.113.10"
```

`ru-entry-test` должен быть inventory host, доступным Ansible, либо `verify_bridge_probe_command` должна самостоятельно выполнять E2E с доверенного entry-окружения. Проверка открытого TCP-порта недостаточна: bridge probe должен установить реальное соединение, выйти в интернет через exit и сравнить egress IP с IP EU-ноды.

Критерии bridge:

- bridge user создан один раз, имеет стабильный `ssPassword` и состоит в нужном Squad;
- bridge inbound активен на exit, но не опубликован как обычный пользовательский Host;
- TCP/UDP bridge-порт доступен с entry и недоступен с независимого probe;
- entry → exit E2E проходит и показывает ожидаемый EU egress IP;
- второй запуск даёт `changed=0` и не меняет bridge password;
- при замене тестовой exit VPS с тем же описанием восстанавливается та же bridge identity.

## 13. Проверка логов на утечку секретов

После испытания проверьте локальные job logs:

```bash
rg -n 'SECRET_KEY=|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|vault_remnawave_panel_token' .cache/test-run
```

Команда не должна находить значения секретов. Task names и слово `SECRET_KEY` без значения допустимы. Не прикладывайте к тикету полный лог, inventory, subscription URL или Panel API response без предварительной очистки.

## 14. Протокол приёмки

Зафиксируйте результат:

```text
Дата:
Commit:
ОС и архитектура:
Node name / public IP / domain:
Panel version:
RemnaNode / Xray / nginx versions:
Первый запуск:
Восстановление после намеренно упавшего probe:
Строгий запуск: ready / fail
Повторный запуск: changed=0 / другое
.env checksum сохранился: yes/no
Сертификат не перевыпущен: yes/no
Контейнеры не пересозданы: yes/no
Node online и xrayUptime > 0: yes/no
Host/Profile/Inbound/Squad проверены: yes/no
Selfsteal и TLS проверены: yes/no
NODE_PORT разрешён Panel и закрыт для probe: yes/no
Subscription обновлена: yes/no
VPN E2E и egress IP проверены: yes/no
Bridge E2E: yes/no/not applicable
Утечек секретов в логах нет: yes/no
Открытые дефекты:
```

Тест считается полностью пройденным только при строгом `deployment_status: ready`, втором запуске `changed=0`, неизменных секретах и сертификате, успешном VPN E2E и закрытом для посторонних `NODE_PORT`. Успешные lint, mock или Molecule не заменяют тест на реальной VPS.

## 15. Завершение теста

В проекте пока нет автоматизированной роли decommission. После сохранения отчёта сначала отключите тестовый Host/Node в Panel и убедитесь, что тестовый пользователь больше его не получает. Затем удалите только созданные для теста Host, Node, профиль, Squad и bridge user, если они не используются другими объектами. После этого удалите DNS-запись и VPS через Terraform или интерфейс тестового провайдера.

Не используйте production Terraform workspace для очистки теста. Если какие-либо Panel-объекты оказались общими, остановитесь и разберите зависимости вместо принудительного удаления.
