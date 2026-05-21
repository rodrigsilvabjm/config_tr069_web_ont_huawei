# Configurador TR-069 para ONT Huawei

Versao atual: `1.2.1`

Automacao local para acessar a interface web de uma ONT Huawei, navegar ate a tela de TR-069/ACS e configurar os dados do servidor ACS.

> Use somente em ONTs suas ou em equipamentos onde voce tem autorizacao administrativa.

## O que ele faz

- Abre a interface da ONT pela URL ou IP informado.
- Se aparecer o aviso de certificado do Chrome, clica em `Avancadas` e `Prosseguir`.
- Faz login com usuario e senha da ONT.
- Detecta o modelo da ONT e escolhe o perfil correto.
- Vai ate a tela TR-069 conforme o perfil do modelo detectado.
- Habilita ACS Management e Periodic Informing.
- Preenche ACS URL, usuario, senha, Connection Request User/Password e DSCP.
- Clica em `Apply`.
- Desloga da ONT apos aplicar a configuracao.

## Modelos suportados

- `HG8245W5-6T`: fluxo ja validado na interface nova.
- `HG8245Q2`: fluxo da interface antiga em `System Tools > TR-069`.

Se um modelo novo aparecer como `UNKNOWN` no painel, envie print da tela TR-069 e o sistema pode ganhar um novo perfil.

## Instalar

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

Em Ubuntu 20.04, o Python padrao costuma ser 3.8. Por isso o projeto fixa `playwright==1.48.0`, que e compativel com essa versao.

## Configurar

Copie `config.example.json` para `config.json` e ajuste os valores:

```powershell
Copy-Item config.example.json config.json
notepad config.json
```

Para ONTs que abrem com aviso de certificado, informe a URL completa no campo `ont.url`, por exemplo:

```json
"url": "https://10.100.207.202:80"
```

Quando `ont.url` estiver preenchido, ele tem prioridade sobre `ont.ip`.

Para executar em varias ONTs, use `ont.targets`:

```json
"ont": {
  "protocol": "https",
  "port": 80,
  "targets": [
    "192.168.88.1",
    "192.168.88.10-192.168.88.20",
    "192.168.89.0/30",
    "https://10.100.207.202:80"
  ],
  "username": "telecomadmin",
  "password": "SENHA_DA_ONT"
}
```

Formatos aceitos:

- IP unico: `192.168.88.1`
- Range abreviado: `192.168.88.10-20`
- Range completo: `192.168.88.10-192.168.88.20`
- CIDR: `192.168.88.0/24`
- URL completa: `https://10.100.207.202:80`

No final, o script imprime uma tabela com `SUCESSO` e `ERRO` para cada ONT. Em caso de erro, ele salva um print e HTML em `debug/` com o IP no nome do arquivo.

## Executar

```powershell
python .\ont_tr069_configurator.py --config .\config.json
```

Para ver o navegador trabalhando:

```powershell
python .\ont_tr069_configurator.py --config .\config.json --headed
```

Para simular sem clicar em `Apply`:

```powershell
python .\ont_tr069_configurator.py --config .\config.json --dry-run --headed
```

Para este firmware, o caminho direto da tela TR-069 e:

```json
"tr069_path": "/html/ssmp/tr069/tr069.asp"
```

Com esse caminho o script evita a tela `The requested URL was not found on this server.` e executa mais rapido. O diagnostico em execucao bem-sucedida fica desligado por padrao com `"save_success_debug": false`.

Se parar na tela de login, o script salva arquivos de diagnostico em `debug/`, como:

- `debug/login_fields_not_found.png`
- `debug/login_fields_not_found.html`
- `debug/login_still_on_screen.png`
- `debug/login_still_on_screen.html`

## Observacoes importantes

- A interface da Huawei muda um pouco entre firmwares. Se algum campo nao for encontrado, rode com `--headed` e me envie o print/HTML da tela onde parou.
- Alguns provedores bloqueiam alteracoes de TR-069 mesmo com usuario admin. Nesse caso o script consegue preencher, mas a ONT pode recusar salvar.
- O servidor ACS/TR-069 precisa estar preparado para provisionar IPv6 depois que a ONT se registrar nele. Este software configura a ONT para falar com o ACS; a regra de IPv6 fica no servidor ACS.

## Painel web em Debian/Ubuntu Server

O arquivo `web_server.py` sobe um painel web para cadastrar IPs/ranges, executar os lotes e ver o grafico de sucesso/erro.
No historico, cada lote pode ser exportado em XLS com abas `Resumo`, `Sucessos` e `Erros`. Tambem existe um botao para exportar o historico completo em XLS.

Instalacao basica:

```bash
sudo apt update
sudo apt install -y python3 python3-venv
cd /opt
sudo mkdir ont-tr069
sudo chown "$USER":"$USER" ont-tr069
cd ont-tr069
```

Envie os arquivos deste projeto para `/opt/ont-tr069` e rode:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

Subir o painel:

```bash
python web_server.py --host 0.0.0.0 --port 8080
```

Acesse no navegador:

```text
http://IP_DO_SERVIDOR:8080
```

Recomendado usar firewall/VPN ou proxy reverso com HTTPS, porque o painel manipula senhas de ONT e ACS.

Servico systemd opcional:

```ini
[Unit]
Description=Painel TR-069 ONT
After=network.target

[Service]
WorkingDirectory=/opt/ont-tr069
ExecStart=/opt/ont-tr069/.venv/bin/python /opt/ont-tr069/web_server.py --host 0.0.0.0 --port 8080
Restart=always
User=SEU_USUARIO

[Install]
WantedBy=multi-user.target
```

## Instalar via GitHub com um comando

Depois de publicar este projeto no GitHub, o servidor pode instalar tudo com:

```bash
curl -fsSL https://raw.githubusercontent.com/USUARIO/REPO/main/install.sh | sudo bash -s -- --repo https://github.com/USUARIO/REPO.git --port 8080
```

O instalador faz:

- instala dependencias do Debian/Ubuntu;
- clona ou atualiza o repositorio em `/opt/ont-tr069`;
- cria `.venv`;
- instala `requirements.txt`;
- instala Chromium/Playwright;
- cria `config.json` se ainda nao existir;
- cria e inicia o servico `ont-tr069-web.service`.

Mais detalhes em `docs/INSTALL_SERVER.md`.

## Versoes e rollback

Antes de publicar a versao 1.2, crie uma tag da versao atual estavel:

```bash
git tag v1.1.0
git push origin v1.1.0
```

Depois de publicar esta versao:

```bash
git tag v1.2.1
git push origin v1.2.1
```

Para instalar uma versao especifica no servidor:

```bash
sudo bash install.sh --repo https://github.com/USUARIO/REPO.git --ref v1.2.1 --port 8080
```

Para voltar para a 1.1:

```bash
sudo bash install.sh --repo https://github.com/USUARIO/REPO.git --ref v1.1.0 --port 8080
```
