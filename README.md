# Bono v2 — Race Engineer Tout-Python pour ACC

Refonte complète 15/05/2026, remplace le fork C# CrewChiefV4 + Python server hybride v1.

## Architecture

```
launcher.py
├─ core_service.py       # Orchestrator (SHM 30Hz, LLM, tools, FastAPI dashboard, MCP-ready)
├─ playback_service.py   # Fish TTS + MediaPlayer Casque (barge-in)
└─ input_service.py      # Fanatec btn 8 polling + sounddevice mic capture
```

IPC : ZeroMQ tcp://127.0.0.1
- 5555 : telemetry PUB (CONFLATE)
- 5556 : audio PUSH (input → core)
- 5557 : playback PUSH (core → playback)
- 5558 : events PUB

## Launch

```
python launcher.py
```

Dashboard : http://127.0.0.1:8766

## Modules

| Fichier | Rôle |
|---------|------|
| `shm/acc_ctypes.py` | Lecteur SHM ACC direct ctypes (Static/Physics/Graphics structs, `_pack_=4`) |
| `tools/registry.py` | Décorateur `@bono_tool` + dispatch Anthropic native tool calling |
| `tools/racing_tools.py` | 7 tools : query_telemetry, query_fuel_strategy, query_tire_state, query_opponents, query_weather, query_session_state, bono_engineer_setup_update_acc |
| `prompts/persona_bono.py` | Persona Bono ~150 lignes ACC-only |
| `memory.py` | SQLite WAL : sessions / exchanges / driver / events / costs |
| `config.py` | Endpoints ZMQ, models, secrets loader |
| `zmq_bus.py` | Helpers PUB/SUB/PUSH/PULL + send_json/bytes |

## Stack

- Python 3.11
- `anthropic` SDK (tool calling natif)
- `deepgram-sdk` (Nova-3 multi-language FR+EN)
- Fish Audio HTTPS (voice clone "Bono")
- `sounddevice` (WASAPI shared, immune ACC focus)
- `pygame.joystick` (Fanatec wheel)
- `pyzmq` IPC
- `fastapi` + `loguru`
- `aiosqlite` + `sqlite3` WAL
- `ctypes` + `mmap` SHM ACC

## v1 → v2 migration

- ❌ Fork C# CrewChiefV4 supprimé du pipeline
- ❌ HTTP localhost server.py legacy supprimé du pipeline (peut tourner en parallèle si besoin)
- ❌ pyaccsharedmemory remplacé par ctypes mmap direct
- ❌ NAudio C# remplacé par sounddevice Python
- ❌ PowerShell MediaPlayer hack reste pour playback (à migrer miniaudio en P4 si besoin)
- ✅ Persona 150 lignes ACC-only portée
- ✅ Tool calling natif Anthropic (vs HTTP /coach legacy)
- ✅ SQLite memory persistante
- ✅ Dashboard live introspection

## Auto-events détectés

- `lap_completed` (tous les 3 laps, brief ack)
- `personal_best` (immediate)
- `fuel_critical` (< 3 laps restants en RACE)
- `yellow_flag` (global yellow)
- `tyre_cliff` (wear > 70%)

Cooldown par event type pour éviter spam.

## Endpoints HTTP

- `GET /` — Dashboard HTML temps réel
- `GET /state` — JSON complet (snapshot + last_exchange + metrics + memory_stats)
- `GET /snapshot` — SHM brut
- `GET /health` — uptime

## Logs

`C:\dev\bono_v2\logs\*.log` (rotation 10 MB, retention 3)

## v2.1 (15/05 nuit) — WebSocket + MCP + Telegram

- ✅ `WS /ws/telemetry` push 10Hz (snapshot + last_exchange + metrics) — clients FastAPI standard
- ✅ `mcp_server.py` FastMCP SSE port **8767** : 10 resources (`bono://state`, `bono://snapshot`, `bono://sessions`, `bono://exchanges`, `bono://events`, `bono://logs/{service}`, `bono://costs`, `bono://processes`) + 4 tools (`ask`, `say`, `stop_speak`, `get_telemetry`) — utilisable par moi (Claude Code) via MCP standard
- ✅ `telegram_bot.py` mobile remote control : commandes `/state /tyres /fuel /weather /session /say /ask /events /exchanges /processes /costs /stop` + push notifications events (lap PB, fuel low, etc.)
  - Requires `TELEGRAM_BONO_TOKEN` env var (créer un bot dédié via @BotFather pour éviter conflit avec d'autres bots existants)
  - Si pas défini, le service tourne en idle

## Limitations connues (TODO v2.2+)

- `bono_engineer_setup_update_acc` est un stub (ne write pas encore le .json ACC)
- Playback via PowerShell+MediaPlayer (à migrer miniaudio pour latence)
- Fallback Whisper local pas branché
- Pas de Pitwall Monte Carlo (legacy v1 dispo dans `C:\dev\bono_engineer\pitwall.py` à porter)
- Cost tracker pas branché aux appels Anthropic (memory.log_cost à appeler dans `handle_ptt`)
