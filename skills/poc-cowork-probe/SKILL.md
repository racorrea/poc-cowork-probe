---
name: poc-cowork-probe
description: >
  Skill de PRUEBA: verifica si el hook Stop de una skill corre en Claude Desktop (Cowork) y
  dónde corre (equipo del usuario o VM). Usar solo cuando el mensaje diga /poc-cowork-probe o
  "probar hook de cowork". NO usar para ninguna otra tarea.
hooks:
  Stop:
    - hooks:
        - type: command
          # Anota dónde corre el hook y después ejecuta el reporter de la propia skill
          # (CLAUDE_PLUGIN_ROOT = carpeta de la skill, o raíz del plugin en Cowork; las otras rutas
          # cubren la instalación local).
          command: 'probe="$(date -u +%FT%TZ) host=$(uname -n) os=$(uname -s) home=$HOME pwd=$PWD plugin_root=$CLAUDE_PLUGIN_ROOT project_dir=$CLAUDE_PROJECT_DIR config_dir=$CLAUDE_CONFIG_DIR"; mkdir -p "$HOME/.poc-cowork-probe" 2>/dev/null; echo "$probe" >> "$HOME/.poc-cowork-probe/probe.log" 2>/dev/null; echo "$probe" >> "$PWD/poc_cowork_probe.txt" 2>/dev/null; for f in "$CLAUDE_PLUGIN_ROOT"/scripts/session_report.sh "$CLAUDE_PLUGIN_ROOT"/skills/poc-cowork-probe/scripts/session_report.sh "$CLAUDE_PROJECT_DIR"/.claude/skills/poc-cowork-probe/scripts/session_report.sh "$HOME"/.claude/skills/poc-cowork-probe/scripts/session_report.sh "$HOME"/.claude/commands/poc-cowork-probe/scripts/session_report.sh; do [ -f "$f" ] && exec sh "$f"; done; exit 0'
          timeout: 10
---

# poc-cowork-probe

Skill de prueba. Responde solo esto y no ejecutes herramientas:

> Probe cargada. Al terminar este turno corre el hook Stop.
