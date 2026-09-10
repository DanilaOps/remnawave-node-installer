# Пошаговая установка Remnawave Node 3.3.2

Эта инструкция относится к установщику **3.3.2-rw3**:

- Remnawave Panel/Node: `3.3.2`;
- Xray-core: строго `26.6.27`;
- маскировка по умолчанию: VLESS + TCP + Reality + Vision;
- домен и его A-запись создаются вручную до запуска установщика.

Команды ниже выполняются **по одной**, от имени `root`, на чистом Debian 12 или
Ubuntu 22.04/24.04.

## 1. Подготовить DNS

Создайте A-запись домена ноды:

```text
de1.example.com -> ПУБЛИЧНЫЙ_IP_НОДЫ
```

Если DNS управляется через Cloudflare, запись должна быть **DNS only** (серое
облако), а не Proxied. Дождитесь обновления DNS и проверьте на сервере:

```bash
getent ahostsv4 de1.example.com
```

В результате должен отображаться публичный IPv4 устанавливаемой ноды.

## 2. Перейти в root

```bash
sudo -i
```

## 3. Установить curl

```bash
apt-get update -qq
```

```bash
apt-get install -y curl
```

## 4. Безопасно сохранить API-токен панели

Выполните:

```bash
umask 077
```

```bash
read -rsp 'Paste panel API token: ' RW_TOKEN
```

После двоеточия вставьте токен и нажмите `Enter`. Ввод не отображается — это
нормально. Затем выполните:

```bash
echo
```

```bash
printf '%s' "$RW_TOKEN" > /root/panel.token
```

```bash
unset RW_TOKEN
```

Проверка файла без вывода самого токена:

```bash
test -s /root/panel.token && echo TOKEN_FILE_OK
```

Токен должен иметь права API на `Keygen`, `Nodes`, `Hosts`, `Config Profiles` и
`Node Plugins` (read/create/update).

## 5. Скачать установщик

```bash
curl -fsSLo /root/remnawave-node.sh https://raw.githubusercontent.com/DanilaOps/remnawave-node-installer/v3.3.2-rw3/remnawave-node.sh
```

```bash
chmod 700 /root/remnawave-node.sh
```

Проверить shell-синтаксис:

```bash
bash -n /root/remnawave-node.sh
```

Если команда ничего не вывела, синтаксис корректен.

## 6. Запустить интерактивную установку

```bash
bash /root/remnawave-node.sh --panel-token-file /root/panel.token
```

Пример правильных ответов:

| Вопрос | Что вводить |
|---|---|
| `Selfsteal domain` | домен ноды, например `de1.example.com` |
| `Is this node behind an SNI-mirror front` | `N`, если отдельного SNI-фронта нет |
| `Panel URL` | только базовый URL, например `https://panel.example.com` |
| `Panel whitelist IP/CIDR` | публичный IP **сервера панели**, без скобок и кавычек |
| `Cert mode` | `le443` |
| `ACME email` | действующий email для Let's Encrypt |
| `Country code` | ISO-2 код страны ноды, например `DE`, `FI`, `NL` |
| `NODE_PORT` | `2222` |
| `Selfsteal port` | `9443` |
| `Renewal port` | `8443` |
| `SSH port` | реальный SSH-порт сервера, обычно `22` |
| `Enable native Remnawave Torrent Blocker` | `Y` |
| `Torrent source-IP block duration` | `3600` |
| `Cascade bridge` | `N` для обычной самостоятельной ноды |
| `Masking model` | `reality` |
| `Transport` | `tcp` |

Для `Panel URL` нельзя указывать `/dashboard/management/settings` или другой
путь интерфейса. Правильно:

```text
https://panel.example.com
```

Для whitelist указывается IP панели, а не IP устанавливаемой ноды. Пример:

```text
198.51.100.20
```

На финальном экране проверьте домен, IP, страну, `NODE_PORT`, профиль и whitelist.
Нажмите `Enter`, чтобы начать установку.

Установка ядра XanMod и создание initramfs могут занимать продолжительное время.
Не закрывайте SSH-сеанс, пока скрипт не напечатает итоговый отчёт.

## 7. Проверить установленную версию Xray

После завершения:

```bash
docker exec remnanode /usr/local/bin/rw-core version
```

Первая строка должна начинаться так:

```text
Xray 26.6.27
```

Проверить bind mount:

```bash
grep '/usr/local/bin/xray:ro' /opt/remnanode/docker-compose.yml
```

Проверить обязательную capability Torrent Blocker:

```bash
grep -A2 'cap_add' /opt/remnanode/docker-compose.yml
```

Проверить контейнеры:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}'
```

Проверить состояние ноды:

```bash
remnanode status
```

Проверить созданные nftables-таблицы плагина после запуска ноды:

```bash
nft list table ip remnanode
```

Если таблица ещё не появилась, проверьте привязку plugin-профиля в панели и логи:

```bash
docker logs remnanode --tail 200 | grep -iE 'torrent|plugin|nft'
```

Проверить слушающие порты:

```bash
ss -lntp | grep -E ':(80|443|2222)\b'
```

## 8. Продолжить прерванную установку

Если установщик был прерван после сохранения параметров:

```bash
bash /root/remnawave-node.sh --resume -y --panel-token-file /root/panel.token
```

Скрипт загрузит сохранённые параметры из:

```text
/opt/remnawave-node/state/inputs.env
```

Он повторно использует созданные ресурсы панели и Reality-ключи, а отсутствующие
этапы выполнит заново.

## 9. Перевести уже установленную ноду на Xray 26.6.27

Сначала скачайте новый установщик:

```bash
curl -fsSLo /root/remnawave-node.sh https://raw.githubusercontent.com/DanilaOps/remnawave-node-installer/v3.3.2-rw3/remnawave-node.sh
```

```bash
chmod 700 /root/remnawave-node.sh
```

```bash
bash -n /root/remnawave-node.sh
```

Затем выполните resume:

```bash
bash /root/remnawave-node.sh --resume -y --panel-token-file /root/panel.token
```

Новый этап `xray-core-26.6.27` скачает официальный Xray `26.6.27`, проверит SHA-256,
обновит Compose и пересоздаст контейнер ноды. После этого обязательно проверьте:

```bash
docker exec remnanode /usr/local/bin/rw-core version
```

## 10. Полезные команды

Статус:

```bash
remnanode status
```

Логи ноды:

```bash
remnanode logs node
```

Перезапуск:

```bash
remnanode restart
```

Повторная проверка Xray:

```bash
docker exec remnanode /usr/local/bin/rw-core version
```

## Важные замечания

- Не вставляйте API-токен непосредственно в команду запуска: он попадёт в shell
  history и может быть виден через `ps`.
- Не публикуйте `/root/panel.token`, `/opt/remnanode/.env` и содержимое
  `/opt/remnawave-node/state`.
- Не включайте Cloudflare Proxy для Reality/selfsteal-домена.
- Не создавайте в панели настройку custom core для этой ноды: установщик намеренно
  проверяет, что реально запущен закреплённый `rw-core 26.6.27`.
- Torrent Blocker включён по умолчанию. Для осознанного отключения используйте
  `--no-torrent-blocker`; обычный Xray blackhole не заменяет source-IP блокировку
  штатного plugin через webhook + nftables.
- Порт `443/tcp` должен быть доступен клиентам; `80/tcp` нужен для выпуска
  сертификата; `NODE_PORT` должен быть доступен только серверу панели.
