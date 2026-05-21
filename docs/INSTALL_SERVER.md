# Instalacao em Debian/Ubuntu Server

Depois de subir este projeto para o GitHub, instale em um servidor com um comando:

```bash
curl -fsSL https://raw.githubusercontent.com/USUARIO/REPO/main/install.sh | sudo bash -s -- --repo https://github.com/USUARIO/REPO.git --port 8080
```

Depois acesse:

```text
http://IP_DO_SERVIDOR:8080
```

O painel nao usa token por padrao. Proteja o acesso com firewall, VPN ou proxy reverso com HTTPS.

## Atualizar

No servidor:

```bash
cd /opt/ont-tr069
sudo git pull
sudo systemctl restart ont-tr069-web.service
```

Ou rode novamente o instalador com a mesma URL do repositorio.

## Instalar versao especifica

```bash
sudo bash install.sh --repo https://github.com/USUARIO/REPO.git --ref v1.2.0 --port 8080
```

Rollback:

```bash
sudo bash install.sh --repo https://github.com/USUARIO/REPO.git --ref v1.1.0 --port 8080
```

## Logs

```bash
sudo systemctl status ont-tr069-web.service
sudo journalctl -u ont-tr069-web.service -f
```

## Arquivos importantes

- Aplicacao: `/opt/ont-tr069`
- Configuracao local: `/opt/ont-tr069/config.json`
- Historico: `/opt/ont-tr069/web_data/history.json`
- Servico: `/etc/systemd/system/ont-tr069-web.service`

## Seguranca

O painel manipula senhas de ONT e ACS. Use firewall, VPN ou proxy reverso com HTTPS. Nao exponha a porta 8080 diretamente para a internet.
