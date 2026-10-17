#!/usr/bin/env python3

from __future__ import annotations

import argparse
import configparser
import ctypes
import ctypes.util
import fcntl
import glob
import html
import os
import platform
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import unicodedata
try:
    import grp
    import pwd
except ImportError:
    grp = pwd = None
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

APP_TITLE = "Digitador Automático (Linux)"
APP_ID = "digitador-linux"
VERSION = "1.1.0"
UINPUT_GROUP = "digitador-uinput"


def _runtime_dir() -> str:
    d = os.environ.get("XDG_RUNTIME_DIR")
    if d and os.path.isdir(d) and os.access(d, os.W_OK):
        return d
    d = os.path.join("/tmp", f"digitador-{os.getuid()}")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        if os.stat(d).st_uid != os.getuid():
            raise OSError("diretório pertence a outro usuário")
        return d
    except OSError:
        import tempfile
        return tempfile.mkdtemp(prefix="digitador-")


_RUNTIME_DIR = _runtime_dir()
STOP_FLAG = os.path.join(_RUNTIME_DIR, "digitador-linux.stop")


TEST_SAMPLE = "Teste: ação, coração, órgão, é à ü ê õ ç Ç, ~ ^ ' \" ` , R$ 1.234,56 (50%) @#&*_+=[]{}<>/\\|;:?!"


KEY_TAB, KEY_ENTER, KEY_LEFTSHIFT, KEY_SPACE = 15, 28, 42, 57


MAIN_KEYS: List[int] = (list(range(2, 14)) + list(range(16, 28)) + list(range(30, 42))
                        + [43] + list(range(44, 54)) + [57, 86, 89])

XKB_KEY_DOWN = 1
COMPOSE_COMPOSING, COMPOSE_COMPOSED, COMPOSE_CANCELLED = 1, 2, 3
_DEAD_RANGE = range(0xFE50, 0xFE90)
_SHIFT_SYMS = {0xFFE1, 0xFFE2}
_ALTGR_SYMS = {0xFE03, 0xFF7E}


_DEAD_COMBINING = {0xFE50: "\u0300", 0xFE51: "\u0301", 0xFE52: "\u0302", 0xFE53: "\u0303",
                   0xFE54: "\u0304", 0xFE55: "\u0306", 0xFE56: "\u0307", 0xFE57: "\u0308",
                   0xFE58: "\u030a", 0xFE59: "\u030b", 0xFE5A: "\u030c", 0xFE5B: "\u0327",
                   0xFE5C: "\u0328"}
_DEAD_SPACING = {0xFE50: "`", 0xFE52: "^", 0xFE53: "~", 0xFE57: "\u00a8", 0xFE5B: "\u00b8"}

Stroke = Tuple[Tuple[int, ...], int]


_PKG_HINTS = {
    "apt-get": ("sudo apt install", "python3-pyside6.qtwidgets  (ou python3-pyqt6)", "libxkbcommon0 xkb-data", "ydotool"),
    "dnf": ("sudo dnf install", "python3-pyside6", "libxkbcommon xkeyboard-config", "ydotool"),
    "yum": ("sudo yum install", "python3-pyside6", "libxkbcommon xkeyboard-config", "ydotool"),
    "pacman": ("sudo pacman -S", "pyside6  (ou python-pyqt6)", "libxkbcommon xkeyboard-config", "ydotool"),
    "zypper": ("sudo zypper install", "python3-pyside6  (ou python3-qt6)", "libxkbcommon0 xkeyboard-config", "ydotool"),
    "apk": ("doas apk add", "py3-pyside6  (ou py3-qt6)", "libxkbcommon xkeyboard-config", "ydotool"),
    "xbps-install": ("sudo xbps-install -S", "python3-PySide6  (ou python3-PyQt6)", "libxkbcommon xkeyboard-config", "ydotool"),
    "emerge": ("sudo emerge", "dev-python/pyside6  (ou dev-python/PyQt6)", "x11-libs/libxkbcommon x11-misc/xkeyboard-config", "gui-apps/ydotool"),
}


def install_hint(kind: str) -> str:
    idx = {"qt": 1, "xkb": 2, "ydotool": 3}[kind]
    for pm, data in _PKG_HINTS.items():
        if shutil.which(pm):
            return f"{data[0]} {data[idx]}"
    names = {"qt": "PySide6 (ou PyQt6)", "xkb": "libxkbcommon e xkeyboard-config", "ydotool": "ydotool"}
    return f"instale {names[kind]} pelo gerenciador de pacotes da sua distribuição"


class XkbError(RuntimeError):
    pass


class _RuleNames(ctypes.Structure):
    _fields_ = [("rules", ctypes.c_char_p), ("model", ctypes.c_char_p),
                ("layout", ctypes.c_char_p), ("variant", ctypes.c_char_p),
                ("options", ctypes.c_char_p)]


_xkb = None


def _lib():
    global _xkb
    if _xkb is not None:
        return _xkb
    cands: List[str] = []
    found = ctypes.util.find_library("xkbcommon")
    if found:
        cands.append(found)
    cands += ["libxkbcommon.so.0", "libxkbcommon.so"]
    for pattern in ("/usr/lib*/libxkbcommon.so.0", "/usr/lib/*/libxkbcommon.so.0", "/lib*/libxkbcommon.so.0",
                    "/lib/*/libxkbcommon.so.0", "/usr/local/lib*/libxkbcommon.so.0", "/run/current-system/sw/lib/libxkbcommon.so.0"):
        cands += sorted(glob.glob(pattern))
    lib, last_exc = None, None
    for name in cands:
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError as exc:
            last_exc = exc
    if lib is None:
        raise XkbError(f"libxkbcommon não encontrada ({last_exc}). Instale: {install_hint('xkb')}")
    c = ctypes
    vp, u32, i32, cp, sz = c.c_void_p, c.c_uint32, c.c_int, c.c_char_p, c.c_size_t
    sig = {
        "xkb_context_new": (vp, [i32]),
        "xkb_context_unref": (None, [vp]),
        "xkb_context_set_log_level": (None, [vp, i32]),
        "xkb_keymap_new_from_names": (vp, [vp, c.POINTER(_RuleNames), i32]),
        "xkb_keymap_unref": (None, [vp]),
        "xkb_keymap_num_levels_for_key": (u32, [vp, u32, u32]),
        "xkb_keymap_key_get_syms_by_level": (i32, [vp, u32, u32, u32, c.POINTER(c.POINTER(u32))]),
        "xkb_state_new": (vp, [vp]),
        "xkb_state_unref": (None, [vp]),
        "xkb_state_update_key": (i32, [vp, u32, i32]),
        "xkb_state_key_get_one_sym": (u32, [vp, u32]),
        "xkb_state_key_get_utf32": (u32, [vp, u32]),
        "xkb_compose_table_new_from_locale": (vp, [vp, cp, i32]),
        "xkb_compose_table_unref": (None, [vp]),
        "xkb_compose_state_new": (vp, [vp, i32]),
        "xkb_compose_state_unref": (None, [vp]),
        "xkb_compose_state_reset": (None, [vp]),
        "xkb_compose_state_feed": (i32, [vp, u32]),
        "xkb_compose_state_get_status": (i32, [vp]),
        "xkb_compose_state_get_utf8": (i32, [vp, c.c_char_p, sz]),
    }
    for fname, (res, args) in sig.items():
        fn = getattr(lib, fname)
        fn.restype, fn.argtypes = res, args
    _xkb = lib
    return lib


def _locales() -> List[str]:
    cands = [os.environ.get("LC_ALL"), os.environ.get("LC_CTYPE"), os.environ.get("LANG"),
             "pt_BR.UTF-8", "en_US.UTF-8", "C.UTF-8"]
    out: List[str] = []
    for c in cands:
        if c and c not in out and c != "C":
            out.append(c)
    return out


@dataclass
class LayoutSpec:
    layout: str = "us"
    variant: str = ""
    model: str = "pc105"
    options: str = ""
    source: str = ""

    def label(self) -> str:
        return self.layout + (f"({self.variant})" if self.variant else "")


def parse_layout_spec(text: str) -> LayoutSpec:
    t = text.strip()
    m = re.match(r"^([A-Za-z0-9_]+)\s*(?:[(:\s]\s*([A-Za-z0-9_\-]+)\s*\)?)?$", t)
    if not m:
        raise XkbError(f"Layout inválido: {text!r} (use, por exemplo, br ou us(intl))")
    return LayoutSpec(m.group(1), m.group(2) or "", source="informado pelo usuário")


class LayoutMap:

    _cache: Dict[Tuple[str, str, str, str], "LayoutMap"] = {}

    def __init__(self, layout: str = "us", variant: str = "", model: str = "pc105", options: str = ""):
        self.layout, self.variant, self.model, self.options = layout, variant, model, options
        self.note = ""
        self.direct: Dict[str, Stroke] = {}
        self.composed: Dict[str, List[Stroke]] = {}
        self.dead: Dict[int, Stroke] = {}
        self._sym: Dict[str, int] = {}
        self.shift_key: Optional[int] = None
        self.altgr_key: Optional[int] = None
        self._ctx = self._km = self._cs = None
        self._pending_dead: Optional[int] = None
        self._open()
        self._scan()
        self._discover_composed()
        self._self_check()


    @classmethod
    def from_spec(cls, spec: LayoutSpec) -> "LayoutMap":
        key = (spec.layout, spec.variant, spec.model or "pc105", spec.options)
        if key in cls._cache:
            return cls._cache[key]
        attempts = [key, (key[0], "", key[2], key[3]), (key[0], key[1], "pc105", ""), (key[0], "", "pc105", "")]
        last: Optional[Exception] = None
        for i, att in enumerate(attempts):
            try:
                lm = cls(*att)
            except XkbError as exc:
                last = exc
                continue
            if i:
                lm.note = f"não foi possível usar {spec.label()} exatamente; usando {lm.label()}"
            cls._cache[key] = lm
            return lm
        raise XkbError(f"Layout XKB não encontrado: {spec.label()} ({last})")

    def label(self) -> str:
        return self.layout + (f"({self.variant})" if self.variant else "")

    def _open(self) -> None:
        lib = _lib()
        self._ctx = lib.xkb_context_new(0)
        if not self._ctx:
            raise XkbError("falha ao criar o contexto do libxkbcommon")
        lib.xkb_context_set_log_level(self._ctx, 10)
        refs = [(x.encode() if x else None) for x in ("evdev", self.model, self.layout, self.variant, self.options)]
        names = _RuleNames(*refs)
        self._km = lib.xkb_keymap_new_from_names(self._ctx, ctypes.byref(names), 0)
        if not self._km:
            raise XkbError(f"layout/variante desconhecidos: {self.label()} "
                           f"(se for um layout válido, os dados XKB podem estar ausentes: {install_hint('xkb')})")
        for loc in _locales():
            table = lib.xkb_compose_table_new_from_locale(self._ctx, loc.encode(), 0)
            if table:
                self._cs = lib.xkb_compose_state_new(table, 0)
                lib.xkb_compose_table_unref(table)
                break

    def close(self) -> None:
        try:
            lib = _lib()
            if self._cs:
                lib.xkb_compose_state_unref(self._cs)
            if self._km:
                lib.xkb_keymap_unref(self._km)
            if self._ctx:
                lib.xkb_context_unref(self._ctx)
        except Exception:
            pass
        self._cs = self._km = self._ctx = None

    def __del__(self) -> None:
        self.close()


    def _find_key(self, wanted: set, prefer: Sequence[int] = ()) -> Optional[int]:
        lib = _lib()
        ptr = ctypes.POINTER(ctypes.c_uint32)()

        order = list(prefer) + [e for e in range(1, 128) if e not in prefer and e != 84]
        for ev in order:
            kc = ev + 8
            if lib.xkb_keymap_num_levels_for_key(self._km, kc, 0) < 1:
                continue
            n = lib.xkb_keymap_key_get_syms_by_level(self._km, kc, 0, 0, ctypes.byref(ptr))
            for i in range(n):
                if ptr[i] in wanted:
                    return ev
        return None

    def _probe(self, mods: Tuple[int, ...], ev: int) -> Tuple[int, int]:
        lib = _lib()
        st = lib.xkb_state_new(self._km)
        try:
            for m in mods:
                lib.xkb_state_update_key(st, m + 8, XKB_KEY_DOWN)
            return lib.xkb_state_key_get_one_sym(st, ev + 8), lib.xkb_state_key_get_utf32(st, ev + 8)
        finally:
            lib.xkb_state_unref(st)

    def _scan(self) -> None:
        self.shift_key = self._find_key(_SHIFT_SYMS, (42, 54))
        self.altgr_key = self._find_key(_ALTGR_SYMS, (100,))
        combos: List[Tuple[int, ...]] = [()]
        if self.shift_key is not None:
            combos.append((self.shift_key,))
        if self.altgr_key is not None:
            combos.append((self.altgr_key,))
        if self.shift_key is not None and self.altgr_key is not None:
            combos.append((self.shift_key, self.altgr_key))
        for mods in combos:
            for ev in MAIN_KEYS:
                sym, utf = self._probe(mods, ev)
                if sym == 0:
                    continue
                if sym in _DEAD_RANGE:
                    self.dead.setdefault(sym, (mods, ev))
                    continue
                if utf < 0x20 or utf == 0x7F:
                    continue
                ch = chr(utf)
                if ch not in self.direct:
                    self.direct[ch] = (mods, ev)
                    self._sym[ch] = sym

    def _discover_composed(self) -> None:
        lib = _lib()
        bases: List[Tuple[int, Stroke]] = [(self._sym[c], s) for c, s in self.direct.items()]
        bases += [(d, s) for d, s in self.dead.items()]
        if self._cs:
            buf = ctypes.create_string_buffer(64)
            for dsym, dstroke in self.dead.items():
                for bsym, bstroke in bases:
                    lib.xkb_compose_state_reset(self._cs)
                    lib.xkb_compose_state_feed(self._cs, dsym)
                    lib.xkb_compose_state_feed(self._cs, bsym)
                    if lib.xkb_compose_state_get_status(self._cs) != COMPOSE_COMPOSED:
                        continue
                    n = lib.xkb_compose_state_get_utf8(self._cs, buf, 64)
                    s = buf.raw[:n].decode("utf-8", "replace") if n > 0 else ""
                    if len(s) == 1 and s not in self.direct and s not in self.composed:
                        self.composed[s] = [dstroke, bstroke]
            lib.xkb_compose_state_reset(self._cs)
        else:
            for dsym, dstroke in self.dead.items():
                mark = _DEAD_COMBINING.get(dsym)
                for ch, bstroke in self.direct.items():
                    if ch == " ":
                        s = _DEAD_SPACING.get(dsym, "")
                    elif mark and ch.isalpha():
                        s = unicodedata.normalize("NFC", ch + mark)
                    else:
                        continue
                    if len(s) == 1 and s != ch and s not in self.direct and s not in self.composed:
                        self.composed[s] = [dstroke, bstroke]


    def simulate(self, strokes: Sequence[Stroke]) -> str:
        lib = _lib()
        out: List[str] = []
        buf = ctypes.create_string_buffer(64)
        if self._cs:
            lib.xkb_compose_state_reset(self._cs)
        self._pending_dead = None
        for mods, ev in strokes:
            st = lib.xkb_state_new(self._km)
            try:
                for m in mods:
                    lib.xkb_state_update_key(st, m + 8, XKB_KEY_DOWN)
                sym = lib.xkb_state_key_get_one_sym(st, ev + 8)
                utf = lib.xkb_state_key_get_utf32(st, ev + 8)
            finally:
                lib.xkb_state_unref(st)
            if self._cs:
                lib.xkb_compose_state_feed(self._cs, sym)
                status = lib.xkb_compose_state_get_status(self._cs)
                if status == COMPOSE_COMPOSING:
                    continue
                if status == COMPOSE_COMPOSED:
                    n = lib.xkb_compose_state_get_utf8(self._cs, buf, 64)
                    out.append(buf.raw[:n].decode("utf-8", "replace"))
                    lib.xkb_compose_state_reset(self._cs)
                    continue
                if status == COMPOSE_CANCELLED:
                    lib.xkb_compose_state_reset(self._cs)
                    continue
            else:
                if sym in _DEAD_RANGE:
                    self._pending_dead = sym
                    continue
                if self._pending_dead is not None:
                    dsym, self._pending_dead = self._pending_dead, None
                    ch = chr(utf) if utf else ""
                    if ch == " ":
                        out.append(_DEAD_SPACING.get(dsym, ""))
                    elif ch and dsym in _DEAD_COMBINING:
                        out.append(unicodedata.normalize("NFC", ch + _DEAD_COMBINING[dsym]))
                    continue
            if utf:
                out.append("\n" if utf == 13 else chr(utf))
        return "".join(out)

    def _self_check(self) -> None:
        for ch in [c for c, s in self.direct.items() if self.simulate([s]) != c]:
            del self.direct[ch]
        for ch in [c for c, s in self.composed.items() if self.simulate(s) != c]:
            del self.composed[ch]

    def resolve(self, ch: str) -> Optional[List[Stroke]]:
        s = self.direct.get(ch)
        if s is not None:
            return [s]
        c = self.composed.get(ch)
        return list(c) if c else None


def _run(cmd: List[str], timeout: float = 2.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _kde_current_index(count: int) -> int:
    out = _run(["busctl", "--user", "call", "org.kde.keyboard", "/Layouts",
                "org.kde.KeyboardLayouts", "getLayout"])
    m = re.search(r"\bu\s+(\d+)", out)
    if not m:
        out = _run(["gdbus", "call", "--session", "--dest", "org.kde.keyboard", "--object-path",
                    "/Layouts", "--method", "org.kde.KeyboardLayouts.getLayout"])
        m = re.search(r"uint32\s+(\d+)", out)
    idx = int(m.group(1)) if m else 0
    return idx if 0 <= idx < max(count, 1) else 0


def _detect_kde() -> Optional[LayoutSpec]:
    path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "kxkbrc")
    if not os.path.exists(path):
        return None
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    try:
        cp.read(path, encoding="utf-8")
        if cp.has_option("Layout", "Use") and not cp.getboolean("Layout", "Use"):
            return None
    except (configparser.Error, OSError, ValueError, UnicodeDecodeError):
        return None
    layouts = [x.strip() for x in cp.get("Layout", "LayoutList", fallback="").split(",") if x.strip()]
    if not layouts:
        return None
    variants = [x.strip() for x in cp.get("Layout", "VariantList", fallback="").split(",")]
    variants += [""] * (len(layouts) - len(variants))
    idx = _kde_current_index(len(layouts))
    return LayoutSpec(layouts[idx], variants[idx], cp.get("Layout", "Model", fallback="pc105") or "pc105",
                      cp.get("Layout", "Options", fallback=""),
                      f"KDE (kxkbrc, layout {idx + 1} de {len(layouts)})")


def _first(csv: str) -> str:
    return (csv or "").split(",")[0].strip()


def _detect_gnome() -> Optional[LayoutSpec]:
    if not shutil.which("gsettings"):
        return None
    base = ["gsettings", "get", "org.gnome.desktop.input-sources"]
    srcs = re.findall(r"\('(\w+)',\s*'([^']*)'\)", _run(base + ["sources"]))
    if not srcs:
        return None
    m = re.search(r"(\d+)\s*$", _run(base + ["current"]).strip())
    idx = int(m.group(1)) if m else 0
    if not 0 <= idx < len(srcs):
        idx = 0
    kind, val = srcs[idx]
    if kind != "xkb":
        xk = [x for x in srcs if x[0] == "xkb"]
        if not xk:
            return None
        kind, val = xk[0]
    layout, _, variant = val.partition("+")
    opts = ",".join(re.findall(r"'([^']+)'", _run(base + ["xkb-options"])))
    return LayoutSpec(layout, variant, "pc105", opts, f"GNOME (gsettings, fonte {idx + 1} de {len(srcs)})")


def _detect_cosmic() -> Optional[LayoutSpec]:
    path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "cosmic", "com.system76.CosmicComp", "v1", "xkb_config")
    try:
        with open(path, encoding="utf-8") as fh:
            txt = fh.read()
    except OSError:
        return None
    lay = re.search(r'layout:\s*"([^"]*)"', txt)
    if not lay or not _first(lay.group(1)):
        return None
    var = re.search(r'variant:\s*"([^"]*)"', txt)
    mod = re.search(r'model:\s*"([^"]*)"', txt)
    return LayoutSpec(_first(lay.group(1)), _first(var.group(1)) if var else "",
                      (mod.group(1) if mod else "") or "pc105", "", "COSMIC (xkb_config)")


def _detect_hyprland() -> Optional[LayoutSpec]:
    if not shutil.which("hyprctl") or not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return None

    def opt(name: str) -> str:
        m = re.search(r'"str":\s*"([^"]*)"', _run(["hyprctl", "getoption", f"input:{name}", "-j"]))
        return m.group(1) if m else ""
    lay = _first(opt("kb_layout"))
    return LayoutSpec(lay, _first(opt("kb_variant")), opt("kb_model") or "pc105", opt("kb_options"),
                      "Hyprland (hyprctl)") if lay else None


def _detect_setxkbmap() -> Optional[LayoutSpec]:
    if not os.environ.get("DISPLAY") or not shutil.which("setxkbmap"):
        return None
    out = _run(["setxkbmap", "-query"])
    lay = re.search(r"^layout:\s*(\S+)", out, re.M)
    if not lay:
        return None
    var = re.search(r"^variant:\s*(\S+)", out, re.M)
    mod = re.search(r"^model:\s*(\S+)", out, re.M)
    opt = re.search(r"^options:\s*(\S+)", out, re.M)
    return LayoutSpec(_first(lay.group(1)), _first(var.group(1)) if var else "", mod.group(1) if mod else "pc105",
                      opt.group(1) if opt else "", "setxkbmap (sessão X11/Xwayland)")


def _detect_localectl() -> Optional[LayoutSpec]:
    out = _run(["localectl", "status"])
    lay = re.search(r"X11 Layout:\s*(\S+)", out)
    if not lay:
        return None
    var = re.search(r"X11 Variant:\s*(\S+)", out)
    mod = re.search(r"X11 Model:\s*(\S+)", out)
    return LayoutSpec(_first(lay.group(1)), (_first(var.group(1)) if var else ""),
                      mod.group(1) if mod else "pc105", "", "localectl (configuração do sistema)")


def _detect_etc_files() -> Optional[LayoutSpec]:
    try:
        with open("/etc/default/keyboard", encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
        kv = {m.group(1): m.group(2) for m in re.finditer(r'^\s*(XKB\w+)\s*=\s*"?([^"\n#]*)"?', txt, re.M)}
        if _first(kv.get("XKBLAYOUT", "")):
            return LayoutSpec(_first(kv["XKBLAYOUT"]), _first(kv.get("XKBVARIANT", "")),
                              kv.get("XKBMODEL", "").strip() or "pc105", kv.get("XKBOPTIONS", "").strip(),
                              "/etc/default/keyboard")
    except OSError:
        pass
    for path in sorted(glob.glob("/etc/X11/xorg.conf.d/*.conf")):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                txt = fh.read()
        except OSError:
            continue
        lay = re.search(r'Option\s+"XkbLayout"\s+"([^"]+)"', txt)
        if lay:
            var = re.search(r'Option\s+"XkbVariant"\s+"([^"]*)"', txt)
            mod = re.search(r'Option\s+"XkbModel"\s+"([^"]*)"', txt)
            return LayoutSpec(_first(lay.group(1)), _first(var.group(1)) if var else "",
                              (mod.group(1) if mod else "") or "pc105", "", path)
    return None


def _detect_env() -> Optional[LayoutSpec]:
    env = os.environ.get("XKB_DEFAULT_LAYOUT")
    if not env:
        return None
    return LayoutSpec(_first(env), _first(os.environ.get("XKB_DEFAULT_VARIANT", "")),
                      os.environ.get("XKB_DEFAULT_MODEL") or "pc105", os.environ.get("XKB_DEFAULT_OPTIONS", ""),
                      "variável XKB_DEFAULT_LAYOUT")


def detect_layout() -> LayoutSpec:
    for fn in (_detect_kde, _detect_gnome, _detect_cosmic, _detect_hyprland, _detect_env,
               _detect_setxkbmap, _detect_localectl, _detect_etc_files):
        try:
            spec = fn()
        except Exception:
            spec = None
        if spec and spec.layout:
            return spec
    return LayoutSpec("us", source="padrão (não foi possível detectar; informe o layout manualmente)")


def capslock_on() -> bool:
    for p in glob.glob("/sys/class/leds/*capslock/brightness"):
        try:
            with open(p) as fh:
                if fh.read().strip() not in ("", "0"):
                    return True
        except OSError:
            pass
    return False


@dataclass
class Options:
    char_delay_ms: int = 50
    line_delay_ms: int = 150
    start_delay_s: float = 5.0
    hold_ms: int = 4
    newline_mode: str = "enter"
    unmappable: str = "replace"


@dataclass
class Unit:
    char: str
    strokes: List[Stroke]
    newline: bool = False


_TRANSLIT = {
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2018": "'", "\u2019": "'",
    "\u201a": "'", "\u201b": "'", "\u00ab": "<<", "\u00bb": ">>", "\u2039": "<", "\u203a": ">",
    "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-", "\u2010": "-", "\u2011": "-",
    "\u2026": "...", "\u2022": "*", "\u00b7": ".", "\u00d7": "x", "\u00f7": "/",
    "\u2192": "->", "\u2190": "<-", "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u2002": " ",
    "\u2003": " ", "\u2007": " ", "\u200a": " ", "\u3000": " ",
    "\u00ad": "", "\u200b": "", "\u200c": "", "\u200d": "", "\u2060": "", "\ufeff": "",
}


def _substitute(ch: str) -> Optional[str]:
    if ch in _TRANSLIT:
        return _TRANSLIT[ch]
    d = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    return d if d and d != ch else None


def _show(ch: str) -> str:
    cp = f"U+{ord(ch):04X}"
    if unicodedata.category(ch)[0] in "CZ":
        return f"{cp} ({unicodedata.name(ch, 'invisível').lower()})"
    return f"'{ch}' ({cp})"


def fmt_duration(sec: float) -> str:
    sec = int(round(sec))
    if sec < 60:
        return f"{sec} s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


@dataclass
class Plan:
    units: List[Unit] = field(default_factory=list)
    expected: str = ""
    n_source: int = 0
    n_direct: int = 0
    n_composed: int = 0
    n_newlines: int = 0
    replaced: Dict[str, str] = field(default_factory=dict)
    replaced_n: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    notes: List[str] = field(default_factory=list)
    layout_label: str = ""
    layout_source: str = ""
    opts: Options = field(default_factory=Options)

    @property
    def lossy(self) -> bool:
        return bool(self.replaced_n or self.skipped)

    def duration(self) -> float:
        o = self.opts
        return len(self.units) * o.char_delay_ms / 1000 + self.n_newlines * o.line_delay_ms / 1000

    def report(self) -> str:
        L = [f"Layout do teclado: {self.layout_label}" + (f"  [{self.layout_source}]" if self.layout_source else ""),
             f"Caracteres no texto: {self.n_source}  (tecla direta: {self.n_direct} · "
             f"com tecla morta: {self.n_composed} · quebras de linha: {self.n_newlines})",
             f"Duração estimada: {fmt_duration(self.duration())}"]
        if self.replaced_n:
            L.append("\nSerão SUBSTITUÍDOS (sem tecla equivalente neste layout):")
            for ch, n in self.replaced_n.most_common(12):
                L.append(f"  {_show(ch)}  →  {self.replaced[ch]!r}   ({n}×)")
        if self.skipped:
            L.append("\nSerão IGNORADOS (não existe tecla para eles):")
            for ch, n in self.skipped.most_common(12):
                L.append(f"  {_show(ch)}   ({n}×)")
        L += [f"\nObs.: {n}" for n in self.notes]
        if not self.lossy:
            L.append("\n✔ Todos os caracteres serão digitados exatamente como estão.")
        return "\n".join(L)


def build_plan(text: str, layout: LayoutMap, opts: Options, source: str = "") -> Plan:
    plan = Plan(opts=opts, layout_label=layout.label(), layout_source=source)
    if layout.note:
        plan.notes.append(layout.note)
    raw = len(text)
    for a in ("\r\n", "\r", "\u2028", "\u2029", "\x0b", "\x0c", "\x85"):
        text = text.replace(a, "\n")
    if "\t" in text:
        plan.notes.append(f"{text.count(chr(9))} tabulação(ões) convertida(s) em 4 espaços (Tab mudaria o foco do campo).")
        text = text.replace("\t", "    ")
    plan.n_source = raw

    space = layout.resolve(" ") or [((), KEY_SPACE)]
    nl = {"enter": [((), KEY_ENTER)], "shift_enter": [((KEY_LEFTSHIFT,), KEY_ENTER)], "space": space}.get(
        opts.newline_mode, [((), KEY_ENTER)])
    exp: List[str] = []
    for ch in text:
        if ch == "\n":
            plan.units.append(Unit("\n", list(nl), True))
            plan.n_newlines += 1
            exp.append(" " if opts.newline_mode == "space" else "\n")
            continue
        st = layout.resolve(ch)
        if st:
            plan.units.append(Unit(ch, st))
            exp.append(ch)
            if len(st) == 1:
                plan.n_direct += 1
            else:
                plan.n_composed += 1
            continue
        rep = _substitute(ch) if opts.unmappable == "replace" else None
        if rep:
            seq: List[Stroke] = []
            for rc in rep:
                s2 = layout.resolve(rc)
                if not s2:
                    seq = []
                    break
                seq += s2
            if seq:
                plan.units.append(Unit(ch, seq))
                exp.append(rep)
                plan.replaced[ch] = rep
                plan.replaced_n[ch] += 1
                continue
        plan.skipped[ch] += 1
    plan.expected = "".join(exp)
    return plan


EV_SYN, EV_KEY, SYN_REPORT = 0x00, 0x01, 0
_EV = struct.Struct("llHHi")


if platform.machine().lower().startswith(("ppc", "powerpc", "mips", "sparc")):
    _SIZEBITS, _IOC_NONE, _IOC_WRITE = 13, 1, 4
else:
    _SIZEBITS, _IOC_NONE, _IOC_WRITE = 14, 0, 1


def _ioc(dir_: int, typ: str, nr: int, size: int) -> int:
    return (dir_ << (16 + _SIZEBITS)) | (size << 16) | (ord(typ) << 8) | nr


UI_DEV_CREATE = _ioc(_IOC_NONE, "U", 1, 0)
UI_DEV_DESTROY = _ioc(_IOC_NONE, "U", 2, 0)
UI_DEV_SETUP = _ioc(_IOC_WRITE, "U", 3, 92)
UI_SET_EVBIT = _ioc(_IOC_WRITE, "U", 100, 4)
UI_SET_KEYBIT = _ioc(_IOC_WRITE, "U", 101, 4)

UINPUT_PATHS = ("/dev/uinput", "/dev/input/uinput")


def uinput_path(writable: bool = False) -> Optional[str]:
    for p in UINPUT_PATHS:
        if os.path.exists(p) and (not writable or os.access(p, os.W_OK)):
            return p
    return None


class BackendError(RuntimeError):
    pass


class Backend:
    name = "?"

    def open(self) -> None: ...
    def tap(self, mods: Sequence[int], key: int, hold: float) -> None: ...
    def release_all(self) -> None: ...
    def close(self) -> None: ...


class UInputBackend(Backend):
    name = "uinput"
    DEVICE_NAME = b"Digitador Virtual Keyboard"

    def __init__(self) -> None:
        self.fd: Optional[int] = None
        self._down: set = set()

    def open(self) -> None:
        if self.fd is not None:
            return
        path = uinput_path() or UINPUT_PATHS[0]
        try:
            fd = os.open(path, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            raise BackendError("/dev/uinput não existe: o módulo 'uinput' do kernel não está carregado.\n"
                               "Corrija com:  sudo modprobe uinput   (ou rode o instalar.sh)")
        except PermissionError:
            raise BackendError(f"Sem permissão para escrever em {path}.\n"
                               f"Rode o instalar.sh (cria a regra udev do grupo {UINPUT_GROUP}) e faça logout/login.")
        except OSError as exc:
            raise BackendError(f"Falha ao abrir {path}: {exc}")
        try:
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
            for code in range(1, 249):
                fcntl.ioctl(fd, UI_SET_KEYBIT, code)
            fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 0x03, 0x1209, 0x0001, 1, self.DEVICE_NAME, 0))
            fcntl.ioctl(fd, UI_DEV_CREATE)
        except OSError as exc:
            os.close(fd)
            raise BackendError(f"Não foi possível criar o teclado virtual: {exc}")
        self.fd = fd
        time.sleep(0.8)

    def _send(self, code: int, value: int) -> None:
        os.write(self.fd, _EV.pack(0, 0, EV_KEY, code, value) + _EV.pack(0, 0, EV_SYN, SYN_REPORT, 0))
        if value:
            self._down.add(code)
        else:
            self._down.discard(code)

    def tap(self, mods: Sequence[int], key: int, hold: float = 0.004) -> None:
        if self.fd is None:
            raise BackendError("teclado virtual fechado")
        try:
            for m in mods:
                self._send(m, 1)
            if mods:
                time.sleep(0.002)
            self._send(key, 1)
            time.sleep(hold)
            self._send(key, 0)
            for m in reversed(mods):
                self._send(m, 0)
        except OSError as exc:
            raise BackendError(f"erro ao enviar tecla ao uinput: {exc}")

    def release_all(self) -> None:
        if self.fd is None:
            return
        for code in list(self._down):
            try:
                self._send(code, 0)
            except OSError:
                break

    def close(self) -> None:
        if self.fd is None:
            return
        try:
            self.release_all()
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None


class YdotoolBackend(Backend):
    name = "ydotool"

    def __init__(self) -> None:
        self.exe: Optional[str] = None
        self.env = dict(os.environ)

    def open(self) -> None:
        self.exe = shutil.which("ydotool")
        if not self.exe:
            raise BackendError(f"ydotool não está instalado ({install_hint('ydotool')}).")
        cands = [os.environ.get("YDOTOOL_SOCKET"), os.path.join(_RUNTIME_DIR, ".ydotool_socket"), "/tmp/.ydotool_socket"]
        sock = next((c for c in cands if c and os.path.exists(c)), None)
        if not sock:
            raise BackendError("O daemon ydotoold não está rodando (socket não encontrado).\n"
                               "Veja a seção 'Alternativa: ydotool' do README.")
        if not os.access(sock, os.W_OK):
            raise BackendError(f"Sem permissão no socket {sock}. Veja a seção 'Alternativa: ydotool' do README.")
        self.env["YDOTOOL_SOCKET"] = sock

    def tap(self, mods: Sequence[int], key: int, hold: float = 0.004) -> None:
        seq = [f"{m}:1" for m in mods] + [f"{key}:1", f"{key}:0"] + [f"{m}:0" for m in reversed(mods)]
        try:
            r = subprocess.run([self.exe, "key", *seq], env=self.env, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackendError(f"falha ao executar ydotool: {exc}")
        if r.returncode != 0:
            raise BackendError(f"ydotool retornou erro: {(r.stderr or r.stdout).strip()}")

    def release_all(self) -> None:
        pass

    def close(self) -> None:
        pass


def backend_status() -> Tuple[bool, str]:
    if uinput_path(writable=True):
        return True, "uinput disponível"
    socks = [os.environ.get("YDOTOOL_SOCKET"), os.path.join(_RUNTIME_DIR, ".ydotool_socket"), "/tmp/.ydotool_socket"]
    if shutil.which("ydotool") and any(x and os.path.exists(x) and os.access(x, os.W_OK) for x in socks):
        return True, "ydotool disponível (daemon em execução)"
    if not uinput_path():
        return False, "o módulo uinput do kernel não está carregado — rode o instalar.sh"
    return False, "sem permissão em /dev/uinput — rode o instalar.sh e faça logout/login"


def make_backend(kind: str = "auto") -> Backend:
    errors: List[str] = []
    for cls, k in ((UInputBackend, "uinput"), (YdotoolBackend, "ydotool")):
        if kind not in ("auto", k):
            continue
        b = cls()
        try:
            b.open()
            return b
        except BackendError as exc:
            errors.append(f"[{k}] {exc}")
    raise BackendError("\n\n".join(errors) or f"backend desconhecido: {kind}")


class StopToken:

    def __init__(self) -> None:
        self._ev = threading.Event()

    def set(self) -> None:
        self._ev.set()

    def is_set(self) -> bool:
        return self._ev.is_set() or os.path.exists(STOP_FLAG)

    def sleep(self, secs: float) -> bool:
        end = time.monotonic() + secs
        while True:
            if self.is_set():
                return True
            rem = end - time.monotonic()
            if rem <= 0:
                return False
            self._ev.wait(min(rem, 0.02))


def clear_stop_flag() -> None:
    try:
        os.unlink(STOP_FLAG)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def request_stop() -> None:
    os.makedirs(_RUNTIME_DIR, mode=0o700, exist_ok=True)
    with open(STOP_FLAG, "w") as fh:
        fh.write(str(time.time()))


@dataclass
class Result:
    typed: int
    total: int
    stopped: bool = False
    error: str = ""
    seconds: float = 0.0


def execute(plan: Plan, backend: Backend, opts: Options, stop: StopToken,
            on_status: Optional[Callable[[str], None]] = None,
            on_progress: Optional[Callable[[int, int], None]] = None) -> Result:
    t0 = time.monotonic()
    total = len(plan.units)
    say = on_status or (lambda _m: None)


    typed = 0
    try:
        for remaining in range(int(round(opts.start_delay_s)), 0, -1):
            say(f"Começa em {remaining} s — clique agora no campo de texto do site…")
            if stop.sleep(1.0):
                return Result(0, total, True, seconds=time.monotonic() - t0)
        say("Digitando…")
        hold = max(0.001, opts.hold_ms / 1000.0)
        last = 0.0
        for i, unit in enumerate(plan.units):
            if stop.is_set():
                return Result(typed, total, True, seconds=time.monotonic() - t0)
            for j, (mods, key) in enumerate(unit.strokes):
                backend.tap(mods, key, hold)
                if j + 1 < len(unit.strokes):
                    time.sleep(0.005)
            typed = i + 1
            now = time.monotonic()
            if on_progress and (now - last >= 0.1 or typed == total):
                last = now
                on_progress(typed, total)
            delay = opts.char_delay_ms + (opts.line_delay_ms if unit.newline else 0)
            if delay > 0 and typed < total and stop.sleep(delay / 1000.0):
                return Result(typed, total, True, seconds=time.monotonic() - t0)
        return Result(typed, total, False, seconds=time.monotonic() - t0)
    except BackendError as exc:
        return Result(typed, total, False, str(exc), time.monotonic() - t0)
    finally:
        try:
            backend.release_all()
        except Exception:
            pass
        clear_stop_flag()


def _yn(label: str, ok: bool, extra: str = "") -> bool:
    print(f"  [{'OK' if ok else '!!'}] {label}" + (f" — {extra}" if extra else ""))
    return ok


def diagnostico(layout_text: Optional[str] = None) -> int:
    print(f"{APP_TITLE} v{VERSION}\n")
    print("Sessão:")
    st = os.environ.get("XDG_SESSION_TYPE", "?")
    _yn(f"tipo de sessão: {st} · desktop: {os.environ.get('XDG_CURRENT_DESKTOP', '?')} · Python {platform.python_version()}", True)
    print("\nInjeção de teclas:")
    uin = uinput_path()
    _yn("/dev/uinput existe", bool(uin), "" if uin else "rode: sudo modprobe uinput (ou o instalar.sh)")
    wr = bool(uinput_path(writable=True))
    _yn("permissão de escrita em /dev/uinput", wr, "" if wr else f"rode o instalar.sh; o grupo {UINPUT_GROUP} só vale após logout/login")
    yd = shutil.which("ydotool")
    _yn("ydotool instalado (opcional)", bool(yd))
    print("\nInterface gráfica:")
    qt = None
    for mod in ("PySide6", "PyQt6"):
        try:
            __import__(mod)
            qt = mod
            break
        except ImportError:
            pass
    _yn("binding Qt 6 para Python", qt is not None, qt or f"instale: {install_hint('qt')}  (sem ele, só o modo texto -f funciona)")
    print("\nLayout do teclado:")
    try:
        spec = parse_layout_spec(layout_text) if layout_text else detect_layout()
        lm = LayoutMap.from_spec(spec)
    except XkbError as exc:
        _yn("mapa do layout", False, str(exc))
        return 1
    _yn(f"layout em uso: {lm.label()}", True, spec.source)
    _yn(f"{len(lm.direct)} caracteres por tecla direta, {len(lm.composed)} por tecla morta", True)
    plan = build_plan(TEST_SAMPLE, lm, Options())
    print("\n" + "\n".join("  " + ln for ln in plan.report().splitlines()))
    print(f"\nResultado do teste de mapeamento: "
          f"{'OK' if not plan.lossy else 'há substituições (normal em layouts sem acentos, como us; veja acima)'}")
    if capslock_on():
        print("\n!! Caps Lock está ligado: desligue antes de digitar.")
    return 0 if (wr or yd) else 1


def cli_main(args: argparse.Namespace) -> int:
    if args.parar:
        request_stop()
        print("Pedido de parada enviado.")
        return 0
    if args.diagnostico:
        return diagnostico(args.layout)
    if args.detectar_layout:
        spec = detect_layout()
        print(f"{spec.label()}  (modelo {spec.model}; {spec.source})")
        return 0
    if args.arquivo is None:
        print("Informe o texto com -f ARQUIVO (ou -f - para ler da entrada padrão).", file=sys.stderr)
        return 2
    text = sys.stdin.read() if args.arquivo == "-" else open(args.arquivo, encoding="utf-8").read()
    opts = Options(args.atraso_ms, args.atraso_linha_ms, args.inicio_s, 4, args.quebra,
                   "abort" if args.estrito else "replace")
    spec = parse_layout_spec(args.layout) if args.layout else detect_layout()
    plan = build_plan(text, LayoutMap.from_spec(spec), opts, spec.source)
    print(plan.report() + "\n")
    if not plan.units:
        print("Nada para digitar.", file=sys.stderr)
        return 2
    if plan.lossy and args.estrito:
        print("Abortado (--estrito): há caracteres sem tecla neste layout.", file=sys.stderr)
        return 3
    if args.dry_run:
        return 0
    if plan.lossy and not args.sim:
        if not sys.stdin.isatty() or input("Continuar mesmo assim? [s/N] ").strip().lower() not in ("s", "sim", "y"):
            print("Cancelado.")
            return 3
    if capslock_on():
        print("Caps Lock está ligado — desligue e tente de novo.", file=sys.stderr)
        return 3
    try:
        backend = make_backend(args.backend)
    except BackendError as exc:
        print(f"\nNão foi possível iniciar a injeção de teclas:\n{exc}", file=sys.stderr)
        return 4
    stop = StopToken()
    for sg in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sg, lambda *_: stop.set())
    print("Para interromper: Ctrl+C aqui, ou rode `digitador.py --parar` (use um atalho global do seu desktop).")
    clear_stop_flag()
    res = execute(plan, backend, opts, stop,
                  on_status=lambda m: print(f"\r{m:<70}", end="", flush=True),
                  on_progress=lambda a, b: print(f"\r  {a}/{b} caracteres", end="", flush=True))
    backend.close()
    print()
    if res.error:
        print(f"Erro: {res.error}", file=sys.stderr)
        return 5
    print(("Interrompido" if res.stopped else "Concluído") + f": {res.typed} de {res.total} caracteres em {fmt_duration(res.seconds)}.")
    return 1 if res.stopped else 0


def _msg_sem_qt() -> str:
    return ("Não encontrei PySide6 nem PyQt6.\n"
            f"Instale com:  {install_hint('qt')}\n"
            "ou rode o instalar.sh de novo (ele cria um ambiente virtual com PySide6 se o pacote da distro não existir).\n"
            "Sem interface gráfica, use o modo texto: digitador.py -f arquivo.txt\n")


def run_gui() -> int:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        sys.stderr.write("Nenhuma sessão gráfica detectada (DISPLAY/WAYLAND_DISPLAY vazios).\n"
                         "Abra o programa dentro do ambiente gráfico ou use o modo texto: digitador.py -f arquivo.txt\n")
        return 2
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        Signal = QtCore.Signal
    except ImportError:
        try:
            from PyQt6 import QtCore, QtGui, QtWidgets
            Signal = QtCore.pyqtSignal
        except ImportError:
            sys.stderr.write(_msg_sem_qt())
            return 2
    W, Qt = QtWidgets, QtCore.Qt

    class Bridge(QtCore.QObject):
        progress = Signal(int, int)
        status = Signal(str)
        done = Signal(object)

    class MainWindow(W.QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle(APP_TITLE)
            self.settings = QtCore.QSettings(APP_ID, "digitador")
            self.backend: Optional[Backend] = None
            self.worker: Optional[threading.Thread] = None
            self.stop_token: Optional[StopToken] = None
            self.test_mode = False
            self.test_expected = ""
            self.bridge = Bridge()
            self.bridge.progress.connect(self.on_progress)
            self.bridge.status.connect(self.on_status)
            self.bridge.done.connect(self.on_done)
            self._build_ui()
            self._build_tray()
            self._load_settings()
            self.refresh_layout_label()
            self.refresh_backend_label()
            self.update_count()
            QtCore.QTimer.singleShot(400, self.warm_backend)


        def _build_ui(self) -> None:
            root = W.QWidget()
            self.setCentralWidget(root)
            root_lay = W.QVBoxLayout(root)
            tabs = W.QTabWidget()
            root_lay.addWidget(tabs)

            main_page = W.QWidget()
            tabs.addTab(main_page, "Digitador")
            lay = W.QVBoxLayout(main_page)
            intro = W.QLabel("<b>Como usar</b><br>1. Escreva ou cole o texto abaixo.<br>"
                             "2. Clique em <i>Iniciar digitação</i>.<br>"
                             "3. Durante a contagem, clique no campo de texto do site: o texto será digitado "
                             "tecla por tecla, sem usar Ctrl+V.")
            intro.setTextFormat(Qt.TextFormat.RichText)
            intro.setWordWrap(True)
            lay.addWidget(intro)

            self.text = W.QPlainTextEdit()
            self.text.setPlaceholderText("Digite ou cole aqui o texto que deve ser digitado…")
            self.text.setMinimumHeight(160)
            self.text.textChanged.connect(lambda *_: self.update_count())
            lay.addWidget(self.text, 1)
            self.lbl_count = W.QLabel()
            lay.addWidget(self.lbl_count)

            box = W.QGroupBox("Opções")
            form = W.QFormLayout(box)
            self.sp_char = W.QSpinBox()
            self.sp_char.setRange(0, 5000)
            self.sp_char.setSuffix(" ms")
            self.sp_char.setValue(50)
            self.lbl_speed = W.QLabel()
            row = W.QHBoxLayout()
            row.addWidget(self.sp_char)
            row.addWidget(self.lbl_speed, 1)
            form.addRow("Intervalo entre caracteres:", row)
            self.sp_line = W.QSpinBox()
            self.sp_line.setRange(0, 20000)
            self.sp_line.setSuffix(" ms")
            self.sp_line.setValue(150)
            form.addRow("Pausa extra em quebra de linha:", self.sp_line)
            self.sp_start = W.QSpinBox()
            self.sp_start.setRange(1, 60)
            self.sp_start.setSuffix(" s")
            self.sp_start.setValue(5)
            form.addRow("Contagem antes de começar:", self.sp_start)
            self.cb_nl = W.QComboBox()
            for label, data in (("Enter", "enter"), ("Shift+Enter", "shift_enter"), ("Espaço (juntar linhas)", "space")):
                self.cb_nl.addItem(label, data)
            form.addRow("Quebra de linha vira:", self.cb_nl)
            self.cb_unm = W.QComboBox()
            for label, data in (("Substituir por equivalente (ex.: “ → \")", "replace"),
                                ("Ignorar o caractere", "skip"), ("Cancelar a digitação", "abort")):
                self.cb_unm.addItem(label, data)
            form.addRow("Caractere sem tecla no layout:", self.cb_unm)
            self.ed_layout = W.QLineEdit()
            self.ed_layout.setPlaceholderText("vazio = detectar automaticamente (ex.: br, us(intl))")
            self.btn_detect = W.QPushButton("Detectar")
            self.btn_detect.clicked.connect(lambda *_: (self.refresh_layout_label(), self.refresh_backend_label()))
            row2 = W.QHBoxLayout()
            row2.addWidget(self.ed_layout, 1)
            row2.addWidget(self.btn_detect)
            form.addRow("Layout do teclado (XKB):", row2)
            self.lbl_layout = W.QLabel()
            self.lbl_layout.setWordWrap(True)
            form.addRow("", self.lbl_layout)
            self.cb_backend = W.QComboBox()
            for label, data in (("Automático", "auto"), ("uinput (direto)", "uinput"), ("ydotool (daemon)", "ydotool")):
                self.cb_backend.addItem(label, data)
            form.addRow("Método de injeção:", self.cb_backend)
            self.lbl_backend = W.QLabel()
            self.lbl_backend.setWordWrap(True)
            self.lbl_backend.setTextFormat(Qt.TextFormat.RichText)
            form.addRow("Injeção de teclas:", self.lbl_backend)
            self.chk_min = W.QCheckBox("Minimizar esta janela ao iniciar")
            self.chk_min.setChecked(True)
            form.addRow("", self.chk_min)
            lay.addWidget(box)
            self.sp_char.valueChanged.connect(lambda *_: self.update_count())
            self.sp_line.valueChanged.connect(lambda *_: self.update_count())

            trow = W.QHBoxLayout()
            self.ed_test = W.QLineEdit()
            self.ed_test.setPlaceholderText("Campo de teste: o botão ao lado digita aqui para conferir o layout")
            self.btn_test = W.QPushButton("Testar layout")
            self.btn_test.clicked.connect(lambda *_: self.on_test())
            trow.addWidget(self.ed_test, 1)
            trow.addWidget(self.btn_test)
            lay.addLayout(trow)

            brow = W.QHBoxLayout()
            self.btn_check = W.QPushButton("Verificar texto")
            self.btn_check.clicked.connect(lambda *_: self.on_check())
            self.btn_start = W.QPushButton("▶  Iniciar digitação")
            self.btn_start.setStyleSheet("QPushButton{background:#4CAF50;color:white;font-weight:bold;padding:8px 18px;"
                                         "border-radius:4px}QPushButton:hover{background:#45a049}"
                                         "QPushButton:disabled{background:#9aa39a}")
            self.btn_start.clicked.connect(lambda *_: self.on_start())
            self.btn_stop = W.QPushButton("■  Parar")
            self.btn_stop.setStyleSheet("QPushButton{background:#d9534f;color:white;font-weight:bold;padding:8px 18px;"
                                        "border-radius:4px}QPushButton:hover{background:#c9302c}"
                                        "QPushButton:disabled{background:#c9a5a4}")
            self.btn_stop.setEnabled(False)
            self.btn_stop.clicked.connect(lambda *_: self.stop_typing())
            brow.addWidget(self.btn_check)
            brow.addStretch(1)
            brow.addWidget(self.btn_start)
            brow.addWidget(self.btn_stop)
            lay.addLayout(brow)

            self.bar = W.QProgressBar()
            self.bar.setRange(0, 100)
            self.lbl_status = W.QLabel("Pronto.")
            lay.addWidget(self.bar)
            lay.addWidget(self.lbl_status)
            foot = W.QLabel("<small>Para parar de qualquer lugar, crie um atalho global no seu desktop para o comando "
                            "<code>digitador --parar</code>.</small>")
            foot.setTextFormat(Qt.TextFormat.RichText)
            foot.setWordWrap(True)
            lay.addWidget(foot)


            credits_page = W.QWidget()
            credits_lay = W.QVBoxLayout(credits_page)
            credits_lay.addStretch(1)
            credits_title = W.QLabel("Créditos")
            credits_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            credits_title.setStyleSheet("font-size: 24px; font-weight: bold;")
            credits_lay.addWidget(credits_title)
            credits_author = W.QLabel("Desenvolvido por <b>Mulari</b>")
            credits_author.setAlignment(Qt.AlignmentFlag.AlignCenter)
            credits_lay.addWidget(credits_author)
            credits_social = W.QLabel(
                '<p align="center"><b>Discord:</b> kks_mulari</p>'
                '<p align="center"><b>Instagram:</b> '
                '<a href="https://www.instagram.com/kks_mulari/">kks_mulari</a></p>'
            )
            credits_social.setTextFormat(Qt.TextFormat.RichText)
            credits_social.setOpenExternalLinks(True)
            credits_social.setAlignment(Qt.AlignmentFlag.AlignCenter)
            credits_lay.addWidget(credits_social)
            credits_lay.addStretch(2)
            tabs.addTab(credits_page, "Créditos")
            self.resize(640, 760)

        def _build_tray(self) -> None:
            icon = QtGui.QIcon.fromTheme("input-keyboard")
            if icon.isNull():
                icon = self.style().standardIcon(W.QStyle.StandardPixmap.SP_ComputerIcon)
            self.setWindowIcon(icon)
            self.tray = None
            if not W.QSystemTrayIcon.isSystemTrayAvailable():
                return
            self.tray = W.QSystemTrayIcon(icon, self)
            menu = W.QMenu()
            menu.addAction("■ Parar digitação").triggered.connect(lambda *_: self.stop_typing())
            menu.addAction("Mostrar janela").triggered.connect(lambda *_: self.bring_front())
            menu.addSeparator()
            menu.addAction("Sair").triggered.connect(lambda *_: self.close())
            self._tray_menu = menu
            self.tray.setContextMenu(menu)
            self.tray.setToolTip(APP_TITLE + " — clique no ícone para parar a digitação")
            self.tray.activated.connect(self.on_tray)
            self.tray.show()

        def _load_settings(self) -> None:
            s = self.settings
            self.sp_char.setValue(int(s.value("char_delay_ms", 50)))
            self.sp_line.setValue(int(s.value("line_delay_ms", 150)))
            self.sp_start.setValue(int(s.value("start_delay_s", 5)))
            self.ed_layout.setText(str(s.value("layout", "")))
            for cb, key in ((self.cb_nl, "newline"), (self.cb_unm, "unmappable"), (self.cb_backend, "backend")):
                i = cb.findData(str(s.value(key, "")))
                if i >= 0:
                    cb.setCurrentIndex(i)
            self.chk_min.setChecked(str(s.value("minimize", "true")).lower() == "true")

        def _save_settings(self) -> None:
            s = self.settings
            s.setValue("char_delay_ms", self.sp_char.value())
            s.setValue("line_delay_ms", self.sp_line.value())
            s.setValue("start_delay_s", self.sp_start.value())
            s.setValue("layout", self.ed_layout.text().strip())
            s.setValue("newline", self.cb_nl.currentData())
            s.setValue("unmappable", self.cb_unm.currentData())
            s.setValue("backend", self.cb_backend.currentData())
            s.setValue("minimize", "true" if self.chk_min.isChecked() else "false")


        def is_running(self) -> bool:
            return self.worker is not None and self.worker.is_alive()

        def get_opts(self) -> Options:
            return Options(self.sp_char.value(), self.sp_line.value(), float(self.sp_start.value()), 4,
                           str(self.cb_nl.currentData()), str(self.cb_unm.currentData()))

        def get_layout(self) -> Tuple[LayoutMap, LayoutSpec]:
            txt = self.ed_layout.text().strip()
            spec = parse_layout_spec(txt) if txt else detect_layout()
            return LayoutMap.from_spec(spec), spec

        def refresh_layout_label(self) -> None:
            self.lbl_layout.setTextFormat(Qt.TextFormat.RichText)
            try:
                lm, spec = self.get_layout()
                self.lbl_layout.setText(f"Em uso: <b>{html.escape(lm.label())}</b> — {html.escape(spec.source)}"
                                        + (f"<br>{html.escape(lm.note)}" if lm.note else ""))
            except XkbError as exc:
                self.lbl_layout.setText(f"<span style='color:#c9302c'>{html.escape(str(exc))}</span>")

        def warm_backend(self) -> None:
            if self.backend is None and backend_status()[0]:
                try:
                    self.backend = make_backend(str(self.cb_backend.currentData()))
                except BackendError:
                    self.backend = None

        def refresh_backend_label(self) -> None:
            ok, msg = backend_status()
            self.lbl_backend.setText(f"<span style='color:#2e7d32'>✔ {html.escape(msg)}</span>" if ok
                                     else f"<span style='color:#c9302c'>⚠ {html.escape(msg)}</span>")

        def update_count(self) -> None:
            t = self.text.toPlainText()
            est = len(t) * self.sp_char.value() / 1000 + t.count("\n") * self.sp_line.value() / 1000
            self.lbl_count.setText(f"{len(t)} caracteres · duração estimada ≈ {fmt_duration(est)}")
            ms = self.sp_char.value()
            self.lbl_speed.setText(f"≈ {1000 / ms:.0f} caracteres/s" if ms else "máxima velocidade")

        def ask(self, title: str, text: str, yes: str = "Continuar", no: str = "Cancelar") -> bool:
            box = W.QMessageBox(self)
            box.setWindowTitle(title)
            box.setText(text)
            b_yes = box.addButton(yes, W.QMessageBox.ButtonRole.AcceptRole)
            box.addButton(no, W.QMessageBox.ButtonRole.RejectRole)
            box.exec()
            return box.clickedButton() == b_yes

        def ensure_backend(self) -> Optional[Backend]:
            kind = str(self.cb_backend.currentData())
            if self.backend is not None and (kind == "auto" or kind == self.backend.name):
                return self.backend
            if self.backend is not None:
                self.backend.close()
                self.backend = None
            try:
                self.backend = make_backend(kind)
            except BackendError as exc:
                W.QMessageBox.critical(self, "Não foi possível digitar",
                                       f"{exc}\n\nPara liberar a injeção de teclas, rode o instalar.sh (veja o README).")
                self.refresh_backend_label()
                return None
            return self.backend


        def on_check(self) -> None:
            try:
                lm, spec = self.get_layout()
            except XkbError as exc:
                W.QMessageBox.warning(self, "Layout", str(exc))
                return
            plan = build_plan(self.text.toPlainText(), lm, self.get_opts(), spec.source)
            W.QMessageBox.information(self, "Verificação do texto", plan.report())

        def on_start(self) -> None:
            if self.is_running():
                return
            text = self.text.toPlainText()
            if not text.strip():
                W.QMessageBox.warning(self, "Aviso", "Por favor, insira o texto para digitação.")
                return
            try:
                lm, spec = self.get_layout()
            except XkbError as exc:
                W.QMessageBox.warning(self, "Layout", str(exc))
                return
            opts = self.get_opts()
            plan = build_plan(text, lm, opts, spec.source)
            if plan.lossy:
                if opts.unmappable == "abort":
                    W.QMessageBox.warning(self, "Digitação cancelada", plan.report())
                    return
                if not self.ask("Alguns caracteres serão alterados", plan.report() + "\n\nContinuar assim mesmo?"):
                    return
            if capslock_on():
                W.QMessageBox.warning(self, "Caps Lock ligado", "Desligue o Caps Lock antes de digitar (ele inverteria as letras).")
                return
            backend = self.ensure_backend()
            if backend is None:
                return
            self.test_mode = False
            self.run_plan(plan, backend, opts, minimize=self.chk_min.isChecked())

        def on_test(self) -> None:
            if self.is_running():
                return
            try:
                lm, spec = self.get_layout()
            except XkbError as exc:
                W.QMessageBox.warning(self, "Layout", str(exc))
                return
            opts = Options(15, 0, 1.0, 4, "space", "replace")
            plan = build_plan(TEST_SAMPLE, lm, opts, spec.source)
            backend = self.ensure_backend()
            if backend is None:
                return
            self.test_mode, self.test_expected = True, plan.expected
            self.ed_test.clear()
            self.ed_test.setFocus()
            self.run_plan(plan, backend, opts, minimize=False)

        def run_plan(self, plan: Plan, backend: Backend, opts: Options, minimize: bool) -> None:


            clear_stop_flag()
            self.stop_token = StopToken()
            self.set_running(True)
            self.bar.setValue(0)
            if minimize:
                self.showMinimized()
            if self.tray is not None and not self.test_mode:
                self.tray.showMessage(APP_TITLE, f"Clique no campo de texto do site: a digitação começa em "
                                      f"{int(round(opts.start_delay_s))} s. Para parar, clique no ícone da bandeja.",
                                      W.QSystemTrayIcon.MessageIcon.Information, 4000)
            token, bridge = self.stop_token, self.bridge

            def work() -> None:
                res = execute(plan, backend, opts, token, bridge.status.emit, bridge.progress.emit)
                bridge.done.emit(res)

            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

        def stop_typing(self) -> None:
            if self.is_running() and self.stop_token is not None:
                self.stop_token.set()
                self.lbl_status.setText("Parando…")

        def set_running(self, running: bool) -> None:
            for w in (self.btn_start, self.btn_test, self.btn_check, self.text, self.btn_detect):
                w.setEnabled(not running)
            self.btn_stop.setEnabled(running)


        def on_status(self, msg: str) -> None:
            self.lbl_status.setText(msg)
            if self.tray is not None:
                self.tray.setToolTip(f"{APP_TITLE} — {msg}")

        def on_progress(self, done: int, total: int) -> None:
            self.bar.setValue(int(done * 100 / total) if total else 0)
            self.lbl_status.setText(f"Digitando… {done} de {total} caracteres")

        def on_done(self, res: Result) -> None:
            self.set_running(False)
            self.bar.setValue(int(res.typed * 100 / res.total) if res.total else 0)
            if self.test_mode:
                self.test_mode = False
                if res.error:
                    W.QMessageBox.critical(self, "Teste falhou", res.error)
                elif res.stopped:
                    self.lbl_status.setText("Teste interrompido.")
                else:
                    self.lbl_status.setText("Conferindo o que o campo recebeu…")
                    QtCore.QTimer.singleShot(600, self.finish_test)
                return
            self.bring_front()
            if res.error:
                self.lbl_status.setText("Erro durante a digitação.")
                W.QMessageBox.critical(self, "Erro durante a digitação", res.error)
                return
            msg = (f"Interrompido em {res.typed} de {res.total} caracteres." if res.stopped
                   else f"Texto digitado com sucesso! ({res.typed} caracteres em {fmt_duration(res.seconds)})")
            self.lbl_status.setText(msg)
            if self.tray is not None:
                self.tray.setToolTip(APP_TITLE)
                self.tray.showMessage(APP_TITLE, msg, W.QSystemTrayIcon.MessageIcon.Information, 5000)

        def finish_test(self) -> None:
            got = self.ed_test.text()
            if got == self.test_expected:
                self.lbl_status.setText("✔ Teste OK: o campo recebeu exatamente o texto esperado.")
                return
            self.lbl_status.setText("✘ O campo recebeu um texto diferente do esperado.")
            W.QMessageBox.warning(self, "Layout diferente do esperado",
                                  f"Esperado:\n{self.test_expected}\n\nRecebido:\n{got}\n\n"
                                  "Informe o layout correto no campo “Layout do teclado” (ex.: br) e teste de novo.")


        def bring_front(self) -> None:
            self.showNormal()
            self.raise_()
            self.activateWindow()

        def on_tray(self, reason) -> None:
            if reason == W.QSystemTrayIcon.ActivationReason.Trigger:
                if self.is_running():
                    self.stop_typing()
                elif self.isMinimized() or not self.isVisible():
                    self.bring_front()
                else:
                    self.showMinimized()

        def closeEvent(self, event) -> None:
            if self.is_running() and self.stop_token is not None:
                self.stop_token.set()
                if self.worker is not None:
                    self.worker.join(timeout=1.0)

                if self.worker is not None and self.worker.is_alive():
                    event.ignore()
                    QtCore.QTimer.singleShot(100, self.close)
                    return
            self._save_settings()
            if self.backend is not None:
                self.backend.close()
                self.backend = None
            if self.tray is not None:
                self.tray.hide()
            event.accept()

    app = W.QApplication(sys.argv)
    QtGui.QGuiApplication.setDesktopFileName(APP_ID)
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_TITLE)
    win = MainWindow()
    win.show()
    return app.exec()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="digitador", description="Digita um texto tecla por tecla (Linux, Wayland ou X11).")
    p.add_argument("-f", "--arquivo", help="arquivo de texto a digitar ('-' = entrada padrão)")
    p.add_argument("--atraso-ms", type=int, default=50, help="intervalo entre caracteres (padrão 50)")
    p.add_argument("--atraso-linha-ms", type=int, default=150, help="pausa extra após cada quebra de linha (padrão 150)")
    p.add_argument("--inicio-s", type=float, default=5, help="contagem antes de começar (padrão 5)")
    p.add_argument("--quebra", choices=["enter", "shift_enter", "space"], default="enter", help="o que fazer nas quebras de linha")
    p.add_argument("--layout", help="layout XKB, ex.: br ou us(intl) (padrão: detectar)")
    p.add_argument("--backend", choices=["auto", "uinput", "ydotool"], default="auto")
    p.add_argument("--estrito", action="store_true", help="aborta se algum caractere não tiver tecla no layout")
    p.add_argument("-y", "--sim", action="store_true", help="não pedir confirmação quando houver substituições")
    p.add_argument("--dry-run", action="store_true", help="só mostra o relatório, sem digitar")
    p.add_argument("--parar", action="store_true", help="interrompe a digitação em andamento")
    p.add_argument("--diagnostico", action="store_true", help="verifica permissões, layout e mapeamento")
    p.add_argument("--detectar-layout", action="store_true", help="mostra o layout detectado")
    p.add_argument("--versao", action="version", version=f"%(prog)s {VERSION}")
    return p


def _maybe_reexec_with_group() -> None:
    if os.environ.get("DIGITADOR_SG") or grp is None or pwd is None:
        return
    path = uinput_path()
    if path is None or os.access(path, os.W_OK):
        return
    try:
        g = grp.getgrnam(UINPUT_GROUP)
        me = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return
    if me not in g.gr_mem or g.gr_gid in os.getgroups():
        return
    sg = shutil.which("sg")
    if not sg:
        return
    os.environ["DIGITADOR_SG"] = "1"
    cmd = " ".join(shlex.quote(a) for a in [sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:])
    try:
        os.execv(sg, [sg, UINPUT_GROUP, "-c", cmd])
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not (args.parar or args.detectar_layout or args.dry_run):
        _maybe_reexec_with_group()
    try:
        if args.arquivo is not None or args.parar or args.diagnostico or args.detectar_layout:
            return cli_main(args)
        return run_gui()
    except XkbError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
