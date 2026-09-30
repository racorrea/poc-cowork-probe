# poc-cowork-probe

Skill de prueba: verifica si el hook `Stop` definido en el frontmatter de una skill corre en
Claude Desktop (Cowork) y dónde corre (equipo del usuario o VM de Cowork).

## Contenido

```
skills/poc-cowork-probe/
  SKILL.md                         # hook Stop: anota dónde corre y ejecuta el reporter
  scripts/session_report.sh   # lanzador (nunca falla ni bloquea)
  scripts/session_report.py   # reporter: resume el transcript y lo envía
```

El reporter lee el destino de `~/.koda/telemetry.json` (`{"endpoint": "..."}`) o de las variables
`KODA_TELEMETRY_ENDPOINT` / `KODA_TELEMETRY_KEY`. La skill no trae ningún destino ni key.

## Prueba

1. Registrar la skill apuntando `skill_md_url` al `SKILL.md` en crudo del repo.
2. En una tarea de Cowork escribir `/poc-cowork-probe` y esperar la respuesta.
3. Revisar:

| Evidencia | Significado |
|---|---|
| Línea nueva en `~/.poc-cowork-probe/probe.log` del equipo + payload con `entrypoint: local-agent` en el destino | El hook corre en el equipo y puede enviar |
| Solo `poc_cowork_probe.txt` en los outputs de la tarea, con otro `host` | El hook corre en la VM (salida limitada) |
| Nada | Cowork no corre hooks de skills |
