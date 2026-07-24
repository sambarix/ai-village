"""
Generisches Agenten-Skript fuer den AI-Village-Community-Stack.

Ersetzt die separaten Skripte pro Modell (qwen_agent.py, ministral_agent.py,
rnj_agent.py etc.) durch ein einziges, das gegen jeden OpenAI-kompatiblen
lokalen Server (LM Studio) funktioniert - unabhaengig davon, welches Modell
gerade geladen ist.

Voraussetzungen auf diesem Rechner:
- LM Studio installiert, ein Modell geladen, lokaler Server gestartet
  (Developer-Tab -> Start Server, Standard-Port 1234)
- Python-Paket "requests" installiert (pip install requests)

Konfiguration: HUB_URL unten an die eigene Hub-Adresse anpassen.
"""

import difflib
import json
import re
import socket
import time
from pathlib import Path

import requests

# --- Konfiguration: pro Installation anpassen ---
HUB_URL = "http://192.168.188.31:9656"
LMSTUDIO_URL = "http://localhost:1234/v1"

POLL_INTERVAL_SECONDS = 30
MAX_CONTEXT_MESSAGES = 12  # nur die letzten N Chat-Nachrichten in den Prompt
REQUEST_TIMEOUT_SECONDS = 180
DUPLICATE_SIMILARITY_THRESHOLD = 0.8  # 0-1, hoeher = strenger beim Erkennen von Wiederholungen
MAX_TOOL_ROUNDS = 5  # Obergrenze gegen endlose Werkzeug-Aufruf-Schleifen
TOOL_RESULT_MAX_CHARS = 4000  # Datei-Inhalte im Kontext begrenzen

# --- Agenten-Memory (Abschnitt 3.4 im Konzept) ---
MEMORY_DIR = Path(__file__).parent / "memory"
MEMORY_DIR.mkdir(exist_ok=True)
ACTIVE_MEMORY_FILE = MEMORY_DIR / "active.md"   # aktueller Fokus, wird ausgeduennt
CORE_MEMORY_FILE = MEMORY_DIR / "core.md"       # dauerhafte Fakten/Entscheidungen
LOG_MEMORY_FILE = MEMORY_DIR / "log.md"         # append-only Verlauf
ACTIVE_MEMORY_MAX_CHARS = 1500

STATE_FILE = Path(__file__).parent / "agent_state.json"

# Verschiedene Modelle markieren ihre Denkspur unterschiedlich - alle bisher
# beobachteten Varianten werden hier gefiltert (Sicherheitsnetz zusaetzlich
# zu LM Studios eigener reasoning_content-Trennung, siehe call_model_once()).
THINK_BLOCK_RE = re.compile(
    r"(<think>.*?</think>|\[THINK\].*?\[/THINK\]|THOUGHT:.*?RESPONSE:)\s*",
    re.DOTALL | re.IGNORECASE,
)
# Zeilen der Form "MERKEN: ..." werden als dauerhafter Fakt ins Core-Memory
# uebernommen und aus der sichtbaren Chat-Antwort entfernt.
CORE_MARKER_RE = re.compile(r"^MERKEN:\s*(.+)$", re.MULTILINE)
# Manche Modelle (z. B. rnj-1-instruct) geben Werkzeug-Aufrufe als rohen
# Text im <tool_call>{...}</tool_call>-Format zurueck, statt sie ins
# strukturierte tool_calls-Feld der API zu legen - offenbar, weil LM Studio
# dieses Format fuer das jeweilige Modell (noch) nicht automatisch erkennt.
TEXT_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


# --- Hilfsfunktionen: Memory ---

def read_memory_file(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def append_memory_file(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def write_memory_file(path: Path, text: str) -> None:
    path.write_text(text.strip() + "\n", encoding="utf-8")


def extract_core_facts(text: str) -> tuple[str, list[str]]:
    """Trennt MERKEN:-Zeilen vom sichtbaren Antworttext ab."""
    facts = CORE_MARKER_RE.findall(text)
    cleaned = CORE_MARKER_RE.sub("", text).strip()
    return cleaned, facts


def is_duplicate(candidate: str, recent_texts: list[str]) -> bool:
    """Grobe Aehnlichkeitspruefung gegen kuerzlich Gesagtes - verhindert
    inhaltsleere Wiederholungsschleifen (siehe Konzept Abschnitt 3.4/7)."""
    for prior in recent_texts:
        if not prior.strip():
            continue
        ratio = difflib.SequenceMatcher(None, candidate.lower(), prior.lower()).ratio()
        if ratio >= DUPLICATE_SIMILARITY_THRESHOLD:
            return True
    return False


def parse_text_tool_calls(text: str) -> list[dict]:
    """Auffangnetz fuer Modelle, die Werkzeug-Aufrufe als rohen Text im
    <tool_call>{...}</tool_call>-Format liefern, statt sie ins
    strukturierte tool_calls-Feld der API zu legen. Gibt eine Liste im
    selben Format wie echte tool_calls-Eintraege zurueck."""
    calls = []
    for match in TEXT_TOOL_CALL_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        calls.append(
            {
                "id": f"textcall-{len(calls)}",
                "function": {
                    "name": data.get("name", ""),
                    "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False),
                },
            }
        )
    return calls


# --- Hilfsfunktionen: LM Studio ---

def wait_for_lmstudio() -> None:
    print("Warte auf LM Studio ...")
    while True:
        try:
            requests.get(f"{LMSTUDIO_URL}/models", timeout=5).raise_for_status()
            print("LM Studio erreichbar.")
            return
        except requests.exceptions.RequestException:
            time.sleep(3)


def get_loaded_model() -> str:
    r = requests.get(f"{LMSTUDIO_URL}/models", timeout=15)
    r.raise_for_status()
    models = r.json().get("data", [])
    if not models:
        raise RuntimeError(
            "LM Studio meldet kein geladenes Modell. Modell laden und "
            "lokalen Server starten (Developer-Tab -> Start Server)."
        )
    return models[0]["id"]


def clean_response(text: str) -> str:
    return THINK_BLOCK_RE.sub("", text).strip()


# --- Werkzeuge (Konzept Abschnitt 3.2/4) ---
# Standard-OpenAI-Function-Calling-Format, damit es gegen jeden
# OpenAI-kompatiblen Server portabel bleibt - keine LM-Studio-spezifische
# MCP-Anbindung noetig. Die eigentliche Ausfuehrung passiert zentral im Hub
# (siehe main.py /tools/*), dieses Skript ruft nur die passenden
# HTTP-Endpunkte auf.
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "list_allowed_directories",
            "description": "Zeigt, in welchem Wurzelverzeichnis du lesen und schreiben darfst.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "Listet Dateien und Unterordner an einem Pfad im gemeinsamen Arbeitsbereich auf.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relativer Pfad, leer = Wurzel des Arbeitsbereichs",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Liest den Inhalt einer Textdatei im gemeinsamen Arbeitsbereich.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relativer Pfad zur Datei"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Schreibt Inhalt in eine Datei im gemeinsamen Arbeitsbereich "
                "(legt Unterordner bei Bedarf automatisch mit an)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relativer Pfad zur Datei"},
                    "content": {"type": "string", "description": "Zu schreibender Inhalt"},
                },
                "required": ["path", "content"],
            },
        },
    },
]


def execute_tool_call(name: str, arguments: dict) -> dict:
    """Fuehrt einen vom Modell angeforderten Werkzeug-Aufruf gegen den Hub
    aus. Der Hub selbst prueft Pfad-Sicherheit - hier wird nur weitergereicht."""
    try:
        if name == "list_allowed_directories":
            r = requests.get(f"{HUB_URL}/tools/list_allowed_directories", timeout=15)
        elif name == "list_directory":
            r = requests.get(
                f"{HUB_URL}/tools/list_directory",
                params={"path": arguments.get("path", "")},
                timeout=15,
            )
        elif name == "read_file":
            r = requests.get(
                f"{HUB_URL}/tools/read_file",
                params={"path": arguments.get("path", "")},
                timeout=15,
            )
        elif name == "write_file":
            r = requests.post(
                f"{HUB_URL}/tools/write_file",
                json={"path": arguments.get("path", ""), "content": arguments.get("content", "")},
                timeout=15,
            )
        else:
            return {"error": f"Unbekanntes Werkzeug: {name}"}
    except requests.exceptions.RequestException as exc:
        return {"error": f"Verbindung zum Hub fehlgeschlagen: {exc}"}

    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text[:200])
        except ValueError:
            detail = r.text[:200]
        return {"error": detail}

    result = r.json()
    if "content" in result and len(result["content"]) > TOOL_RESULT_MAX_CHARS:
        result["content"] = result["content"][:TOOL_RESULT_MAX_CHARS] + "\n[...gekuerzt...]"
    return result


def _post_chat_completion(payload_variants: list[dict]):
    """Versucht eine Reihe von Anfrage-Varianten der Reihe nach und faellt
    bei Timeout auf die naechste, einfachere Variante zurueck. Manche
    Modelle/Backends haengen sich an bestimmten Zusatzfeldern (Denkspur-
    Unterdrueckung, Werkzeug-Schema) auf, andere nicht - deshalb probieren
    statt raten."""
    last_exc = None
    for i, payload in enumerate(payload_variants):
        try:
            return requests.post(
                f"{LMSTUDIO_URL}/chat/completions",
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.exceptions.Timeout as exc:
            last_exc = exc
            if i < len(payload_variants) - 1:
                print("[Hinweis] Anfrage-Variante in Timeout gelaufen, versuche einfachere Variante.")
    raise last_exc


def call_model_once(model: str, messages: list[dict], tool_choice: str = "auto") -> dict:
    """Ein einzelner Roh-Aufruf an LM Studio, gibt die komplette
    message-Struktur zurueck (inkl. moeglicher tool_calls).
    tool_choice="none" erzwingt eine reine Text-Antwort ohne weitere
    Werkzeug-Aufrufe."""
    base = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 500,
    }
    with_tools = {**base, "tools": TOOLS_SCHEMA, "tool_choice": tool_choice}
    # Reihenfolge: volle Ausstattung -> ohne Denkspur-Unterdrueckung ->
    # ganz ohne Werkzeuge (letzter Ausweg, falls das Modell/Backend damit
    # spuerbar langsamer wird oder haengt).
    variants = [
        {**with_tools, "chat_template_kwargs": {"enable_thinking": False}},
        with_tools,
        base,
    ]
    r = _post_chat_completion(variants)
    if r.status_code >= 400:
        raise RuntimeError(f"LM Studio Fehler {r.status_code}: {r.text[:500]}")
    return r.json()["choices"][0]["message"]


def run_conversation(model: str, system_frame: str, user_prompt: str) -> str:
    """Fuehrt bei Bedarf mehrere Werkzeug-Aufrufe nacheinander aus (bis
    MAX_TOOL_ROUNDS), bevor die eigentliche Chat-Antwort zurueckgegeben wird."""
    messages = [
        {"role": "system", "content": system_frame},
        {"role": "user", "content": user_prompt},
    ]
    for _ in range(MAX_TOOL_ROUNDS):
        message = call_model_once(model, messages)
        tool_calls = message.get("tool_calls")
        assistant_message = message

        if not tool_calls:
            content = message.get("content") or ""
            if not content.strip():
                content = message.get("reasoning_content") or ""
            # Auffangnetz: manche Modelle liefern Werkzeug-Aufrufe als
            # rohen Text statt strukturiert - hier nachtraeglich erkennen.
            fallback_calls = parse_text_tool_calls(content)
            if not fallback_calls:
                return clean_response(content)
            tool_calls = fallback_calls
            assistant_message = {"role": "assistant", "content": content, "tool_calls": fallback_calls}

        messages.append(assistant_message)
        for call in tool_calls:
            fn_name = call.get("function", {}).get("name", "")
            raw_args = call.get("function", {}).get("arguments") or "{}"
            try:
                fn_args = json.loads(raw_args)
            except json.JSONDecodeError:
                fn_args = {}
            result = execute_tool_call(fn_name, fn_args)
            print(f"[Werkzeug] {fn_name}({fn_args}) -> {str(result)[:120]}")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", fn_name),
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    print(f"[Hinweis] {MAX_TOOL_ROUNDS} Werkzeug-Runden erreicht, erzwinge finale Antwort ohne weitere Werkzeuge.")
    messages.append(
        {
            "role": "user",
            "content": (
                "Du hast das Limit an Werkzeug-Aufrufen fuer diese Runde erreicht. "
                "Antworte jetzt direkt im Chat, ohne weitere Werkzeug-Aufrufe - "
                "fasse kurz zusammen, was du erreicht hast oder woran es gerade hakt."
            ),
        }
    )
    final_message = call_model_once(model, messages, tool_choice="none")
    content = final_message.get("content") or final_message.get("reasoning_content") or ""
    return clean_response(content)


def maybe_compress_active_memory(model: str, identity: str) -> None:
    """Wird der 'aktueller Fokus'-Notizzettel zu lang, bittet der Agent
    sich selbst, ihn zusammenzufassen - core.md bleibt davon unberuehrt."""
    active = read_memory_file(ACTIVE_MEMORY_FILE)
    if len(active) <= ACTIVE_MEMORY_MAX_CHARS:
        return
    prompt = (
        f"Das ist dein bisheriger 'aktueller Fokus'-Notizzettel als {identity}:\n\n"
        f"{active}\n\nFasse ihn deutlich kuerzer zusammen (maximal "
        f"{ACTIVE_MEMORY_MAX_CHARS // 2} Zeichen), behalte nur, was fuer die "
        "naechsten Schritte noch relevant ist. Gib NUR den gekuerzten Text zurueck."
    )
    try:
        r = requests.post(
            f"{LMSTUDIO_URL}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 400,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if r.status_code < 400:
            summary = clean_response(r.json()["choices"][0]["message"].get("content") or "")
            if summary:
                write_memory_file(ACTIVE_MEMORY_FILE, summary)
                print("[Memory] Aktueller Fokus zusammengefasst.")
    except requests.exceptions.RequestException as exc:
        print(f"[Memory] Zusammenfassen fehlgeschlagen: {exc}")


# --- Hilfsfunktionen: Hub ---

def is_paused() -> bool:
    """Nur relevant im Einzelbetrieb (siehe Konzept 3.1) - im
    Community-Modell wird dieser Schalter nie bedient und bleibt einfach
    immer False."""
    r = requests.get(f"{HUB_URL}/paused", timeout=15)
    r.raise_for_status()
    return r.json().get("paused", False)


def get_latest_message_id() -> int:
    """Ermittelt die aktuell letzte Nachricht-ID im Agenten-Kanal, ohne sie zu verarbeiten."""
    messages = fetch_new_messages(0)
    return messages[-1]["id"] if messages else 0


def load_last_id() -> int:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text()).get("last_id", 0)
        except (json.JSONDecodeError, OSError):
            pass
    # Keine (verwertbare) Zustandsdatei vorhanden: beim allerersten Start
    # nicht die komplette bisherige Chat-Historie nachtraeglich beantworten,
    # sondern still auf den aktuellen Stand katapultieren.
    latest = get_latest_message_id()
    save_last_id(latest)
    print(f"Kein vorheriger Stand gefunden - starte ab aktueller Nachricht-ID {latest}.")
    return latest


def save_last_id(last_id: int) -> None:
    STATE_FILE.write_text(json.dumps({"last_id": last_id}))


def fetch_new_messages(since: int) -> list[dict]:
    r = requests.get(
        f"{HUB_URL}/messages",
        params={"since": since, "channel": "agenten"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["messages"]


def post_message(sender: str, content: str) -> None:
    r = requests.post(
        f"{HUB_URL}/messages",
        json={"sender": sender, "content": content, "channel": "agenten"},
        timeout=15,
    )
    r.raise_for_status()


# --- Prompt-Aufbau ---

def build_system_frame(identity: str, active_memory: str, core_memory: str) -> str:
    parts = [
        f"Du bist '{identity}', ein Teilnehmer in einem kleinen, privaten "
        "Multi-Agenten-Chat (AI-Village-Community-Stack). Der Nutzer-Prompt "
        "jeder Anfrage enthaelt den ECHTEN, AKTUELLEN Chat-Verlauf - das ist "
        "kein Beispiel. Antworte kurz, auf Deutsch, konversationell auf die "
        "letzte Nachricht. Wiederhole nicht, was du oder andere bereits "
        "gesagt haben. Gib NUR deine Chat-Antwort zurueck, ohne "
        "Anfuehrungszeichen, ohne Praefix.",
        "Falls du dir etwas dauerhaft merken willst (eine wichtige "
        "Entscheidung, einen Fakt), schreib zusaetzlich eine eigene Zeile "
        "'MERKEN: <Inhalt>' - diese erscheint nicht im Chat, sondern wird "
        "in deinem dauerhaften Gedaechtnis gespeichert.",
        "Dir stehen Werkzeuge zur Verfuegung, um im gemeinsamen "
        "Arbeitsbereich der Village Dateien zu lesen, zu schreiben und "
        "Ordner aufzulisten. Nutze sie, wenn ihr gemeinsam etwas "
        "Konkretes entwickelt, statt Inhalte nur im Chat zu beschreiben.",
    ]
    if core_memory:
        parts.append(f"Dein dauerhaftes Gedaechtnis (Fakten/Entscheidungen):\n{core_memory}")
    if active_memory:
        parts.append(f"Dein aktueller Fokus:\n{active_memory}")
    return "\n\n".join(parts)


def build_user_prompt(messages: list[dict]) -> str:
    verlauf = "\n".join(
        f"{m['sender']}: {m['content']}" for m in messages[-MAX_CONTEXT_MESSAGES:]
    )
    return f"Chat-Verlauf (neueste zuletzt):\n{verlauf}\n\nDeine Antwort:"


# --- Hauptschleife ---

def main() -> None:
    wait_for_lmstudio()
    last_id = load_last_id()
    own_hostname = socket.gethostname()
    print(f"Agent gestartet auf {own_hostname}. Letzte gesehene Nachricht-ID: {last_id}")

    while True:
        try:
            if is_paused():
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            model = get_loaded_model()
            identity = f"{own_hostname}/{model}"

            messages = fetch_new_messages(last_id)
            if messages:
                last_id = messages[-1]["id"]
                save_last_id(last_id)

                # Eigene fruehere Nachrichten rausfiltern - ueber den
                # Rechnernamen, nicht die volle Kennung, damit ein
                # Modellwechsel in LM Studio nicht dazu fuehrt, dass der
                # Agent seine eigenen alten Beitraege als fremd behandelt.
                relevante = [
                    m for m in messages if not m["sender"].startswith(own_hostname + "/")
                ]

                if relevante:
                    active_memory = read_memory_file(ACTIVE_MEMORY_FILE)
                    core_memory = read_memory_file(CORE_MEMORY_FILE)
                    system_frame = build_system_frame(identity, active_memory, core_memory)
                    user_prompt = build_user_prompt(relevante)

                    raw_response = run_conversation(model, system_frame, user_prompt)
                    response, core_facts = extract_core_facts(raw_response)

                    for fact in core_facts:
                        if fact.strip():
                            append_memory_file(CORE_MEMORY_FILE, f"- {fact.strip()}")
                            print(f"[Memory] Neuer Fakt gemerkt: {fact.strip()[:80]}")

                    if response:
                        recent_texts = [m["content"] for m in relevante[-5:]]
                        recent_texts += read_memory_file(LOG_MEMORY_FILE).splitlines()[-10:]

                        if is_duplicate(response, recent_texts):
                            print(f"[{identity}] Antwort unterdrueckt (zu aehnlich wie zuvor).")
                        else:
                            post_message(identity, response)
                            print(f"[{identity}] {response[:100]}")
                            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                            append_memory_file(LOG_MEMORY_FILE, f"{timestamp} - {response[:200]}")
                            append_memory_file(ACTIVE_MEMORY_FILE, f"- {response[:200]}")

                    maybe_compress_active_memory(model, identity)

        except Exception as exc:  # bewusst breit: Loop soll nicht abbrechen
            print(f"Fehler im Durchlauf: {exc}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
