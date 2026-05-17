"""Bono v2 — configuration centralisée."""
import os
import json
from pathlib import Path

ROOT = Path(__file__).parent
LOGS_DIR = ROOT / "logs"
DB_DIR = ROOT / "db"
TTS_CACHE_DIR = ROOT / "tts_cache"
PROMPTS_DIR = ROOT / "prompts"

# ZMQ endpoints (TCP localhost, low-overhead)
ZMQ_TELEMETRY_PUB = "tcp://127.0.0.1:5555"   # Core publishes SHM snapshots
ZMQ_AUDIO_INPUT   = "tcp://127.0.0.1:5556"   # Input → Core (PTT audio chunks)
ZMQ_PLAYBACK_REQ  = "tcp://127.0.0.1:5557"   # Core → Playback (Fish MP3 fetch + play)
ZMQ_EVENTS        = "tcp://127.0.0.1:5558"   # Core → Subscribers (event log)

# Audio settings
SAMPLE_RATE = 16000
CHANNELS = 1
PTT_BUTTON = 8                      # btn 8 = N snap-dome Fanatec
PTT_MAX_DURATION_S = 10.0           # hard limit
PTT_JOYSTICK_POLL_HZ = 50           # 20ms
SHM_POLL_HZ = 30                    # SHM watcher

# Audio devices
AUDIO_DEVICE_CASQUE = "Casque"      # MediaPlayer/miniaudio playback
AUDIO_MIC_DEVICE = None             # None = sounddevice default

# LLM
ANTHROPIC_MODELS = {
    "fast": "claude-haiku-4-5-20251001",
    "default": "claude-sonnet-4-6",
    "deep": "claude-opus-4-7",
}
LLM_MAX_TOKENS = 600
LLM_TEMPERATURE_RACE = 0.6
LLM_TEMPERATURE_PADDOCK = 0.85

# STT
DEEPGRAM_MODEL = "nova-3"
DEEPGRAM_LANGUAGE = "multi"

# Fish TTS
FISH_API = "https://api.fish.audio/v1/tts"
FISH_MODEL = "speech-1.6"

# FastAPI
HTTP_PORT = 8766                    # different from server.py legacy
MCP_PORT = 8767

# Load secrets from existing .env paths
def _load_env(*paths):
    for p in paths:
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env(
    str(ROOT / ".env"),
    r"C:\Users\danjo\Desktop\botv3\.env",
    r"C:\dev\bono_engineer\.env",
)

# Load Fish + Anthropic keys from CrewChief config (legacy compatibility)
CREWCHIEF_CFG = r"C:\Users\danjo\Documents\CrewChiefV4\claude_engineer_config.json"


def get_secrets() -> dict:
    """Returns dict of all available API keys (env + CrewChief config)."""
    s = {
        "deepgram": os.environ.get("DEEPGRAM_API_KEY", "").strip(),
        "anthropic": os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        "fish_key": "",
        "fish_voice_id": "",
        "elevenlabs_key": os.environ.get("ELEVENLABS_API_KEY", "").strip(),
        "elevenlabs_voice_id": "",
        "elevenlabs_model": "eleven_flash_v2_5",  # latence ~75ms (vs turbo 250ms)
        "gemini": os.environ.get("GEMINI_API_KEY", "").strip(),
        "cerebras": os.environ.get("CEREBRAS_API_KEY", "").strip(),
        "openrouter": os.environ.get("OPENROUTER_API_KEY", "").strip(),
    }
    if os.path.exists(CREWCHIEF_CFG):
        try:
            cfg = json.load(open(CREWCHIEF_CFG, encoding="utf-8"))
            s["fish_key"] = s["fish_key"] or cfg.get("FishAudioApiKey", "")
            s["fish_voice_id"] = s["fish_voice_id"] or cfg.get("FishAudioVoiceId", "")
            s["elevenlabs_key"] = s["elevenlabs_key"] or cfg.get("ElevenLabsApiKey", "")
            s["elevenlabs_voice_id"] = s["elevenlabs_voice_id"] or cfg.get("ElevenLabsVoiceId", "")
            # Allow override of default flash model if user prefers turbo
            cc_el_model = cfg.get("ElevenLabsModel", "")
            if cc_el_model and "flash" in cc_el_model.lower():
                s["elevenlabs_model"] = cc_el_model
            if not s["anthropic"]:
                s["anthropic"] = cfg.get("AnthropicApiKey", "")
            if not s["gemini"]:
                s["gemini"] = cfg.get("GeminiApiKey", "")
            if not s["cerebras"]:
                s["cerebras"] = cfg.get("CerebrasApiKey", "")
        except Exception:
            pass
    return s


# Mkdir
for d in (LOGS_DIR, DB_DIR, TTS_CACHE_DIR, PROMPTS_DIR):
    d.mkdir(exist_ok=True, parents=True)
