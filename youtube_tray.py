import os
os.environ['QT_LOGGING_RULES'] = '*.debug=false;qt.qpa.*=false'

import fcntl
import io
import json
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib, GLibUnix
from PIL import Image, ImageDraw


MPRIS_PREFIX = 'org.mpris.MediaPlayer2.'
MPRIS_PATH = '/org/mpris/MediaPlayer2'
MPRIS_ROOT_IFACE = 'org.mpris.MediaPlayer2'
MPRIS_PLAYER_IFACE = 'org.mpris.MediaPlayer2.Player'

# Sólo nos interesan los players que estén sonando YouTube.
YT_HINTS = ('youtube.com', 'youtu.be', 'ytimg.com', 'googlevideo.com')
PLASMA_INTEGRATION = 'plasma-browser-integration'
VOLUME_STEP = 0.05

# El navegador publica UNA sola sesión de medios por fuente MPRIS: si otra
# pestaña reproduce algo, YouTube desaparece del bus. El runner de pestañas
# de la extensión de Plasma, en cambio, lista todas las pestañas abiertas
# sin importar cuál tiene el foco, y sólo le ofrece la acción MUTE a la que
# está emitiendo audio. Eso alcanza para saber que YouTube sigue sonando.
PLASMA_SERVICE = 'org.mpris.MediaPlayer2.plasma-browser-integration'
TABS_RUNNER_PATH = '/TabsRunner'
KRUNNER_IFACE = 'org.kde.krunner1'

# "(11) Tema - YouTube" -> "Tema". Los navegadores publican el título de la
# pestaña tal cual: contador de notificaciones al frente y sufijo del sitio.
_TAB_COUNT_RE = re.compile(r'^\(\d+\)\s*')
_TAB_SUFFIX_RE = re.compile(r'\s*-\s*YouTube(\s*Music)?\s*$', re.IGNORECASE)


def clean_title(title):
    return _TAB_SUFFIX_RE.sub('', _TAB_COUNT_RE.sub('', title)).strip()


def _first(value, default=''):
    """xesam:artist es una lista; xesam:title un string. Normaliza ambos."""
    if value is None:
        return default
    if isinstance(value, (dbus.Array, list, tuple)):
        return str(value[0]) if len(value) else default
    return str(value)


# --- Volumen por PipeWire (stream de audio del navegador) ---
# El Volume que YouTube expone por MPRIS es el del reproductor *dentro de la
# página*, y YouTube lo reinicia solo en cada canción (medido: 0.11 -> 0.59 al
# pasar de tema). Por eso el nivel real lo maneja el stream del navegador en
# PipeWire, una capa que la página no puede tocar, y el de la página queda
# clavado en 100%.
BROWSER_HINTS = ('brave', 'chrome', 'chromium', 'firefox', 'vivaldi', 'opera')
# pactl expresa el volumen en enteros donde 65536 == 100%.
PA_NORM = 65536.0


def _pactl(*args, parse=False):
    """Corre pactl. Con parse=True devuelve el JSON; si no, True/False."""
    cmd = ['pactl'] + (['-f', 'json'] if parse else []) + list(args)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None if parse else False
    if res.returncode != 0:
        return None if parse else False
    if not parse:
        return True
    try:
        return json.loads(res.stdout)
    except ValueError:
        return None


def browser_sink_inputs():
    """Streams de reproducción que pertenecen al navegador."""
    data = _pactl('list', 'sink-inputs', parse=True)
    if not data:
        return []
    found = []
    for si in data:
        props = si.get('properties') or {}
        haystack = ' '.join(str(props.get(k, '')) for k in (
            'application.process.binary', 'application.name', 'node.name')).lower()
        if any(hint in haystack for hint in BROWSER_HINTS):
            found.append(si)
    return found


def stream_volume():
    """Volumen 0..1 del stream del navegador, o None si no hay ninguno."""
    for si in browser_sink_inputs():
        values = [c.get('value', 0) for c in (si.get('volume') or {}).values()]
        if values:
            return max(values) / PA_NORM
    return None


# Arrastrar el slider dispara hasta ~8 escrituras por segundo y listar los
# streams en cada una duplicaría los subprocesos. Los índices casi no cambian,
# así que se cachean un rato; si una escritura falla, se invalida al toque.
_SINK_TTL = 2.0
_sink_ids = {'t': 0.0, 'ids': []}


def browser_sink_ids():
    if _sink_ids['ids'] and time.monotonic() - _sink_ids['t'] < _SINK_TTL:
        return _sink_ids['ids']
    _sink_ids['ids'] = [si['index'] for si in browser_sink_inputs()]
    _sink_ids['t'] = time.monotonic()
    return _sink_ids['ids']


def set_stream_volume(value):
    """Fija el volumen (0..1) de todos los streams del navegador."""
    pct = max(0, min(100, int(round(value * 100))))
    ok = False
    for idx in browser_sink_ids():
        if _pactl('set-sink-input-volume', str(idx), '%d%%' % pct):
            ok = True
    if not ok:
        _sink_ids['t'] = 0.0      # índices viejos: reconstruir en el próximo intento
    return ok


class YouTubePlayer:
    """Cliente MPRIS que sigue al navegador que está reproduciendo YouTube."""

    def __init__(self, bus, on_change):
        self._bus = bus
        self._on_change = on_change
        self.bus_name = None
        self.title = ''
        self.artist = ''
        self.status = 'Stopped'
        self.identity = ''
        self.art_url = ''
        # Sólo se usa cuando MPRIS perdió a YouTube: id de la pestaña que
        # está sonando, para poder saltar a ella.
        self.tab_id = None
        # youtube.com/watch no registra los handlers de MediaSession para
        # siguiente/anterior, así que el navegador los publica como false.
        # YouTube Music sí. Hay que respetarlo: si no, quedan botones muertos.
        self.can_next = False
        self.can_prev = False
        # MPRIS no define Mute: sólo Volume. Silenciar es poner Volume en 0,
        # guardando el anterior para poder restaurarlo.
        # self.volume va redondeado (para comparar sin ruido); raw_volume
        # guarda el exacto, que es el que hay que restaurar al desilenciar.
        self.volume = 0.0
        self.raw_volume = 0.0
        # Mutear = clavar el volumen de página de YouTube en 0 (en vez de 100%),
        # así el silencio afecta sólo a la pestaña de YouTube y no al resto del
        # navegador. El nivel del slider (PipeWire) queda intacto mientras tanto.
        self.muted = False
        # Bus del player cuyo volumen de página fijamos.
        self._volume_bus = None
        # Ya trasladamos el nivel audible de la página al stream de PipeWire.
        self._page_pinned = False

        self._bus.add_signal_receiver(
            self._on_props_changed,
            signal_name='PropertiesChanged',
            dbus_interface='org.freedesktop.DBus.Properties',
            path=MPRIS_PATH,
            sender_keyword='sender')
        self._bus.add_signal_receiver(
            self._on_name_owner_changed,
            signal_name='NameOwnerChanged',
            dbus_interface='org.freedesktop.DBus')
        self.refresh()

    # --- descubrimiento ---

    def _players(self):
        try:
            names = self._bus.list_names()
        except dbus.DBusException:
            return []
        return [str(n) for n in names if str(n).startswith(MPRIS_PREFIX)]

    def _props_of(self, name):
        try:
            obj = self._bus.get_object(name, MPRIS_PATH)
            return obj.GetAll(MPRIS_PLAYER_IFACE,
                              dbus_interface=dbus.PROPERTIES_IFACE)
        except dbus.DBusException:
            return None

    def _is_youtube(self, name, meta):
        blob = ' '.join([
            _first(meta.get('xesam:url')),
            _first(meta.get('mpris:artUrl')),
            _first(meta.get('xesam:comment')),
            _first(meta.get('mpris:trackid')),
        ]).lower()
        if any(h in blob for h in YT_HINTS):
            return True
        # Brave/Chromium no publican xesam:url: sólo el título de la pestaña,
        # que YouTube siempre termina en "- YouTube".
        title = _first(meta.get('xesam:title')).strip().lower()
        return title.endswith('- youtube') or title.endswith('- youtube music')

    def playing_youtube_tab(self):
        """Pestaña de YouTube que está emitiendo audio, vía el runner de
        pestañas de Plasma. Devuelve None si no hay ninguna.

        Se usa sólo como respaldo de MPRIS: da el título y permite ir a la
        pestaña, pero no play/pausa ni volumen.
        """
        try:
            obj = self._bus.get_object(PLASMA_SERVICE, TABS_RUNNER_PATH)
            matches = obj.Match('youtube', dbus_interface=KRUNNER_IFACE,
                                timeout=2)
        except dbus.DBusException:
            return None
        for m in matches:
            props = dict(m[5]) if len(m) > 5 else {}
            url = str(props.get('subtext', '')).lower()
            if not any(h in url for h in YT_HINTS):
                continue
            # MUTE = "silenciar esta pestaña", o sea que está sonando.
            actions = [str(a) for a in props.get('actions', [])]
            if 'MUTE' in actions:
                return {'id': str(m[0]), 'title': clean_title(str(m[1]))}
        return None

    def refresh(self):
        """Elige el player de YouTube: prioriza el que está sonando."""
        best = None
        best_rank = -1
        # El Volume del MPRIS nativo del navegador es un valor fijo (1.0) que no
        # refleja el volumen real de la página y encima ignora las escrituras. El
        # bueno es el de plasma-browser-integration. Lo capturamos aparte para no
        # adoptar nunca el 1.0 basura: durante un cambio de tema el player de
        # plasma parpadea de estado y el ranking elige un instante al nativo, y
        # ahí es cuando el volumen "saltaba solo" al 100%.
        plasma_vol = None
        plasma_name = None
        for name in self._players():
            props = self._props_of(name)
            if props is None:
                continue
            meta = props.get('Metadata') or {}
            if not self._is_youtube(name, meta):
                continue
            status = str(props.get('PlaybackStatus', 'Stopped'))
            # Playing > Paused > Stopped, y preferimos el actual ante empate.
            rank = {'Playing': 30, 'Paused': 20}.get(status, 10)
            # El mismo video aparece dos veces: el MPRIS nativo del navegador
            # y plasma-browser-integration. Este último trae artista y título
            # ya separados en vez del título crudo de la pestaña.
            if PLASMA_INTEGRATION in name:
                rank += 5
                plasma_name = name
                if 'Volume' in props:
                    try:
                        plasma_vol = float(props['Volume'])
                    except (TypeError, ValueError):
                        pass
            if name == self.bus_name:
                rank += 1
            if rank > best_rank:
                best_rank, best = rank, (name, props, meta, status)

        if best is None:
            # MPRIS no ve YouTube: puede que otra pestaña le haya robado la
            # sesión de medios. El runner de pestañas sigue viéndolo.
            tab = self.playing_youtube_tab()
            nuevo = (None, tab['title'] if tab else '',
                     tab['id'] if tab else None,
                     'Playing' if tab else 'Stopped')
            actual = (self.bus_name, self.title, self.tab_id, self.status)
            self.bus_name = None
            self._volume_bus = None
            self.title = tab['title'] if tab else ''
            self.tab_id = tab['id'] if tab else None
            self.status = 'Playing' if tab else 'Stopped'
            self.artist = self.identity = self.art_url = ''
            self.can_next = self.can_prev = False
            if nuevo != actual:
                self._on_change()
            return

        name, props, meta, status = best
        title = clean_title(_first(meta.get('xesam:title')))
        artist = _first(meta.get('xesam:artist'))
        # YouTube Music marca los canales de artista como "Fulano - Topic".
        if artist.endswith(' - Topic'):
            artist = artist[:-len(' - Topic')]
        identity = ''
        try:
            obj = self._bus.get_object(name, MPRIS_PATH)
            identity = str(obj.Get(MPRIS_ROOT_IFACE, 'Identity',
                                   dbus_interface=dbus.PROPERTIES_IFACE))
        except dbus.DBusException:
            pass

        art_url = _first(meta.get('mpris:artUrl'))
        can_next = bool(props.get('CanGoNext', False))
        can_prev = bool(props.get('CanGoPrevious', False))
        # Se redondea sólo para comparar: el navegador reporta el volumen con
        # micro-variaciones que dispararían un refresh por nada. El valor sin
        # redondear es el que se guarda, así el mute restaura el exacto.
        # El player al que hay que escribirle el volumen de página es el de
        # plasma; el MPRIS nativo del navegador ignora las escrituras.
        self._volume_bus = plasma_name or name
        stream_vol = stream_volume()

        # Dejamos el volumen de la página de YouTube clavado en 100%: es el que
        # YouTube reinicia por su cuenta en cada canción. El nivel audible lo
        # lleva el stream de PipeWire. La primera vez trasladamos el nivel actual
        # al stream (stream * página) para no pegar un bocinazo al subir la
        # página al 100%.
        # El volumen de página es nuestro: 100% normalmente, 0 si el usuario
        # muteó. Reescribirlo en cada desvío es lo que sostiene el mute (y el
        # nivel) a través de los cambios de canción, que es cuando YouTube lo
        # reinicia por su cuenta.
        page_target = 0.0 if self.muted else 1.0
        if plasma_vol is not None:
            if not self._page_pinned:
                # Primera vez: trasladamos el nivel audible de la página al
                # stream para no pegar un bocinazo al subirla al 100%.
                if stream_vol is not None and plasma_vol < 0.99 and not self.muted:
                    stream_vol = max(0.0, min(1.0, stream_vol * plasma_vol))
                    set_stream_volume(stream_vol)
                self._page_pinned = True
            if abs(plasma_vol - page_target) > 0.01:
                self._write_volume(page_target)

        # El volumen que la app muestra y controla es el del stream. Si PipeWire
        # no reporta stream (nada sonando todavía) conservamos el último.
        raw_volume = self.raw_volume if stream_vol is None else stream_vol
        volume = round(raw_volume, 3)
        if (name, title, artist, status, art_url, can_next, can_prev,
                volume) != (
                self.bus_name, self.title, self.artist, self.status,
                self.art_url, self.can_next, self.can_prev, self.volume):
            self.bus_name = name
            self.tab_id = None       # volvimos a tener control por MPRIS
            self.title, self.artist = title, artist
            self.status, self.identity = status, identity
            self.art_url = art_url
            self.can_next, self.can_prev = can_next, can_prev
            self.volume = volume
            self.raw_volume = raw_volume
            self._on_change()

    # --- señales ---

    def _on_props_changed(self, iface, changed, invalidated, sender=None):
        if str(iface) == MPRIS_PLAYER_IFACE:
            GLib.idle_add(self.refresh)

    def _on_name_owner_changed(self, name, old, new):
        if str(name).startswith(MPRIS_PREFIX):
            GLib.idle_add(self.refresh)

    # --- control ---

    def _call(self, method):
        if not self.bus_name:
            return
        try:
            obj = self._bus.get_object(self.bus_name, MPRIS_PATH)
            getattr(obj, method)(dbus_interface=MPRIS_PLAYER_IFACE)
        except dbus.DBusException:
            self.refresh()

    def play_pause(self):
        self._call('PlayPause')

    def next(self):
        self._call('Next')

    def previous(self):
        self._call('Previous')

    def _write_volume(self, target):
        """Escribe el volumen al player que lo controla (plasma). Devuelve
        True si se pudo escribir. No toca el estado interno ni dispara guards:
        lo usan tanto set_volume (con guard) como la reposición por cambio de
        canción (que necesita escribir aunque el estado interno ya muestre el
        valor deseado)."""
        bus = self._volume_bus or self.bus_name
        if not bus:
            return False
        try:
            obj = self._bus.get_object(bus, MPRIS_PATH)
            obj.Set(MPRIS_PLAYER_IFACE, 'Volume', dbus.Double(target),
                    dbus_interface=dbus.PROPERTIES_IFACE)
            return True
        except dbus.DBusException:
            return False

    def set_volume(self, target):
        target = min(1.0, max(0.0, target))
        # Si es casi el mismo, no gastamos un pactl de más.
        if abs(target - self.raw_volume) < 0.001:
            return
        # El nivel vive en el stream de PipeWire, no en la página de YouTube:
        # así no hay nada que YouTube pueda reiniciar al cambiar de canción.
        if not set_stream_volume(target):
            return
        self.volume = round(target, 3)
        self.raw_volume = target
        self._on_change()

    def volume_up(self):
        self.set_volume(self.raw_volume + VOLUME_STEP)

    def volume_down(self):
        self.set_volume(self.raw_volume - VOLUME_STEP)

    def toggle_mute(self):
        """Silencia sólo la pestaña de YouTube poniendo su volumen de página en
        0. No toca el nivel del slider (que vive en PipeWire), así que al
        desmutear se vuelve exactamente al volumen que había."""
        if not self.bus_name:
            return
        self.muted = not self.muted
        self._write_volume(0.0 if self.muted else 1.0)
        self._on_change()

    def raise_window(self):
        if not self.bus_name:
            # Sin MPRIS todavía podemos saltar a la pestaña por el runner.
            if self.tab_id:
                try:
                    obj = self._bus.get_object(PLASMA_SERVICE,
                                               TABS_RUNNER_PATH)
                    obj.Run(self.tab_id, '', dbus_interface=KRUNNER_IFACE,
                            timeout=2)
                except dbus.DBusException:
                    pass
            return
        try:
            obj = self._bus.get_object(self.bus_name, MPRIS_PATH)
            obj.Raise(dbus_interface=MPRIS_ROOT_IFACE)
        except dbus.DBusException:
            pass

    # --- presentación ---

    @property
    def is_playing(self):
        return self.status == 'Playing'

    @property
    def is_muted(self):
        # El mute es un estado propio (volumen de página en 0), no "el slider
        # está en cero": bajar el slider a 0 no cuenta como mutear.
        return self.muted

    def label(self):
        if not self.bus_name and not self.tab_id:
            return 'Nada sonando en YouTube'
        if not self.bus_name:
            # Modo respaldo: el runner sólo da el título de la pestaña.
            return '%s  (sin controles)' % self.title[:52]
        # Los títulos de YouTube suelen venir ya como "Artista - Tema", así
        # que anteponer el artista lo duplicaría.
        if self.artist and self.artist.lower() in self.title.lower():
            parts = [self.title]
        else:
            parts = [p for p in (self.artist, self.title) if p]
        text = ' — '.join(parts) if parts else 'YouTube'
        if len(text) > 60:
            text = text[:57] + '...'
        return text


# --- Carátulas ---

class ArtCache:
    """Carga la carátula del tema actual, cacheada por URL.

    plasma-browser-integration deja un file:// en /tmp; otros navegadores
    publican un https:// de ytimg. Se cachea porque tanto el tooltip como
    la ventana piden la misma imagen, y el archivo de /tmp cambia por tema.
    """

    def __init__(self, limit=8):
        self._cache = {}
        self._order = []
        self._limit = limit

    def get(self, url):
        if not url:
            return None
        if url in self._cache:
            return self._cache[url]
        img = self._load(url)
        self._cache[url] = img
        self._order.append(url)
        while len(self._order) > self._limit:
            self._cache.pop(self._order.pop(0), None)
        return img

    def _load(self, url):
        try:
            if url.startswith('file://'):
                path = unquote(urlparse(url).path)
                with open(path, 'rb') as fh:
                    return Image.open(io.BytesIO(fh.read())).convert('RGB')
            if url.startswith(('http://', 'https://')):
                with urlopen(url, timeout=3) as resp:
                    return Image.open(io.BytesIO(resp.read())).convert('RGB')
        except (OSError, ValueError, URLError):
            return None
        return None


def fit_art(img, w, h):
    """Recorta al centro y escala a w×h, sin deformar la imagen."""
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    new = (max(1, round(src_w * scale)), max(1, round(src_h * scale)))
    img = img.resize(new, Image.LANCZOS)
    left = (new[0] - w) // 2
    top = (new[1] - h) // 2
    return img.crop((left, top, left + w, top + h))


# --- Icono para el tray ---

def create_tray_icon_image(playing=True):
    size = 128
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    red = (255, 0, 0, 255) if playing else (130, 130, 130, 255)
    draw.rounded_rectangle([6, 24, size - 6, size - 24], radius=28, fill=red)
    if playing:
        draw.polygon([(52, 42), (52, 86), (88, 64)], fill='#ffffff')
    else:
        draw.rectangle([50, 44, 60, 84], fill='#ffffff')
        draw.rectangle([70, 44, 80, 84], fill='#ffffff')
    return img
# --- Ventana con slider de volumen (click derecho) ---

BG = '#1c1c1c'
BG_HOVER = '#2d2d2d'
TEXT = '#d4d4d4'
TEXT_DIM = '#8a8a8a'
TEXT_OFF = '#4a4a4a'
TEXT_HOVER = '#ffffff'
BORDER_COLOR = '#0a0a0a'
TRACK_BG = '#3a3a3a'
ACCENT = '#ff0000'

SLIDER_X0, SLIDER_X1 = 34, 196
SLIDER_Y = 16
SLIDER_W, SLIDER_H = 240, 32
KNOB_R = 7
# Arrastrar genera decenas de eventos por segundo: se manda a DBus como
# mucho cada 120 ms, y siempre el valor final al soltar.
SEND_INTERVAL = 0.12
# Margen para llegar con el mouse desde el icono hasta la ventana, y cierre
# rápido una vez que saliste de ella.
GRACE_BEFORE_ENTER = 6000
HIDE_AFTER_LEAVE = 700
TITLE_MAX = 58


class VolumePopup:
    """Ventana chica junto al icono: tema, slider arrastrable y controles."""

    def __init__(self, root, player, quit_cb):
        self.player = player
        self._hide_job = None
        self._dragging = False
        self._entered = False
        self._display_volume = 0.0

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.configure(bg=BORDER_COLOR)

        outer = tk.Frame(self.win, bg=BG, padx=12, pady=10)
        outer.pack(padx=1, pady=1)

        self._title_label = tk.Label(
            outer, bg=BG, fg=TEXT, font=('sans', 10, 'bold'),
            wraplength=SLIDER_W, justify='left', anchor='w')
        self._title_label.pack(fill='x')
        self._artist_label = tk.Label(
            outer, bg=BG, fg=TEXT_DIM, font=('sans', 9),
            wraplength=SLIDER_W, justify='left', anchor='w')
        self._artist_label.pack(fill='x', pady=(0, 6))

        self._canvas = tk.Canvas(outer, width=SLIDER_W, height=SLIDER_H,
                                 bg=BG, highlightthickness=0)
        self._canvas.pack()
        self._canvas.bind('<Button-1>', self._on_press)
        self._canvas.bind('<B1-Motion>', self._on_drag)
        self._canvas.bind('<ButtonRelease-1>', self._on_release)
        self._last_send = 0.0

        controls = tk.Frame(outer, bg=BG)
        controls.pack(pady=(4, 0))
        self._prev_btn = self._button(controls, '⏮', player.previous)
        self._play_btn = self._button(controls, '⏯', player.play_pause)
        self._next_btn = self._button(controls, '⏭', player.next)
        self._mute_btn = self._button(controls, '🔇', player.toggle_mute)

        # Estas dos vivían en el menú del click derecho, que ya no existe.
        extra = tk.Frame(outer, bg=BG)
        extra.pack(fill='x', pady=(6, 0))
        self._tab_btn = self._link(extra, 'Ir a la pestaña', player.raise_window)
        self._link(extra, 'Salir', quit_cb, side='right')

        # En Wayland una ventana override-redirect no recibe foco, así que
        # <FocusOut> no sirve: se cierra cuando el mouse se va.
        for w in (self.win, outer):
            w.bind('<Enter>', self._cancel_hide)
            w.bind('<Leave>', self._schedule_hide)
        self.win.bind('<Escape>', lambda _e: self.hide())

    # --- botones ---

    def _button(self, parent, glyph, command):
        btn = tk.Label(parent, text=glyph, bg=BG, fg=TEXT,
                       font=('sans', 15), padx=14, pady=2, cursor='hand2')
        btn.pack(side='left', padx=2)
        btn._enabled = True
        btn.bind('<Enter>', lambda _e: btn._enabled and btn.configure(
            bg=BG_HOVER, fg=TEXT_HOVER))
        btn.bind('<Leave>', lambda _e: btn.configure(
            bg=BG, fg=TEXT if btn._enabled else TEXT_OFF))
        # El click llega en el hilo de tkinter y la conexión DBus la atiende
        # el hilo de GLib: hay que cruzar de vuelta o se cuelga.
        btn.bind('<Button-1>',
                 lambda _e: btn._enabled and GLib.idle_add(command))
        return btn

    def _link(self, parent, text, command, side='left'):
        lbl = tk.Label(parent, text=text, bg=BG, fg=TEXT_DIM,
                       font=('sans', 8), cursor='hand2')
        lbl.pack(side=side)
        lbl._enabled = True
        lbl.bind('<Enter>', lambda _e: lbl._enabled and lbl.configure(
            fg=TEXT_HOVER))
        lbl.bind('<Leave>', lambda _e: lbl.configure(
            fg=TEXT_DIM if lbl._enabled else TEXT_OFF))
        lbl.bind('<Button-1>',
                 lambda _e: lbl._enabled and GLib.idle_add(command))
        return lbl

    def _set_enabled(self, btn, enabled):
        btn._enabled = enabled
        btn.configure(fg=TEXT if enabled else TEXT_OFF,
                      cursor='hand2' if enabled else '')

    # --- slider ---

    def _value_from_x(self, x):
        span = SLIDER_X1 - SLIDER_X0
        return min(1.0, max(0.0, (x - SLIDER_X0) / span))

    def _on_press(self, event):
        self._dragging = True
        self._apply(self._value_from_x(event.x))

    def _on_drag(self, event):
        if self._dragging:
            self._apply(self._value_from_x(event.x))

    def _on_release(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self._apply(self._value_from_x(event.x), force=True)

    def _apply(self, value, force=False):
        self._display_volume = value
        self._draw()
        now = time.monotonic()
        if force or now - self._last_send >= SEND_INTERVAL:
            self._last_send = now
            GLib.idle_add(self.player.set_volume, value)

    def _volume_glyph(self):
        if self.player.is_muted or self._display_volume <= 0.001:
            return '🔇'
        return '🔈' if self._display_volume < 0.5 else '🔊'

    def _draw(self):
        c = self._canvas
        c.delete('all')
        v = self._display_volume
        x = SLIDER_X0 + v * (SLIDER_X1 - SLIDER_X0)
        c.create_text(12, SLIDER_Y, text=self._volume_glyph(),
                      fill=TEXT_DIM, font=('sans', 11))
        c.create_line(SLIDER_X0, SLIDER_Y, SLIDER_X1, SLIDER_Y,
                      fill=TRACK_BG, width=4, capstyle='round')
        if x > SLIDER_X0 + 1:
            c.create_line(SLIDER_X0, SLIDER_Y, x, SLIDER_Y,
                          fill=ACCENT, width=4, capstyle='round')
        c.create_oval(x - KNOB_R, SLIDER_Y - KNOB_R,
                      x + KNOB_R, SLIDER_Y + KNOB_R,
                      fill='#ffffff', outline='')
        c.create_text(SLIDER_X1 + 24, SLIDER_Y, text='%d%%' % round(v * 100),
                      fill=TEXT, font=('sans', 9))

    # --- ciclo de vida ---

    def refresh(self):
        """Repinta con el estado actual. Sólo desde el hilo de tkinter."""
        p = self.player
        # Mientras se arrastra manda el usuario, no el navegador: pisar el
        # valor acá haría saltar el knob bajo el dedo.
        if not self._dragging:
            self._display_volume = p.raw_volume
        # Los títulos de YouTube son larguísimos y estiran la ventana: se
        # cortan a dos líneas de ancho aproximado.
        title = p.title or 'Nada sonando en YouTube'
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX - 1].rstrip() + '…'
        self._title_label.configure(text=title)
        self._artist_label.configure(text=p.artist or '')
        self._play_btn.configure(text='⏸' if p.is_playing else '▶')
        self._mute_btn.configure(text='🔊' if p.is_muted else '🔇')
        self._set_enabled(self._prev_btn, p.can_prev)
        self._set_enabled(self._next_btn, p.can_next)
        self._set_enabled(self._play_btn, bool(p.bus_name))
        self._set_enabled(self._mute_btn, bool(p.bus_name))
        # Sin MPRIS pero con pestaña detectada, saltar a ella sigue andando.
        puede_ir = bool(p.bus_name or p.tab_id)
        self._tab_btn.configure(fg=TEXT_DIM if puede_ir else TEXT_OFF)
        self._tab_btn._enabled = puede_ir
        self._draw()

    def _cancel_hide(self, _e=None):
        self._entered = True
        if self._hide_job is not None:
            self.win.after_cancel(self._hide_job)
            self._hide_job = None

    def _schedule_hide(self, _e=None, delay=HIDE_AFTER_LEAVE):
        if self._hide_job is not None:
            self.win.after_cancel(self._hide_job)
        self._hide_job = self.win.after(delay, self.hide)

    def is_visible(self):
        return bool(self.win.winfo_viewable())

    def toggle(self, x, y):
        self.hide() if self.is_visible() else self.show(x, y)

    def show(self, x, y):
        self.refresh()
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        h = self.win.winfo_reqheight()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        # El icono está pegado al borde: hay que plegar la ventana hacia
        # adentro de la pantalla, no dibujarla fuera.
        px = min(max(x - w // 2, 8), sw - w - 8)
        py = y - h - 12 if y > sh // 2 else y + 12
        py = min(max(py, 8), sh - h - 8)
        self.win.geometry('+%d+%d' % (px, py))
        self.win.deiconify()
        self.win.lift()
        self.win.attributes('-topmost', True)
        # Al abrirse, el mouse todavía está sobre el icono del tray, no sobre
        # la ventana: sin este margen se cerraría antes de que llegues a ella.
        self._entered = False
        self._schedule_hide(delay=GRACE_BEFORE_ENTER)

    def hide(self):
        self._cancel_hide()
        self._dragging = False
        self.win.withdraw()


# --- Tray icon vía KDE StatusNotifierItem (DBus) ---

class TrayIcon(dbus.service.Object):
    IFACE = 'org.kde.StatusNotifierItem'
    BUS_NAME = 'org.kde.StatusNotifierItem-youtube-%d' % os.getpid()

    def __init__(self, app):
        self.app = app
        self._bus = dbus.SessionBus()
        bus_name = dbus.service.BusName(self.BUS_NAME, self._bus,
                                        allow_replacement=True,
                                        replace_existing=True)
        super().__init__(bus_name, '/StatusNotifierItem')

        self._pixmaps = {
            state: self._to_pixmap(
                create_tray_icon_image(state).resize((48, 48)))
            for state in (True, False)
        }
        self._empty_pixmap = dbus.Array([], signature='(iiay)')
        self._art_pixmap_url = None
        self._art_pixmap_cache = self._empty_pixmap

        watcher = self._bus.get_object('org.kde.StatusNotifierWatcher',
                                       '/StatusNotifierWatcher')
        watcher.RegisterStatusNotifierItem(
            self.BUS_NAME,
            dbus_interface='org.kde.StatusNotifierWatcher')

    def _to_pixmap(self, img):
        """RGBA -> ARGB crudo, que es lo que pide StatusNotifierItem.

        Se arma con slicing sobre bytes y se manda como ByteArray. Hacerlo
        pixel por pixel creaba un dbus.Byte por canal (20k objetos para el
        tooltip, ~1 MB y 38 ms) cada vez que Plasma leía las propiedades.
        """
        img = img.convert('RGBA')
        w, h = img.size
        rgba = img.tobytes()
        argb = bytearray(len(rgba))
        argb[0::4] = rgba[3::4]   # A
        argb[1::4] = rgba[0::4]   # R
        argb[2::4] = rgba[1::4]   # G
        argb[3::4] = rgba[2::4]   # B
        return dbus.Array([
            dbus.Struct((dbus.Int32(w), dbus.Int32(h),
                         dbus.ByteArray(bytes(argb))), signature=None)
        ], signature='(iiay)')

    def refresh(self):
        self.NewIcon()
        self.NewToolTip()

    # Click derecho: ventana con el slider y los controles (ver ContextMenu).
    # Click izquierdo: play/pausa. Rueda: volumen, si el mouse la tiene.
    @dbus.service.method(IFACE, in_signature='ii')
    def Activate(self, x, y):
        self.app.player.play_pause()

    @dbus.service.method(IFACE, in_signature='ii')
    def ContextMenu(self, x, y):
        self.app.request_popup(int(x), int(y))

    @dbus.service.method(IFACE, in_signature='ii')
    def SecondaryActivate(self, x, y):
        self.app.player.raise_window()

    @dbus.service.method(IFACE, in_signature='is')
    def Scroll(self, delta, orientation):
        if int(delta) > 0:
            self.app.player.volume_up()
        else:
            self.app.player.volume_down()

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ss',
                         out_signature='v')
    def Get(self, interface, prop):
        props = self._props()
        if prop in props:
            return props[prop]
        raise dbus.exceptions.DBusException(
            'org.freedesktop.DBus.Error.UnknownProperty',
            'Unknown property: %s' % prop)

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='s',
                         out_signature='a{sv}')
    def GetAll(self, interface):
        return self._props()

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ssv')
    def Set(self, interface, prop, value):
        pass

    def _art_pixmap(self):
        """Miniatura de la carátula para el tooltip, cacheada por URL.

        Plasma lee las propiedades cada vez que muestra el tooltip, así que
        sin cache esto se recalculaba a cada hover.
        """
        url = self.app.player.art_url
        if url == self._art_pixmap_url:
            return self._art_pixmap_cache
        img = self.app.art.get(url)
        pixmap = (self._empty_pixmap if img is None
                  else self._to_pixmap(fit_art(img, 96, 54)))
        self._art_pixmap_url = url
        self._art_pixmap_cache = pixmap
        return pixmap

    def _props(self):
        p = self.app.player
        tooltip = dbus.Struct(
            ('', self._art_pixmap(), 'YouTube Widget', p.label()),
            signature=None)
        return {
            'Category': 'ApplicationStatus',
            'Id': 'youtube-widget',
            'Title': 'YouTube Widget',
            'Status': 'Active',
            'WindowId': dbus.Int32(0),
            'IconName': '',
            'IconPixmap': self._pixmaps[p.is_playing],
            'OverlayIconName': '',
            'OverlayIconPixmap': self._empty_pixmap,
            'AttentionIconName': '',
            'AttentionIconPixmap': self._empty_pixmap,
            'AttentionMovieName': '',
            'ToolTip': tooltip,
            # Sin menú declarado, Plasma no dibuja uno propio y nos manda el
            # click derecho a ContextMenu(), que es donde abrimos la ventana.
            'Menu': dbus.ObjectPath('/NO_DBUSMENU'),
            'ItemIsMenu': dbus.Boolean(False),
            'IconThemePath': '',
        }

    @dbus.service.signal(IFACE)
    def NewIcon(self):
        pass

    @dbus.service.signal(IFACE)
    def NewToolTip(self):
        pass


# --- App principal ---

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        # Tkinter/Tcl no es thread-safe y DBus corre en el hilo de GLib: ese
        # hilo sólo encola acá y el hilo de tkinter drena en _drain_events.
        # Nunca tocar self.popup desde el hilo de GLib.
        self._events = queue.Queue()
        self.art = ArtCache()
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._loop = GLib.MainLoop()
        self._bus = dbus.SessionBus()
        # El primer refresh() del player ocurre dentro de su __init__, antes
        # de que exista el tray; _on_player_change tiene que tolerarlo.
        self.tray = None
        self.player = YouTubePlayer(self._bus, self._on_player_change)
        self.tray = TrayIcon(self)
        self.popup = VolumePopup(self.root, self.player, self.quit)
        # Red de seguridad: algunos players no emiten PropertiesChanged.
        GLib.timeout_add_seconds(5, self._poll)
        self.root.after(20, self._drain_events)

    def request_popup(self, x, y):
        self._events.put(('popup', (x, y)))

    def _drain_events(self):
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == 'popup':
                    self.popup.toggle(*payload)
                elif kind == 'refresh_popup':
                    if self.popup.is_visible():
                        self.popup.refresh()
                elif kind == 'quit':
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(20, self._drain_events)

    def _on_player_change(self):
        if self.tray is not None:
            self.tray.refresh()
        self._events.put(('refresh_popup', None))

    def _poll(self):
        self.player.refresh()
        return True

    def quit(self):
        self._loop.quit()
        self._events.put(('quit', None))

    def run(self):
        # Salir limpio ante `systemctl --user stop` (SIGTERM) y Ctrl-C.
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, self.quit)
        threading.Thread(target=self._loop.run, daemon=True).start()
        self.root.mainloop()


def acquire_single_instance_lock():
    """Evita iconos duplicados en la bandeja si la app se lanza dos veces.

    Se usa flock y no un pidfile: el kernel libera el candado solo, incluso
    si el proceso muere sin poder limpiar. El fd queda abierto a proposito
    durante toda la vida del proceso, que es lo que sostiene el candado.
    """
    runtime = os.environ.get('XDG_RUNTIME_DIR') or '/tmp'
    fd = open(os.path.join(runtime, 'youtube-tray.lock'), 'w')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('youtube-tray ya esta corriendo.', file=sys.stderr)
        raise SystemExit(1)
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


if __name__ == '__main__':
    _lock = acquire_single_instance_lock()
    App().run()
