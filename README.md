# youtube-widget

Mini icono en el system tray (KDE/Wayland) para controlar la música de YouTube que está sonando en tu navegador, sin cambiar de ventana.

Usa **MPRIS vía DBus**: se engancha a la sesión de YouTube que ya tenés abierta y logueada en Brave / Firefox / Chromium. No hay que volver a loguearse ni instalar nada aparte.

Si estás mirando algo que no es YouTube (Netflix, HBO Max, etc.), el icono lo ignora a propósito.

## Controles

| Acción | Cómo |
|---|---|
| Ver qué suena (con carátula) | Pasar el mouse por encima |
| Abrir la ventana de control | **Click derecho** en el icono |
| Regular el volumen | Arrastrar el slider de la ventana |
| Play / Pausa | Click izquierdo, o el botón ⏯ |
| Silenciar | Botón 🔇 de la ventana |
| Siguiente / Anterior | Botones ⏮ ⏭ (ver limitaciones abajo) |
| Ir a la pestaña | Click del medio, o el link de la ventana |
| Subir / bajar volumen (alternativa) | Rueda del mouse sobre el icono (de a 5%) |

El icono es rojo con ▶ cuando está reproduciendo, y gris con ❚❚ cuando está en pausa.

## La ventana del volumen

El **click derecho** abre una ventanita junto al icono. Reemplaza al menú del tray: tiene todos los controles.

```
┌──────────────────────────────┐
│  Enzo Celis – Garganta´i…    │
│  La Voz del Río              │
│                              │
│  🔈 ━━━━━●─────────    40%   │
│                              │
│    ⏮     ⏸     ⏭     🔇     │
│                              │
│  Ir a la pestaña      Salir  │
└──────────────────────────────┘
```

Se cierra sola: tenés 6 segundos para llegar con el mouse desde el icono, y una vez que entrás, se cierra 700 ms después de que salgas. También con Escape.

Mientras arrastrás, el volumen se manda al navegador como mucho cada 120 ms (y siempre el valor exacto al soltar), así el arrastre se siente fluido sin inundar DBus.

Silenciar guarda el volumen exacto y lo restaura tal cual al desilenciar (MPRIS no define `Mute`: sólo `Volume`, así que silenciar es ponerlo en 0).

## Qué NO se puede hacer (y por qué)

Estas son limitaciones del navegador y de YouTube, no de la app:

- **No se puede mostrar cuál es el tema siguiente ni el anterior.** El reproductor sólo expone `org.mpris.MediaPlayer2.Player`; no implementa la interfaz `TrackList`, así que la cola de reproducción no está publicada en DBus. La única forma de leerla sería una extensión de navegador que mire el DOM de la página.
- **El tooltip no puede tener botones.** Lo dibuja Plasma y la app sólo aporta texto e imagen; tampoco recibe aviso del hover. Por eso los controles viven en la ventana del click derecho.
- **El menú del tray no puede tener un slider.** `com.canonical.dbusmenu` sólo define ítems, separadores, iconos y checkboxes. Por eso el click derecho abre una ventana propia en vez del menú de Plasma: es la única forma de tener un slider arrastrable.
- **Si otra pestaña reproduce algo, YouTube desaparece de MPRIS.** El navegador publica una sola sesión de medios por fuente, así que el widget de medios de Plasma directamente pierde de vista a YouTube. Esta app lo resuelve con un respaldo (ver abajo): el icono sigue en rojo, aunque sin controles.
- **"Anterior" casi nunca retrocede.** YouTube declara `CanGoPrevious=true`, pero su handler reinicia el tema actual en vez de ir al anterior. Comprobado llamándolo directo al reproductor, salteando esta app.
- **"Siguiente" depende de la página.** Funciona cuando YouTube registra los handlers de MediaSession (típico en playlists y en YouTube Music). En algunos videos publica `CanGoNext=false` y no hay nada que hacer.

La app lee `CanGoNext` / `CanGoPrevious` y **apaga los botones cuando no están disponibles**, así que si los ves grises no está roto: es que YouTube no lo permite en esa página.

## Dependencias

Ya vienen con el sistema en Manjaro/KDE (`python-dbus`, `python-gobject`, `python-pillow`). No necesita venv. Si faltara alguna:

```bash
sudo pacman -S python-dbus python-gobject python-pillow
```

Para la mejor metadata (artista y título separados en vez del título crudo de la pestaña), conviene tener la extensión **Plasma Integration** en el navegador. La app funciona igual sin ella.

## Ejecutar desde terminal

```bash
./youtube-tray.sh
```

## Instalar como app de escritorio

```bash
cp youtube-tray.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/
```

### Inicio automático

```bash
cp youtube-tray.desktop ~/.config/autostart/
```

## Correr como servicio (systemd user)

Igual que en `popup-linux`, recomendado para que se reinicie solo si falla:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/youtube-tray.service <<EOF
[Unit]
Description=YouTube Widget - control de musica desde el system tray
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=$HOME/youtube-widget-manjaro/youtube-tray.sh
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now youtube-tray.service
```

### Comandos útiles

```bash
systemctl --user restart youtube-tray     # reiniciar tras cambios en el codigo
systemctl --user status youtube-tray      # ver si esta corriendo
journalctl --user -u youtube-tray -f      # logs en vivo
```

## Una sola instancia

La app toma un candado (`flock` sobre `$XDG_RUNTIME_DIR/youtube-tray.lock`) al arrancar, así que lanzarla dos veces no duplica el icono en la bandeja: la segunda avisa *"youtube-tray ya esta corriendo"* y sale con código 1. Es seguro tenerla en autostart y además lanzarla desde el menú.

Si alguna vez ves iconos de más, quedaron procesos viejos:

```bash
pkill -f youtube_tray.py     # los mata a todos
./youtube-tray.sh            # arranca uno limpio
```

## Cómo funciona

- `YouTubePlayer` — cliente MPRIS. Descubre los buses `org.mpris.MediaPlayer2.*`, se queda con el que suena YouTube y escucha `PropertiesChanged` / `NameOwnerChanged` para actualizarse al toque (más un poll cada 5s de red de seguridad).
- `TrayIcon` — `org.kde.StatusNotifierItem`, el mismo patrón que usa `popup-linux`.
- Respaldo por pestañas — si MPRIS pierde a YouTube, se consulta el `/TabsRunner` de la extensión de Plasma (`org.kde.krunner1`). Lista **todas** las pestañas sin importar cuál tiene el foco, y sólo le ofrece la acción `MUTE` a la que está emitiendo audio: con eso alcanza para saber que YouTube sigue sonando y mantener el icono rojo. La consulta cuesta ~3 ms y sólo se hace cuando MPRIS falla.
- `VolumePopup` — la ventana en tkinter con el slider. Corre en el hilo principal; DBus corre en el hilo de GLib y se comunican por una cola (tkinter no es thread-safe).

### Detalle: por qué se prefiere plasma-browser-integration

El mismo video aparece dos veces en DBus: el MPRIS nativo del navegador y el de la extensión de Plasma. Se prefiere el de Plasma porque trae artista y título ya separados; el nativo del navegador sólo publica el título crudo de la pestaña (`(11) Tema - YouTube`), que hay que limpiar a mano.
