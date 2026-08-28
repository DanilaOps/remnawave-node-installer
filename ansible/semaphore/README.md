# Semaphore UI

Semaphore запускает `ansible/playbooks/provision_node.yml` прямо из корня клона. Корневой `ansible.cfg` нужен именно для этого режима; консольные команды по-прежнему работают из каталога `ansible` через вложенный конфиг.

Сервис из `semaphore.service` работает от отдельного пользователя `semaphore` и слушает только `127.0.0.1:3000`. Открывать UI следует через SSH-туннель:

```bash
ssh -L 3000:127.0.0.1:3000 root@CONTROLLER_IP
```

В проекте Semaphore указываются репозиторий `https://github.com/DanilaOps/remnawave-node-installer.git`, нужная ветка и playbook `ansible/playbooks/provision_node.yml`. Production inventory хранится в Semaphore как Static YAML, постоянные API-токены — как secrets в Variable Group, а корневой пароль новой VPS вводится в secret survey field только для первого bootstrap и не сохраняется в inventory.

Контроллер оставляет SSH снаружи, поэтому рядом лежит минимальный jail fail2ban для `sshd`. Он не заменяет переход на ключи и ограничение SSH по доверенным адресам, но блокирует обычный перебор паролей.
