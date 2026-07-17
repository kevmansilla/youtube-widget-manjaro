#!/bin/bash
# La ruta se resuelve desde la ubicacion del propio script: asi la app sigue
# funcionando si se mueve la carpeta, sin tener que editar nada mas.
cd "$(dirname "$(readlink -f "$0")")"
exec python3 youtube_tray.py
