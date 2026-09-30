"""Hook Stop de KODA: resume la sesión de Claude Code a partir del transcript y la reporta.

Solo envía agregados (tokens, tiempos, skills, tools, líneas de código, editor, koda_id),
nunca contenido: ni prompts, ni código, ni rutas de archivos. El email del dev (git config)
solo va al endpoint de usrv-koda, nunca a otro destino.
Nunca bloquea ni molesta: lee el stdin del hook, se desacopla con fork y el proceso padre
sale 0 enseguida. Cada resumen se guarda primero en ~/.koda/spool/ y se borra al enviarse;
lo que no se pudo enviar se reintenta en el próximo turno. Solo biblioteca estándar (3.8+).
"""

import glob
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # Windows: sin candado entre ejecuciones
    fcntl = None

REPORTER_VERSION = "0.6.0"
KODA_HOME = os.path.expanduser("~/.koda")
CONFIG_PATH = os.path.join(KODA_HOME, "telemetry.json")
SPOOL_DIR = os.path.join(KODA_HOME, "spool")
LOG_PATH = os.path.join(KODA_HOME, "telemetry.log")
LOCK_PATH = os.path.join(KODA_HOME, "report.lock")
# Si no está telemetry.json, la config viene de estas env vars (managed settings de Claude Code).
ENDPOINT_ENV, API_KEY_ENV = "KODA_TELEMETRY_ENDPOINT", "KODA_TELEMETRY_KEY"
# Ruta del endpoint de usrv-koda: solo ahí se manda el email del dev (nunca a un webhook de prueba).
SESSION_USAGE_PATH = "/v1/metrics/session-usage"
INACTIVITY_THRESHOLD_SECONDS = 300
SEND_TIMEOUT_SECONDS = 3
KODA_ID_KEYS = ("koda_id", "initiative_id")
COMMAND_NAME_RE = re.compile(r"<command-name>/?([\w:.-]+)</command-name>")
# Convención de rama del builder: feature/koda-<koda_id>. Se exige la forma completa del id
# para no confundir valores de ejemplo (KODA-INI-ABC) con iniciativas reales.
KODA_ID_RE = re.compile(r"KODA-INI-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
LOCAL_COMMAND_MARK = "<local-command-"
INTERRUPTED_MARK = "[Request interrupted by user"
GIT_COMMIT_RE = re.compile(r"\bgit\b(?:\s+-[Cc]\s+\S+)*\s+commit\b")
EDITORS_BY_BUNDLE = {"com.microsoft.VSCode": "vscode", "com.microsoft.VSCodeInsiders": "vscode",
                     "com.todesktop.230313mzl4w4u92": "cursor", "com.exafunction.windsurf": "windsurf"}
NO_EXTENSION = "(sin extensión)"
# Mensajes que el sistema inyecta como "user" en transcripts sin campo origin (versiones viejas).
SYSTEM_MESSAGE_MARKS = ("<task-notification>", "<agent-message")
# Tools con las que la IA se detiene a esperar una respuesta del dev.
DEVELOPER_INPUT_TOOLS = ("AskUserQuestion", "ExitPlanMode")
# Tools que lanzan un subagente ("Task" en versiones viejas de Claude Code).
AGENT_TOOLS = ("Agent", "Task")
# Aviso de fin de un subagente en segundo plano; de él solo se leen ids y estado.
TASK_NOTIFICATION_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
# Argumentos de las tools de archivos donde se busca el koda_id de la iniciativa.
FILE_PATH_KEYS = ("file_path", "path", "notebook_path")
PHASE_KEYWORDS = (
    ("plan", ("plan", "design", "diseñ", "architect", "arquitect")),
    ("implement", ("implement", "build", "develop", "desarroll", "construi", "code", "codific", "write")),
    ("test", ("test", "qa", "prueba")),
    ("review", ("review", "revis", "audit")),
    ("validate", ("validat", "valida", "verif", "lint", "fix", "corrig", "refine", "refina")),
    ("explore", ("explor", "search", "research", "investig", "busca", "analy", "anali", "find", "locat")),
    ("document", ("document",)),
)
# Categoría de cada archivo editado, para separar código productivo de tests, docs y generados.
GENERATED_MARKERS = ("/mocks/", "/generated/", ".pb.go", "_gen.go", "_generated.")
LOCK_FILES = ("go.sum", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock")
TEST_FILE_RE = re.compile(r"(_test\.go|\.(test|spec)\.[jt]sx?|_test\.py)$|^test_.*\.py$")
TEST_DIR_MARKERS = ("/test/", "/tests/", "/__tests__/", "/testdata/")
DOC_EXTENSIONS = (".md", ".mdx", ".rst", ".txt", ".adoc")
CONFIG_EXTENSIONS = (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg")
NO_SKILL = "(sin skill)"
NO_KODA_ID = "(sin iniciativa)"


def log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (datetime.now(timezone.utc).isoformat(), message))
    except OSError:
        pass


def parse_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def find_koda_ids(value, found):
    """Recorre los argumentos de una tool y junta los valores de las claves koda_id."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in KODA_ID_KEYS and isinstance(item, str) and item.strip():
                found.add(item.strip())
            else:
                find_koda_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            find_koda_ids(item, found)


def is_installed_skill(name, cwd):
    """Distingue una skill invocada por slash de un comando nativo (/clear, /usage…)."""
    if ":" in name:
        return True
    roots = [os.path.expanduser("~/.claude/skills")]
    if cwd:
        roots.append(os.path.join(cwd, ".claude", "skills"))
    return any(os.path.isdir(os.path.join(root, name)) for root in roots)


def count_skill(skills, name, ts_raw):
    ts = parse_ts(ts_raw)
    first_at = datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else None
    entry = skills.setdefault(name, {"name": name, "count": 0, "first_at": first_at})
    entry["count"] += 1


def git_output(cwd, args):
    try:
        out = subprocess.run(["git"] + (["-C", cwd] if cwd else []) + args, capture_output=True,
                             text=True, timeout=2)
        return (out.stdout.strip() or None) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def repo_info(cwd):
    info = {"name": os.path.basename(cwd) if cwd else None, "remote": None, "branch": None}
    if not cwd:
        return info
    info["remote"] = git_output(cwd, ["remote", "get-url", "origin"])
    info["branch"] = git_output(cwd, ["symbolic-ref", "--short", "-q", "HEAD"])
    if info["remote"]:
        # Una URL https puede traer usuario:token; nunca se envía.
        info["remote"] = re.sub(r"//[^/@]+@", "//", info["remote"])
        info["name"] = re.sub(r"\.git$", "", info["remote"].rstrip("/").split("/")[-1].split(":")[-1])
    return info


def count_hunk_lines(hunks):
    added = removed = 0
    for hunk in hunks if isinstance(hunks, list) else []:
        for line in (hunk.get("lines") or []) if isinstance(hunk, dict) else []:
            if isinstance(line, str) and line.startswith("+"):
                added += 1
            elif isinstance(line, str) and line.startswith("-"):
                removed += 1
    return added, removed


def add_code_change(result, changes):
    """Suma las líneas de un Edit/Write (structuredPatch) o de un Bash que editó archivos."""
    if not isinstance(result, dict):
        return
    edited = []
    path = result.get("filePath")
    if isinstance(path, str) and isinstance(result.get("structuredPatch"), list):
        content = result.get("content")
        if result.get("type") == "create" and not result["structuredPatch"] and isinstance(content, str):
            # Un archivo nuevo no trae patch: todo su contenido son líneas agregadas.
            edited.append((path, len(content.splitlines()), 0))
        else:
            edited.append((path,) + count_hunk_lines(result["structuredPatch"]))
        if result.get("type") == "create":
            changes["files_created"].add(path)
        if result.get("userModified"):
            changes["edits_adjusted_by_developer"] += 1
    bash_diff = result.get("bashEditDiff")
    if isinstance(bash_diff, dict):
        for item in bash_diff.get("files") or []:
            if isinstance(item, dict) and isinstance(item.get("filePath"), str):
                edited.append((item["filePath"],) + count_hunk_lines(item.get("hunks")))
    for path, added, removed in edited:
        acc = changes["files"].setdefault(path, [0, 0])
        acc[0] += added
        acc[1] += removed


def classify_file(path):
    """code, test, docs, config o generated, según la ruta (la ruta en sí no se envía)."""
    normalized = "/" + path.replace(os.sep, "/").lstrip("/").lower()
    name = os.path.basename(normalized)
    if name in LOCK_FILES or any(marker in normalized for marker in GENERATED_MARKERS):
        return "generated"
    if TEST_FILE_RE.search(name) or any(marker in normalized for marker in TEST_DIR_MARKERS):
        return "test"
    if name.endswith(DOC_EXTENSIONS) or "/docs/" in normalized:
        return "docs"
    if name.endswith(CONFIG_EXTENSIONS):
        return "config"
    return "code"


def code_changes_summary(changes):
    """Solo cantidades y tipos de archivo: las rutas no salen de la máquina."""
    by_type = {}
    by_category = {}
    for path, (added, removed) in changes["files"].items():
        category = classify_file(path)
        acc = by_category.setdefault(category, {"category": category, "files": 0,
                                                "lines_added": 0, "lines_removed": 0})
        acc["files"] += 1
        acc["lines_added"] += added
        acc["lines_removed"] += removed
        extension = os.path.splitext(path)[1].lower() or NO_EXTENSION
        acc = by_type.setdefault(extension, {"file_type": extension, "files": 0,
                                             "lines_added": 0, "lines_removed": 0})
        acc["files"] += 1
        acc["lines_added"] += added
        acc["lines_removed"] += removed
    return {
        "lines_added": sum(a for a, _ in changes["files"].values()),
        "lines_removed": sum(r for _, r in changes["files"].values()),
        "files_changed": len(changes["files"]),
        "files_created": len(changes["files_created"]),
        "edits_adjusted_by_developer": changes["edits_adjusted_by_developer"],
        "by_category": sorted(by_category.values(), key=lambda r: -(r["lines_added"] + r["lines_removed"])),
        "by_file_type": sorted(by_type.values(), key=lambda r: -(r["lines_added"] + r["lines_removed"])),
    }


def detect_editor(env):
    """El hook hereda el entorno de Claude Code, que dice desde qué editor o terminal corre."""
    if "JetBrains" in env.get("TERMINAL_EMULATOR", ""):
        return "jetbrains"
    if env.get("CURSOR_TRACE_ID"):
        return "cursor"
    bundle = env.get("__CFBundleIdentifier", "")
    if bundle in EDITORS_BY_BUNDLE:
        return EDITORS_BY_BUNDLE[bundle]
    if env.get("TERM_PROGRAM") == "vscode":
        return "vscode"
    return env.get("TERM_PROGRAM") or "unknown"


def environment_info(env, entrypoint, permission_mode, effort):
    if env.get("CLAUDE_CODE_USE_BEDROCK"):
        provider, region = "bedrock", env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    elif env.get("CLAUDE_CODE_USE_VERTEX"):
        provider, region = "vertex", env.get("CLOUD_ML_REGION")
    elif env.get("CLAUDE_CODE_USE_FOUNDRY"):
        provider, region = "foundry", None
    else:
        provider, region = "anthropic", None
    return {
        "editor": detect_editor(env),
        "editor_version": env.get("TERM_PROGRAM_VERSION"),
        "ide_integration": bool(env.get("CLAUDE_CODE_SSE_PORT")),
        "entrypoint": entrypoint or env.get("CLAUDE_CODE_ENTRYPOINT"),
        "api_provider": provider,
        "api_region": region,
        "os": sys.platform,
        "permission_mode": permission_mode,
        "effort": effort,
    }


def merge_intervals(intervals):
    merged = []
    for start, end in sorted((s, e) for s, e in intervals if e > s):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def subtract_intervals(intervals, holes):
    holes = merge_intervals(holes)
    result = []
    for start, end in merge_intervals(intervals):
        cursor = start
        for hole_start, hole_end in holes:
            if hole_end <= cursor or hole_start >= end:
                continue
            if hole_start > cursor:
                result.append([cursor, hole_start])
            cursor = max(cursor, hole_end)
        if cursor < end:
            result.append([cursor, end])
    return result


def interval_seconds(intervals):
    """Segundos cubiertos por los intervalos, sin contar dos veces lo que se solapa."""
    return sum(end - start for start, end in merge_intervals(intervals))


def detect_phase(description):
    """Fase del pipeline según el primer verbo reconocible de la descripción del subagente."""
    text = (description or "").lower()
    found = [(match.start(), phase) for phase, keywords in PHASE_KEYWORDS
             for match in [re.search(r"\b(?:%s)" % "|".join(keywords), text)] if match]
    return min(found)[1] if found else "other"


def read_subagent_meta(transcript_file):
    try:
        with open(transcript_file[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
            meta = json.load(fh)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def new_scope():
    """Acumuladores de un tramo: toda la sesión, un segmento de la sesión principal o un subagente."""
    return {"usage": {}, "tools": {}, "skills": {}, "koda_ids": [],
            "code": {"files": {}, "files_created": set(), "edits_adjusted_by_developer": 0},
            "events": {"git_commits": 0, "api_errors": 0}}


def merge_scopes(scopes):
    merged = new_scope()
    for scope in scopes:
        merged["usage"].update(scope["usage"])
        for path, (added, removed) in scope["code"]["files"].items():
            acc = merged["code"]["files"].setdefault(path, [0, 0])
            acc[0] += added
            acc[1] += removed
        merged["code"]["files_created"] |= scope["code"]["files_created"]
        merged["code"]["edits_adjusted_by_developer"] += scope["code"]["edits_adjusted_by_developer"]
        for name, value in scope["events"].items():
            merged["events"][name] = merged["events"].get(name, 0) + value
    return merged


def compact_code_changes(changes):
    summary = code_changes_summary(changes)
    summary.pop("by_file_type")
    return summary


def token_totals(usages):
    totals = {}
    for model, usage in usages:
        acc = totals.setdefault(model, {"model": model, "input": 0, "output": 0,
                                        "cache_read": 0, "cache_creation": 0})
        acc["input"] += usage.get("input_tokens") or 0
        acc["output"] += usage.get("output_tokens") or 0
        acc["cache_read"] += usage.get("cache_read_input_tokens") or 0
        acc["cache_creation"] += usage.get("cache_creation_input_tokens") or 0
    return list(totals.values())


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else None


def xml_tag(text, name):
    match = re.search(r"<%s>([^<]*)</%s>" % (name, name), text)
    return match.group(1).strip() if match else None


def clean_status(value):
    value = str(value or "").strip().lower()
    return value if re.fullmatch(r"[a-z_]{1,24}", value) else "unknown"


def summarize(transcript_path, session_id, hook_cwd, env=None):
    env = os.environ if env is None else env
    # Acumuladores de toda la sesión. Cada segmento y cada subagente lleva además los suyos, para
    # repartir tokens y cambios de código por skill, por koda_id y por subagente.
    session = new_scope()
    session["events"]["context_compactions"] = 0
    timestamps = []
    koda_tools = {}
    pending_tool_uses = {}
    # Subagentes: quién los lanzó (id del tool_use Agent → segmento o subagente) y cómo terminaron.
    agent_launches = {}
    subagent_status = {}
    handbacks = set()
    interactions = {"prompts": 0, "questions_answered": 0, "questions_rejected": 0,
                    "interruptions": 0, "permission_mode_changes": 0}
    koda_id_sources = {}
    branches = set()
    # Un segmento es un tramo en que la IA trabaja en la sesión principal. Lo abre un prompt del
    # dev o un aviso del sistema (p. ej. terminó un subagente en segundo plano) y lo cierra
    # turn_duration. Las esperas de respuesta del dev dentro del segmento van en "waits".
    segments = []
    current_skill = NO_SKILL
    current_koda_id = None
    client_version = None
    entrypoint = permission_mode = effort = None
    cwd = hook_cwd
    partial = False

    def add_koda_id(koda_id, source):
        nonlocal current_koda_id
        koda_id_sources.setdefault(koda_id, set()).add(source)
        if source == "mcp":
            current_koda_id = koda_id
            if segments:
                segments[-1]["koda_id"] = koda_id

    def note_permission_mode(value):
        nonlocal permission_mode
        if isinstance(value, str) and value:
            if permission_mode and value != permission_mode:
                interactions["permission_mode_changes"] += 1
            permission_mode = value

    def open_segment(ts, human):
        segments.append({"start": ts, "end": ts, "open": True, "human": human, "waits": [],
                         "skill": current_skill, "koda_id": current_koda_id, "scope": new_scope()})

    def note_subagent_end(text, origin):
        # Del aviso solo se leen ids y estado; el resumen y el resultado del subagente no se envían.
        for block in TASK_NOTIFICATION_RE.findall(text):
            status = xml_tag(block, "status")
            for key in (xml_tag(block, "tool-use-id"), xml_tag(block, "task-id")):
                if key and status:
                    subagent_status[key] = clean_status(status)
        # Un subagente también puede terminar devolviendo el control (hand-back) sin aviso.
        if origin.get("kind") == "peer" and origin.get("handback") and origin.get("from"):
            handbacks.add(str(origin["from"]))

    def record_usage(entry, message, scopes):
        # Una misma respuesta se escribe en varias líneas con el mismo message.id:
        # se deduplica para no contar sus tokens más de una vez.
        usage = message.get("usage")
        model = message.get("model")
        if isinstance(usage, dict) and model and model != "<synthetic>":
            key = message.get("id") or entry.get("requestId") or entry.get("uuid")
            for scope in scopes:
                scope["usage"][key] = (model, usage)

    def record_tool_uses(content, ts_raw, scopes, main_session, launcher):
        """Cuenta tools, skills y koda_id de una respuesta; devuelve la skill cargada, si hubo.

        scopes son los acumuladores a los que suma (la sesión y el segmento o el subagente);
        launcher, quién lanza los subagentes de esta respuesta.
        """
        loaded_skill = None
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            name = item.get("name") or ""
            tool_input = item.get("input") if isinstance(item.get("input"), dict) else {}
            # Las tools MCP se agrupan por servidor; las de KODA se detallan aparte.
            group = "mcp__" + name.split("__")[1] if name.startswith("mcp__") else name
            for scope in scopes:
                scope["tools"].setdefault(group, {"name": group, "count": 0, "errors": 0})["count"] += 1
            is_commit = name == "Bash" and bool(GIT_COMMIT_RE.search(str(tool_input.get("command", ""))))
            pending_tool_uses[item.get("id")] = {"scopes": scopes, "tool": group, "is_commit": is_commit,
                                                 "asks_developer": main_session and name in DEVELOPER_INPUT_TOOLS,
                                                 "launches_agent": name in AGENT_TOOLS,
                                                 "ts": parse_ts(ts_raw)}
            if name in AGENT_TOOLS and launcher:
                agent_launches[item.get("id")] = launcher
            # Archivos de una iniciativa (p. ej. ksk-sdd/KODA-INI-…/prd.md): sale el id, nunca la ruta.
            for key in FILE_PATH_KEYS:
                for koda_id in KODA_ID_RE.findall(str(tool_input.get(key) or "")):
                    koda_id_sources.setdefault(koda_id, set()).add("file")
            if name == "Skill" and tool_input.get("skill"):
                loaded_skill = str(tool_input["skill"])
                for scope in scopes:
                    count_skill(scope["skills"], loaded_skill, ts_raw)
            elif name.startswith("mcp__") and "koda" in name.split("__")[1].lower():
                short = name.split("__", 2)[-1]
                koda_tools[short] = koda_tools.get(short, 0) + 1
                found = set()
                find_koda_ids(tool_input, found)
                for koda_id in sorted(found):
                    if main_session:
                        add_koda_id(koda_id, "mcp")
                    else:
                        # En un subagente, la iniciativa queda en su propio acumulador (el último).
                        koda_id_sources.setdefault(koda_id, set()).add("mcp")
                        if koda_id not in scopes[-1]["koda_ids"]:
                            scopes[-1]["koda_ids"].append(koda_id)
        return loaded_skill

    def record_tool_results(entry, items, ts):
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            pending = pending_tool_uses.pop(item.get("tool_use_id"), None) or {}
            scopes = pending.get("scopes") or [session]
            if pending.get("launches_agent"):
                result = entry.get("toolUseResult") if isinstance(entry.get("toolUseResult"), dict) else {}
                status = "failed" if item.get("is_error") else result.get("status")
                # Uno en segundo plano solo avisa que arrancó: su fin llega en un <task-notification>.
                if status and status != "async_launched":
                    subagent_status[item.get("tool_use_id")] = clean_status(status)
            if pending.get("asks_developer"):
                # Mientras la IA espera la respuesta del dev, el que trabaja es el dev.
                if segments and pending.get("ts") is not None and ts is not None:
                    segments[-1]["waits"].append([pending["ts"], ts])
                interactions["questions_rejected" if item.get("is_error") else "questions_answered"] += 1
            if item.get("is_error"):
                for scope in pending.get("scopes") or []:
                    scope["tools"][pending["tool"]]["errors"] += 1
                continue
            for scope in scopes:
                if pending.get("is_commit"):
                    scope["events"]["git_commits"] += 1
                add_code_change(entry.get("toolUseResult"), scope["code"])

    with open(transcript_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                partial = True
                continue
            if not isinstance(entry, dict):
                continue
            ts_raw = entry.get("timestamp")
            ts = parse_ts(ts_raw)
            if entry.get("type") == "permission-mode":
                note_permission_mode(entry.get("permissionMode"))
                continue
            if entry.get("type") == "system":
                # turn_duration marca el cierre del turno; su timestamp es el fin real. Su
                # durationMs no se usa: tras una interrupción mide desde el primer prompt.
                if entry.get("subtype") == "turn_duration" and segments:
                    if ts is not None:
                        timestamps.append(ts)
                        segments[-1]["end"] = max(segments[-1]["end"], ts)
                    segments[-1]["open"] = False
                elif entry.get("subtype") == "compact_boundary":
                    session["events"]["context_compactions"] += 1
                continue
            if entry.get("type") not in ("user", "assistant"):
                continue
            client_version = entry.get("version") or client_version
            entrypoint = entry.get("entrypoint") or entrypoint
            note_permission_mode(entry.get("permissionMode"))
            effort = entry.get("effort") or effort
            cwd = entry.get("cwd") or cwd
            if entry.get("gitBranch") and entry["gitBranch"] != "HEAD":
                branches.add(entry["gitBranch"])
            message = entry.get("message") or {}
            if not isinstance(message, dict):
                continue
            content = message.get("content")

            def extend_segment():
                if ts is not None:
                    timestamps.append(ts)
                    if segments:
                        segments[-1]["end"] = max(segments[-1]["end"], ts)

            if entry["type"] == "user":
                items = content if isinstance(content, list) else [content]
                if any(isinstance(i, dict) and i.get("type") == "tool_result" for i in items):
                    record_tool_results(entry, items, ts)
                    extend_segment()
                    continue
                text = " ".join(i if isinstance(i, str) else str(i.get("text", ""))
                                for i in items if isinstance(i, (str, dict)))
                # El dev cortó la respuesta: cierra el turno, no abre uno nuevo.
                if text.startswith(INTERRUPTED_MARK):
                    interactions["interruptions"] += 1
                    extend_segment()
                    continue
                # Comandos nativos (/model, /clear…) y su salida no son trabajo del agente.
                if LOCAL_COMMAND_MARK in text:
                    continue
                origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
                from_developer = origin.get("kind") == "human" or (
                    not origin.get("kind") and not entry.get("isMeta")
                    and not text.lstrip().startswith(SYSTEM_MESSAGE_MARKS))
                if not from_developer:
                    # Aviso del sistema o de un subagente: la IA sigue trabajando sin el dev.
                    note_subagent_end(text, origin)
                    if segments and segments[-1]["open"]:
                        extend_segment()
                    elif segments and ts is not None:
                        timestamps.append(ts)
                        open_segment(ts, human=False)
                    continue
                commands = COMMAND_NAME_RE.findall(text)
                invoked = [name for name in commands if is_installed_skill(name, cwd)]
                if commands and not invoked:
                    continue
                for name in invoked:
                    count_skill(session["skills"], name, ts_raw)
                    current_skill = name
                if ts is not None:
                    timestamps.append(ts)
                    interactions["prompts"] += 1
                    open_segment(ts, human=True)
                continue

            extend_segment()
            scopes = [session, segments[-1]["scope"]] if segments else [session]
            if entry.get("isApiErrorMessage"):
                for scope in scopes:
                    scope["events"]["api_errors"] += 1
            record_usage(entry, message, scopes)
            loaded_skill = record_tool_uses(content, ts_raw, scopes, main_session=True,
                                            launcher=("segment", segments[-1]) if segments else None)
            if loaded_skill:
                current_skill = loaded_skill
                if segments:
                    segments[-1]["skill"] = loaded_skill

    # Los subagentes escriben su propio transcript en <sesión>/subagents/. Sus tokens, tools,
    # skills y cambios de código se suman. Su tiempo cuenta como trabajo de la IA: los que corren
    # en segundo plano siguen trabajando cuando el turno principal ya terminó.
    subagent_dir = os.path.join(os.path.dirname(transcript_path), session_id, "subagents")
    subagent_files = sorted(glob.glob(os.path.join(subagent_dir, "*.jsonl")))
    subagents = []
    for path in subagent_files:
        subagent = {"agent_id": re.sub(r"^agent-", "", os.path.basename(path)[:-len(".jsonl")]),
                    "meta": read_subagent_meta(path), "scope": new_scope(), "attribution_skill": None}
        scopes = [session, subagent["scope"]]
        sub_timestamps = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        partial = True
                        continue
                    message = entry.get("message") if isinstance(entry, dict) else None
                    if not isinstance(message, dict):
                        continue
                    ts = parse_ts(entry.get("timestamp"))
                    if ts is not None:
                        sub_timestamps.append(ts)
                    if isinstance(entry.get("attributionSkill"), str) and not subagent["attribution_skill"]:
                        subagent["attribution_skill"] = entry["attributionSkill"]
                    content = message.get("content")
                    if entry.get("type") == "user" and isinstance(content, list):
                        record_tool_results(entry, content, ts)
                    elif entry.get("type") == "assistant":
                        if entry.get("isApiErrorMessage"):
                            for scope in scopes:
                                scope["events"]["api_errors"] += 1
                        record_usage(entry, message, scopes)
                        record_tool_uses(content, entry.get("timestamp"), scopes, main_session=False,
                                         launcher=("subagent", subagent))
        except OSError:
            partial = True
        subagent["start"] = min(sub_timestamps) if sub_timestamps else None
        subagent["end"] = max(sub_timestamps) if sub_timestamps else None
        subagents.append(subagent)

    repo = repo_info(cwd)
    if repo["branch"] and repo["branch"] != "HEAD":
        branches.add(repo["branch"])
    jira_keys = set()
    for branch in branches:
        for koda_id in KODA_ID_RE.findall(branch):
            add_koda_id(koda_id, "branch")
        jira_keys.update(JIRA_KEY_RE.findall(branch))

    # Los segmentos previos a la primera detección se asignan a la iniciativa de la sesión:
    # la primera vista por MCP o, si no hubo, la de la rama o la de los archivos que tocó la IA.
    default_koda_id = next((s["koda_id"] for s in segments if s["koda_id"]), None) or next(
        (k for source in ("branch", "file") for k in sorted(koda_id_sources)
         if source in koda_id_sources[k]), None)
    for segment in segments:
        if not segment["koda_id"]:
            segment["koda_id"] = default_koda_id
        else:
            break

    def find_parent(subagent, depth=0):
        """Segmento de la sesión principal que lanzó el subagente, directa o indirectamente."""
        kind, launcher = agent_launches.get(subagent["meta"].get("toolUseId"), (None, None))
        if kind == "segment":
            return launcher
        if kind == "subagent" and launcher is not subagent and depth < 5:
            return find_parent(launcher, depth + 1)
        # Transcripts viejos sin toolUseId: el último segmento que arrancó antes que el subagente.
        if subagent["start"] is None or not segments:
            return None
        return next((s for s in reversed(segments) if s["start"] <= subagent["start"]), segments[0])

    for subagent in subagents:
        parent = find_parent(subagent)
        parent_skill = parent["skill"] if parent and parent["skill"] != NO_SKILL else None
        subagent["skill"] = parent_skill or subagent["attribution_skill"] or NO_SKILL
        own_koda_ids = subagent["scope"]["koda_ids"]
        subagent["koda_id"] = own_koda_ids[0] if own_koda_ids else (parent["koda_id"] if parent else None)

    # Tiempo, tokens y cambios por tramos: cada segmento se asigna a su skill y koda_id; cada
    # subagente, a la skill del segmento que lo lanzó y a su iniciativa.
    buckets = {}

    def owner_buckets(owner):
        for key in (("skill", owner["skill"]), ("koda_id", owner["koda_id"] or NO_KODA_ID)):
            yield buckets.setdefault(key, {"ai": [], "developer": [], "turns": 0, "prompts": 0,
                                           "subagents": 0, "scopes": [], "skills": set()})

    def add_time(owner, kind, intervals):
        for acc in owner_buckets(owner):
            acc[kind].extend(intervals)

    def add_owner(owner, is_subagent):
        for acc in owner_buckets(owner):
            acc["scopes"].append(owner["scope"])
            acc["skills"].add(owner["skill"])
            if is_subagent:
                acc["subagents"] += 1
            else:
                acc["turns"] += 1
                acc["prompts"] += 1 if owner["human"] else 0

    ai_intervals = []
    developer_intervals = []
    for segment in segments:
        add_owner(segment, is_subagent=False)
        ai_part = subtract_intervals([[segment["start"], segment["end"]]], segment["waits"])
        ai_intervals.extend(ai_part)
        developer_intervals.extend(segment["waits"])
        add_time(segment, "ai", ai_part)
        add_time(segment, "developer", segment["waits"])
    for subagent in subagents:
        add_owner(subagent, is_subagent=True)
        if subagent["start"] is None or not segments:
            continue
        interval = [[subagent["start"], subagent["end"]]]
        ai_intervals.extend(interval)
        add_time(subagent, "ai", interval)
    # La pausa antes de cada prompt del dev (desde que la IA terminó todo, subagentes incluidos)
    # es tiempo del dev si es corta (leer, revisar, escribir); si supera el umbral, es inactividad.
    busy = merge_intervals(ai_intervals + developer_intervals)
    for index, segment in enumerate(segments):
        if index == 0 or not segment["human"]:
            continue
        previous_end = max((end for start, end in busy if start < segment["start"]), default=None)
        if previous_end is None or previous_end >= segment["start"]:
            continue
        if segment["start"] - previous_end < INACTIVITY_THRESHOLD_SECONDS:
            pause = [[previous_end, segment["start"]]]
            developer_intervals.extend(pause)
            add_time(segments[index - 1], "developer", pause)

    def time_rows(bucket_name):
        rows = []
        for (name, key), acc in buckets.items():
            if name != bucket_name:
                continue
            merged = merge_scopes(acc["scopes"])
            worked = merge_intervals(acc["ai"] + acc["developer"])
            row = {bucket_name: key, "turns": acc["turns"], "prompts": acc["prompts"],
                   "subagents": acc["subagents"],
                   "started_at": iso(worked[0][0] if worked else None),
                   "ended_at": iso(worked[-1][1] if worked else None),
                   "ai_working_seconds": round(interval_seconds(acc["ai"])),
                   "developer_working_seconds": round(interval_seconds(acc["developer"])),
                   "total_working_seconds": round(interval_seconds(worked)),
                   "tokens": token_totals(merged["usage"].values()),
                   "code_changes": compact_code_changes(merged["code"]),
                   "git_commits": merged["events"]["git_commits"]}
            if bucket_name == "koda_id":
                # Qué skills trabajaron la iniciativa en esta sesión.
                row["skills"] = sorted(acc["skills"] - {NO_SKILL})
            rows.append(row)
        return sorted(rows, key=lambda r: -r["total_working_seconds"])

    subagent_rows = []
    for subagent in sorted(subagents, key=lambda s: s["start"] if s["start"] is not None else float("inf")):
        meta = subagent["meta"]
        scope = subagent["scope"]
        seconds = subagent["end"] - subagent["start"] if subagent["start"] is not None else 0
        # Sin aviso de fin ni hand-back, el subagente seguía trabajando al momento del reporte.
        status = (subagent_status.get(meta.get("toolUseId")) or subagent_status.get(subagent["agent_id"])
                  or ("completed" if subagent["agent_id"] in handbacks else "running"))
        # De la descripción (texto libre) solo sale la fase; el texto no se envía.
        subagent_rows.append({"agent_type": meta.get("agentType") or "unknown",
                              "phase": detect_phase(meta.get("description")),
                              "background": meta.get("requestShape") == "background",
                              "status": status,
                              "launched_by_skill": subagent["skill"],
                              "koda_id": subagent["koda_id"],
                              "spawn_depth": meta.get("spawnDepth") if isinstance(meta.get("spawnDepth"), int) else None,
                              "started_at": iso(subagent["start"]),
                              "ended_at": iso(subagent["end"]),
                              "ai_working_seconds": round(seconds),
                              "requests": len(scope["usage"]),
                              "tokens": token_totals(scope["usage"].values()),
                              "tools": sorted(scope["tools"].values(), key=lambda r: -r["count"]),
                              "skills": sorted(scope["skills"]),
                              "code_changes": compact_code_changes(scope["code"]),
                              "git_commits": scope["events"]["git_commits"],
                              "api_errors": scope["events"]["api_errors"]})

    timestamps.sort()
    session_seconds = timestamps[-1] - timestamps[0] if timestamps else 0
    working_seconds = interval_seconds(ai_intervals + developer_intervals)

    return {
        "session_id": session_id,
        "source": "claude_code",
        "client_version": client_version,
        "reporter_version": REPORTER_VERSION,
        "reporter_status": "parse_partial" if partial else "ok",
        "koda_ids": sorted(koda_id_sources),
        "koda_id_sources": {k: sorted(v) for k, v in sorted(koda_id_sources.items())},
        "jira_keys": sorted(jira_keys),
        "repo": repo,
        "environment": environment_info(env, entrypoint, permission_mode, effort),
        "started_at": iso(timestamps[0] if timestamps else None),
        "last_activity_at": iso(timestamps[-1] if timestamps else None),
        "session_duration_seconds": round(session_seconds),
        "total_working_seconds": round(working_seconds),
        "ai_working_seconds": round(interval_seconds(ai_intervals)),
        "developer_working_seconds": round(interval_seconds(developer_intervals)),
        "inactive_seconds": round(max(0.0, session_seconds - working_seconds)),
        "turns": len(segments),
        "subagents": len(subagent_files),
        "developer_interactions": interactions,
        "working_time_by_skill": time_rows("skill"),
        "working_time_by_koda_id": time_rows("koda_id"),
        "working_time_by_subagent": subagent_rows,
        "code_changes": code_changes_summary(session["code"]),
        "events": session["events"],
        "tokens": token_totals(session["usage"].values()),
        "skills": list(session["skills"].values()),
        "tools": sorted(session["tools"].values(), key=lambda r: -r["count"]),
        "koda_tools": [{"name": n, "count": c} for n, c in sorted(koda_tools.items())],
    }


def load_config():
    """~/.koda/telemetry.json (lo escribe install_skill); si no está, las env vars de managed settings."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            config = json.load(fh)
        if isinstance(config, dict) and config.get("endpoint"):
            return config
    except (OSError, ValueError):
        pass
    if os.environ.get(ENDPOINT_ENV):
        return {"endpoint": os.environ[ENDPOINT_ENV], "api_key": os.environ.get(API_KEY_ENV)}
    return None


def is_koda_endpoint(endpoint):
    url = urllib.parse.urlsplit(endpoint or "")
    return url.scheme == "https" and url.path.rstrip("/").endswith(SESSION_USAGE_PATH)


def spool_write(payload):
    path = os.path.join(SPOOL_DIR, "%s.json" % payload["session_id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def send(payload, config):
    headers = {"Content-Type": "application/json",
               "User-Agent": "koda-session-report/%s" % REPORTER_VERSION}
    if config.get("api_key"):
        headers["Authorization"] = "Bearer %s" % config["api_key"]
    if config.get("user_email"):
        payload = dict(payload, user_email=config["user_email"])
    request = urllib.request.Request(config["endpoint"], data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS) as response:
        return 200 <= response.status < 300


def flush_spool(config, current_session):
    """Envía los resúmenes pendientes; el de la sesión actual va primero."""
    names = sorted(os.listdir(SPOOL_DIR), key=lambda n: n != "%s.json" % current_session)
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(SPOOL_DIR, name)
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            if payload.get("session_id") != current_session and payload.get("reporter_status") == "ok":
                payload["reporter_status"] = "spooled"
            if send(payload, config):
                os.remove(path)
        except Exception as exc:  # sin red, timeout, 4xx/5xx: queda en el spool
            log("send_failed session=%s error=%r" % (name[:-5], exc))
            return


def run(hook_input):
    session_id = hook_input.get("session_id")
    transcript_path = hook_input.get("transcript_path")
    log("hook event=%s session=%s cwd=%s script=%s dirs=%s" % (
        hook_input.get("hook_event_name"), session_id, os.getcwd(), os.path.abspath(__file__),
        {k: os.environ.get(k) for k in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")}))
    if not session_id or not transcript_path or not os.path.isfile(transcript_path):
        return
    os.makedirs(SPOOL_DIR, exist_ok=True)
    with open(LOCK_PATH, "w") as lock:
        # Si varias skills registraron el hook, sus ejecuciones van en fila y no se pisan el spool.
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        cwd = hook_input.get("cwd")
        payload = summarize(transcript_path, session_id, cwd)
        config = load_config()
        if config is None:
            payload["reporter_status"] = "no_key"
        spool_write(payload)
        if config is None:
            return
        # El email identifica al dev en KODA: se agrega al enviar y nunca queda en el spool.
        if is_koda_endpoint(config["endpoint"]):
            config["user_email"] = git_output(cwd, ["config", "user.email"])
        flush_spool(config, session_id)


def main():
    try:
        os.umask(0o077)  # spool, log y config solo legibles por el dev
        os.makedirs(KODA_HOME, mode=0o700, exist_ok=True)
        hook_input = json.loads(sys.stdin.read() or "{}")
        # Se desacopla del hook para que Claude Code no espere el envío.
        if hasattr(os, "fork") and not os.environ.get("KODA_TELEMETRY_FOREGROUND"):
            if os.fork() > 0:
                os._exit(0)
            os.setsid()
        started = time.time()
        run(hook_input if isinstance(hook_input, dict) else {})
        log("done in %.2fs" % (time.time() - started))
    except Exception:
        log("error %s" % traceback.format_exc().replace("\n", " | "))
    os._exit(0)


if __name__ == "__main__":
    main()
