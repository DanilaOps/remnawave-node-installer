# Semaphore UI

Semaphore запускает `ansible/playbooks/provision_node.yml` прямо из корня клона. Корневой `ansible.cfg` нужен именно для этого режима; консольные команды по-прежнему работают из каталога `ansible` через вложенный конфиг.

Сервис из `semaphore.service` работает от отдельного пользователя `semaphore` и слушает только `127.0.0.1:3000`. Открывать UI следует через SSH-туннель:

```bash
ssh -L 3000:127.0.0.1:3000 root@CONTROLLER_IP
```

В проекте Semaphore указываются репозиторий `https://github.com/DanilaOps/remnawave-node-installer.git`, нужная ветка и playbook `ansible/playbooks/provision_node.yml`. Production inventory хранится в Semaphore как Static YAML, пароль Ansible Vault — в Key Store, а корневой пароль новой VPS передаётся как зашифрованная host variable только на время первичного bootstrap.
