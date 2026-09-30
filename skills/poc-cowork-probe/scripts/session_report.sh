#!/bin/sh
# Hook Stop: reporta las métricas agregadas de la sesión de Claude Code.
# Nunca falla ni bloquea: ante cualquier problema del entorno sale 0 en silencio.
DIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd) || exit 0
PY=$(command -v python3 2>/dev/null) || exit 0
# En macOS, /usr/bin/python3 es un shim que abre el instalador de Command Line Tools si faltan.
if [ "$PY" = "/usr/bin/python3" ] && [ "$(uname)" = "Darwin" ]; then
  xcode-select -p >/dev/null 2>&1 || exit 0
fi
"$PY" "$DIR/session_report.py" >/dev/null 2>&1
exit 0
