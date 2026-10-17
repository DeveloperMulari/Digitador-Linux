#!/usr/bin/env bash
set -euo pipefail

export PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/digitador-linux"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
RULE="/etc/udev/rules.d/60-digitador-uinput.rules"
MODLOAD="/etc/modules-load.d/digitador-uinput.conf"
GROUP="digitador-uinput"
LOGIN_USER="$(id -un)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*" >&2; }

if [ "$(id -u)" -eq 0 ]; then
    warn "Rode como usuário normal (o script usa sudo/doas apenas onde precisa)."
    exit 1
fi

as_root() {
    if command -v sudo >/dev/null 2>&1; then sudo "$@"
    elif command -v doas >/dev/null 2>&1; then doas "$@"
    else
        warn "sudo/doas não encontrados; usando 'su' (digite a senha do root)."
        su -c "$(printf '%q ' "$@")"
    fi
}

uinput_node() {
    for p in /dev/uinput /dev/input/uinput; do [ -e "$p" ] && { echo "$p"; return 0; }; done
    return 1
}

if [ "${1:-}" = "--desinstalar" ]; then
    info "Removendo o programa do seu usuário…"
    rm -rf "$DEST" "$BIN/digitador" "$APPS/digitador-linux.desktop"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true
    ok "Programa removido."
    read -r -p "Remover também a regra do uinput (volta a exigir root)? [s/N] " r || r=""
    r="$(printf '%s' "$r" | tr '[:upper:]' '[:lower:]')"
    if [ "$r" = "s" ] || [ "$r" = "sim" ]; then
        as_root rm -f "$RULE" "$MODLOAD"
        if getent group "$GROUP" >/dev/null 2>&1; then
            as_root gpasswd -d "$LOGIN_USER" "$GROUP" >/dev/null 2>&1 \
                || as_root delgroup "$LOGIN_USER" "$GROUP" >/dev/null 2>&1 || true
            as_root groupdel "$GROUP" >/dev/null 2>&1 || as_root delgroup "$GROUP" >/dev/null 2>&1 || true
        fi
        if command -v udevadm >/dev/null 2>&1; then
            as_root udevadm control --reload-rules || true
            as_root udevadm trigger --subsystem-match=misc --action=change || true
        fi
        ok "Regra removida. Pode ser necessário reiniciar para descarregar o módulo uinput."
    fi
    exit 0
fi

[ -f "$DIR/digitador.py" ] || { warn "digitador.py precisa estar na mesma pasta do instalar.sh."; exit 1; }
if [ "$(uname -s)" != "Linux" ]; then warn "Este programa só funciona no Linux."; exit 1; fi
case "${XDG_SESSION_TYPE:-}" in
    wayland|x11) ;;
    *) warn "Sessão gráfica não detectada (XDG_SESSION_TYPE='${XDG_SESSION_TYPE:-}'). Tudo bem se você estiver instalando por terminal/SSH." ;;
esac

info "1/4  Verificando dependências (Python 3.8+, Qt 6 para Python, libxkbcommon)…"

PM=""
for c in apt-get dnf yum pacman zypper apk xbps-install; do
    command -v "$c" >/dev/null 2>&1 && { PM="$c"; break; }
done

py_ok()  { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; }
qt_ok()  { local py="${1:-python3}"
           "$py" -c 'import PySide6.QtWidgets' 2>/dev/null || "$py" -c 'import PyQt6.QtWidgets' 2>/dev/null; }
xkb_ok() { python3 - <<'PY' 2>/dev/null
import ctypes, ctypes.util, glob, sys
names = [ctypes.util.find_library("xkbcommon"), "libxkbcommon.so.0", "libxkbcommon.so"]
names += glob.glob("/usr/lib*/libxkbcommon.so.0") + glob.glob("/usr/lib/*/libxkbcommon.so.0") + glob.glob("/lib*/libxkbcommon.so.0")
for n in filter(None, names):
    try:
        ctypes.CDLL(n); sys.exit(0)
    except OSError:
        pass
sys.exit(1)
PY
}

pkg_install() {
    case "$PM" in
        apt-get) as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
        dnf)     as_root dnf install -y "$@" ;;
        yum)     as_root yum install -y "$@" ;;
        pacman)  as_root pacman -S --needed --noconfirm "$@" ;;
        zypper)  as_root zypper --non-interactive install "$@" ;;
        apk)     as_root apk add "$@" ;;
        xbps-install) as_root xbps-install -y "$@" ;;
        *) return 1 ;;
    esac
}

BASE_PKGS=""; QT_CANDIDATES=()
case "$PM" in
    apt-get) BASE_PKGS="python3 libxkbcommon0 xkb-data"
             QT_CANDIDATES=("python3-pyside6.qtwidgets python3-pyside6.qtgui python3-pyside6.qtcore" "python3-pyqt6") ;;
    dnf|yum) BASE_PKGS="python3 libxkbcommon xkeyboard-config"
             QT_CANDIDATES=("python3-pyside6" "python3-pyqt6") ;;
    pacman)  BASE_PKGS="python libxkbcommon xkeyboard-config"
             QT_CANDIDATES=("pyside6" "python-pyqt6") ;;
    zypper)  BASE_PKGS="python3 libxkbcommon0 xkeyboard-config"
             QT_CANDIDATES=("python3-pyside6" "python3-qt6" "python3-PyQt6") ;;
    apk)     BASE_PKGS="python3 libxkbcommon xkeyboard-config"
             QT_CANDIDATES=("py3-pyside6" "py3-qt6") ;;
    xbps-install) BASE_PKGS="python3 libxkbcommon xkeyboard-config"
             QT_CANDIDATES=("python3-PySide6" "python3-PyQt6") ;;
esac

if ! py_ok || ! xkb_ok; then
    if [ -n "$PM" ]; then
        info "Instalando base ($BASE_PKGS) com $PM…"
        [ "$PM" = "apt-get" ] && { as_root apt-get update || warn "apt-get update falhou; tentando instalar mesmo assim."; }
        pkg_install $BASE_PKGS || warn "Não consegui instalar os pacotes base automaticamente."
    fi
    py_ok  || { warn "Python 3.8 ou mais novo não encontrado. Instale-o e rode o instalador de novo."; exit 1; }
    xkb_ok || warn "libxkbcommon não foi encontrada: instale-a pelo gerenciador de pacotes da sua distro (pacote 'libxkbcommon' / 'libxkbcommon0')."
fi
ok "Python $(python3 -c 'import platform; print(platform.python_version())')"

USE_VENV=0
if ! qt_ok; then
    if [ -n "$PM" ]; then
        for cand in "${QT_CANDIDATES[@]}"; do
            info "Tentando instalar o Qt 6 para Python: $cand"
            pkg_install $cand >/dev/null 2>&1 || true
            qt_ok && break
        done
    fi
fi
if ! qt_ok; then
    info "Pacote da distro indisponível; criando ambiente virtual com PySide6 (pip)…"
    mkdir -p "$DEST"
    if ! python3 -m venv --system-site-packages "$DEST/venv" >/dev/null 2>&1; then
        [ "$PM" = "apt-get" ] && pkg_install python3-venv python3-pip >/dev/null 2>&1 || true
        rm -rf "$DEST/venv"
        python3 -m venv --system-site-packages "$DEST/venv" >/dev/null 2>&1 || warn "Não consegui criar o ambiente virtual (falta o módulo venv do Python)."
    fi
    if [ -x "$DEST/venv/bin/python" ]; then
        "$DEST/venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
        if "$DEST/venv/bin/python" -m pip install --quiet PySide6-Essentials 2>/dev/null \
           || "$DEST/venv/bin/python" -m pip install --quiet PyQt6 2>/dev/null; then
            USE_VENV=1
        fi
    fi
fi
if qt_ok || { [ "$USE_VENV" = 1 ] && qt_ok "$DEST/venv/bin/python"; }; then
    ok "Qt 6 para Python disponível$([ "$USE_VENV" = 1 ] && echo ' (ambiente virtual)')."
    if [ "$USE_VENV" = 1 ] && [ "$PM" = "apt-get" ]; then
        pkg_install libxcb-cursor0 >/dev/null 2>&1 || true
    fi
else
    warn "Não foi possível instalar o Qt 6 para Python (comum em musl/Alpine ou arquiteturas sem wheels)."
    warn "A interface gráfica não abrirá, mas o modo texto funciona:  digitador -f arquivo.txt"
fi

info "2/4  Liberando o teclado virtual (uinput) para o seu usuário…"

as_root modprobe uinput 2>/dev/null || true
if [ -d /etc/modules-load.d ] || [ -d /run/systemd/system ]; then
    printf 'uinput\n' > "$TMP/mod.conf"
    as_root install -Dm644 "$TMP/mod.conf" "$MODLOAD"
elif [ -f /etc/modules ]; then
    grep -qx uinput /etc/modules 2>/dev/null || as_root sh -c 'echo uinput >> /etc/modules'
else
    warn "Não sei como carregar o módulo uinput automaticamente no boot nesta distro; se faltar, rode: sudo modprobe uinput"
fi

if ! getent group "$GROUP" >/dev/null 2>&1; then
    as_root groupadd --system "$GROUP" 2>/dev/null || as_root groupadd -r "$GROUP" 2>/dev/null \
        || as_root addgroup -S "$GROUP"
fi
as_root usermod -aG "$GROUP" "$LOGIN_USER" 2>/dev/null || as_root gpasswd -a "$LOGIN_USER" "$GROUP" >/dev/null 2>&1 \
    || as_root addgroup "$LOGIN_USER" "$GROUP"

cat > "$TMP/rule" <<EOF
KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess", GROUP="$GROUP", MODE="0660"
EOF
as_root install -Dm644 "$TMP/rule" "$RULE"
if command -v udevadm >/dev/null 2>&1; then
    as_root udevadm control --reload-rules || true
    as_root udevadm trigger --subsystem-match=misc --action=add || true
    as_root udevadm settle || true
else
    warn "udevadm não encontrado: esta distro pode usar outro gerenciador de dispositivos (mdev, devd…)."
    warn "Se necessário, dê acesso de escrita ao uinput ao grupo $GROUP manualmente."
fi
for _ in 1 2 3 4 5 6; do
    n="$(uinput_node || true)"
    [ -n "$n" ] && [ -w "$n" ] && break
    sleep 0.5
done
n="$(uinput_node || true)"
if [ -n "$n" ] && [ -w "$n" ]; then
    ok "$n liberado para $LOGIN_USER."
elif [ -z "$n" ]; then
    warn "O dispositivo uinput não existe: o kernel desta máquina pode não ter o módulo uinput."
else
    warn "$n ainda não está gravável nesta sessão. Sem problema: o programa usa o grupo $GROUP automaticamente (via 'sg');"
    warn "se mesmo assim reclamar de permissão, faça logout/login. Não adicione seu usuário ao grupo genérico 'input'."
fi

info "3/4  Instalando o programa em $DEST …"
install -Dm755 "$DIR/digitador.py" "$DEST/digitador.py"
mkdir -p "$BIN" "$APPS"
cat > "$BIN/digitador" <<EOF
#!/bin/sh
PY=python3
[ -x "$DEST/venv/bin/python" ] && PY="$DEST/venv/bin/python"
exec "\$PY" "$DEST/digitador.py" "\$@"
EOF
chmod +x "$BIN/digitador"
cat > "$APPS/digitador-linux.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Digitador Automático
GenericName=Digitação automática
Comment=Digita um texto tecla por tecla (Wayland ou X11)
Exec="$BIN/digitador"
TryExec=$BIN/digitador
Icon=input-keyboard
Terminal=false
Categories=Utility;
Keywords=digitar;teclado;texto;
StartupWMClass=digitador-linux
Actions=Parar;

[Desktop Action Parar]
Name=Parar digitação
Exec="$BIN/digitador" --parar
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true
for k in kbuildsycoca6 kbuildsycoca5; do command -v "$k" >/dev/null 2>&1 && { "$k" >/dev/null 2>&1 || true; break; }; done
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) warn "$BIN não está no seu PATH; use o menu de aplicativos ou rode $BIN/digitador." ;;
esac

info "4/4  Diagnóstico:"
"$BIN/digitador" --diagnostico || true

cat <<EOF

Instalação concluída. Abra "Digitador Automático" no menu de aplicativos (ou rode: $BIN/digitador).
Não execute o programa como root nem com sudo.

Atalho global para PARAR de qualquer lugar (recomendado) — crie nas configurações de teclado do seu desktop:
  Comando: $BIN/digitador --parar      Atalho: por exemplo Ctrl+Alt+P
  (KDE: Configurações → Teclado → Atalhos · GNOME: Configurações → Teclado → Atalhos personalizados
   Xfce: Teclado → Atalhos de aplicativos · Sway/Hyprland: um bind no arquivo de configuração)

Se o diagnóstico acusou falta de permissão no uinput, faça logout/login uma vez.
Para remover: bash instalar.sh --desinstalar
EOF
