# Digitador Automático para Linux

Versão **1.1.0** — feito para funcionar nas principais distribuições Linux, em sessões **Wayland ou X11**.

Testado em **CachyOS** (Arch) e **Nobara** (Fedora). As demais distros e desktops abaixo são suportados pelo
instalador e pela detecção de layout, mas ainda não foram validados.

## Arquivos

- `digitador.py`: aplicação Python/Qt e modo de diagnóstico.
- `instalar.sh`: instala dependências, cria o atalho e configura o acesso ao `uinput`.

Mantenha os dois arquivos na mesma pasta.

## Instalar

```bash
bash instalar.sh
```

O instalador detecta a distribuição sozinho e **só instala o que estiver faltando**. Distribuições compatíveis:

| Gerenciador | Distribuições (exemplos) |
|---|---|
| `apt-get` | Debian, Ubuntu, Linux Mint, Pop!_OS, Zorin, Devuan |
| `dnf` / `yum` | Fedora, Nobara, RHEL/Rocky/Alma |
| `pacman` | Arch, Manjaro, EndeavourOS, SteamOS |
| `zypper` | openSUSE Leap/Tumbleweed |
| `apk` | Alpine (melhor esforço) |
| `xbps-install` | Void |

Para o Qt 6 ele tenta, nesta ordem: pacote PySide6 da distro → PyQt6 da distro → **ambiente virtual com PySide6 via pip**
(em `~/.local/share/digitador-linux/venv`, sem tocar no Python do sistema). Se nada disso for possível
(ex.: musl/Alpine sem pacote), a interface gráfica não abre, mas o **modo texto continua funcionando**.
Em outras distros (Gentoo, NixOS…), instale manualmente `python3`, `libxkbcommon`, `xkeyboard-config` e PySide6 ou PyQt6,
depois rode o `instalar.sh` só para configurar o `uinput` e o atalho.

Usa `sudo`, `doas` ou `su` (o que existir) somente para instalar pacotes e criar a regra do dispositivo.
Nunca execute o programa como root.

### Permissão do teclado virtual (`uinput`)

A regra udev dá acesso (a) ao usuário da **sessão ativa** (`uaccess`, imediato em sistemas com logind) e
(b) aos membros do grupo dedicado `digitador-uinput` (não usa o grupo genérico `input`).
Se o grupo ainda não valer na sessão atual, o programa se reabre automaticamente via `sg`, sem exigir logout.
Se mesmo assim houver erro de permissão, faça logout/login uma vez.

## Diagnóstico

```bash
~/.local/bin/digitador --diagnostico            # layout detectado automaticamente
~/.local/bin/digitador --diagnostico --layout br
```

Retorna erro apenas se não houver como injetar teclas. Substituições de caracteres (ex.: `ç` num layout `us`) aparecem
como aviso, pois são esperadas em layouts sem acentos. O diagnóstico não substitui o botão **Testar layout** do app.

## Layout do teclado

O programa descobre o layout ativo, em ordem, por: KDE (`kxkbrc`) → GNOME (`gsettings`) → COSMIC → Hyprland →
variáveis `XKB_DEFAULT_*` → `setxkbmap` → `localectl` → `/etc/default/keyboard` / `xorg.conf.d`.
Se detectar errado (ou usar Sway/outros), informe manualmente no campo **Layout do teclado** (ex.: `br`, `us(intl)`, `de`)
ou com `--layout`. Ele também usa o layout correto se você alternar entre vários (KDE/GNOME).

## Primeiro teste

1. Abra pelo menu ou execute `~/.local/bin/digitador`.
2. Use **Testar layout** e confira se o campo de teste recebeu o texto esperado (`ação`, `coração`, símbolos…).
3. Faça um teste curto num editor local antes de usar em um site.
4. Para interromper: botão **Parar**, ícone da bandeja (se o desktop tiver) ou um atalho global para
   `~/.local/bin/digitador --parar`. No GNOME sem extensão de bandeja, o atalho global é o caminho recomendado.

## Modo texto (sem interface gráfica)

```bash
digitador -f texto.txt --layout br --inicio-s 5
echo "olá" | digitador -f - --dry-run
```

## Desinstalar

```bash
bash instalar.sh --desinstalar
```

Remove os arquivos do usuário e, se você confirmar, a regra udev e o grupo dedicado.

## Requisitos mínimos

Python 3.8+, `libxkbcommon` + dados XKB (`xkeyboard-config`/`xkb-data`), kernel com `uinput`
(módulo ou embutido), e PySide6/PyQt6 para a interface gráfica. Pensado para x86_64 e aarch64.

## Limitações

- O envio de teclas usa um teclado virtual do kernel; o layout detectado precisa coincidir com o ativo no compositor.
- Layouts não latinos (ex.: `ru`) só digitam o que o layout produz; para texto latino use um layout latino ativo.
- Com `uinput`, qualquer processo do seu usuário pode, em tese, criar teclados virtuais; use apenas software confiável.
- Distros imutáveis (Silverblue/Bazzite/SteamOS): o instalador usa o ambiente virtual em vez de instalar pacotes do sistema.

## Créditos

Desenvolvido por **Mulari**

- Discord: kks_mulari
- Instagram: [kks_mulari](https://www.instagram.com/kks_mulari/)
