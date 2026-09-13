# Painel do PC

Dashboard de monitoramento que abre em tela cheia no monitor virtual `DP-0`, transmitido pro tablet por uma segunda instância do Sunshine (Moonlight).

- `server.py` — servidor local em `127.0.0.1:8787` (`/api/stats` com os dados do PC, `POST /api/run/<id>` pros atalhos)
- `index.html` — a tela do painel
- `abrir-painel.sh` — sobe o serviço e abre o Chrome em modo app no `DP-0`
- `atalhos.exemplo.json` — modelo dos atalhos; o arquivo usado de verdade é `~/.config/pc-dashboard/atalhos.json`

## Instalação

```ini
# ~/.config/systemd/user/pc-dashboard.service
[Unit]
Description=Painel do PC (servidor local 127.0.0.1:8787)

[Service]
ExecStart=/usr/bin/python3 /mnt/SSD/git-projeto/pc-dashboard/server.py
KillMode=process
Restart=on-failure
RestartSec=3s
```

Pra abrir sozinho ao entrar na sessão, `~/.config/autostart/pc-dashboard.desktop` roda `sh -c "sleep 15; bash /mnt/SSD/git-projeto/pc-dashboard/abrir-painel.sh"`.

Depende de `google-chrome`, `wmctrl`, `xrandr` e `curl`.
