# Painel do PC

Dashboard de monitoramento que abre em tela cheia no monitor virtual `DP-0`, transmitido pro tablet por uma segunda instância do Sunshine (Moonlight).

- `server.py` — servidor local em `127.0.0.1:8787` (`/api/stats` com os dados do PC e a música atual via MPRIS, `/api/audio` com o áudio do PC em PCM pro visualizador, `POST /api/run/<id>` pros atalhos)
- `index.html` — a tela do painel: página de monitoramento com visualizador de áudio, página de redes sociais e página de atalhos (botão discreto no canto inferior direito; volta sozinha pro painel depois de 90 s), mais o texugo pixelado que passeia por cima dos cards, dança com a música, dorme quando fica parado e sua quando o PC esquenta
- `redes.py` — contadores do YouTube (Data API v3) e da Twitch (Helix), com histórico diário pro "+N hoje" e a tendência de 30 dias. Instagram e TikTok ainda não estão integrados. As credenciais ficam em `~/.config/pc-dashboard/redes.json` (fora do git) e o histórico em `~/.config/pc-dashboard/redes-historico.json`
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

Depende de `google-chrome`, `wmctrl`, `xrandr`, `curl`, `parec` (PulseAudio/PipeWire) e `busctl` (systemd).

O painel se recarrega sozinho quando o `index.html` muda. Mudanças no `server.py` ou no `redes.py` precisam de `systemctl --user restart pc-dashboard.service`.
