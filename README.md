# Digitador Automático para Linux

Digita um texto tecla por tecla no campo que estiver em foco, sem usar Ctrl+V
nem a área de transferência. Funciona em Wayland e X11, em qualquer
distribuição Linux (Debian/Ubuntu, Fedora, Arch, openSUSE e outras).

## Destaques

- Teclado virtual via `/dev/uinput`, compatível com qualquer compositor
- Detecta o layout ativo (KDE, GNOME, COSMIC, Hyprland, X11) e digita
  acentos, `ç` e símbolos corretamente, inclusive com teclas mortas
- Interface gráfica em Qt 6 e modo texto pela linha de comando
- Velocidade, pausa entre linhas e contagem inicial configuráveis
- Parada imediata por botão, bandeja ou atalho global (`digitador --parar`)
- Instalador que detecta a distro e só instala o que falta

## Instalação

    bash instalar.sh

Desenvolvido por **Mulari**
