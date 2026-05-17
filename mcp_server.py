"""Bono v2 — MCP server (FastMCP).

Expose toutes les ressources et tools de Bono via le standard Model Context Protocol.
N'importe quel agent IA Claude (ex: moi via Claude Code) peut s'y brancher pour
introspection complète du système.

Resources exposées :
- bono://state          : état complet (snapshot + last_exchange + metrics)
- bono://snapshot       : SHM live
- bono://sessions       : liste sessions
- bono://exchanges      : conversation history
- bono://events         : auto-events history
- bono://logs/{service} : tail logs core/input/playback

Tools exposés (proxy vers HTTP core service) :
- ask     : injection question debug
- say     : force Bono speak
- stop    : barge-in playback
- query_telemetry, query_fuel_strategy, query_tire_state, query_opponents, query_weather, query_session_state
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import httpx
from fastmcp import FastMCP
from loguru import logger

from config import HTTP_PORT

BASE = f"http://127.0.0.1:{HTTP_PORT}"
mcp = FastMCP("Bono v2 Race Engineer", instructions="""
Bono is a voice race engineer for Assetto Corsa Competizione (ACC).
Use these tools to introspect Bono's live state, ask questions, or force speech.
Resources expose telemetry, conversation history, events, and logs.
""")


@mcp.resource("bono://state")
def res_state() -> dict:
    """Full Bono state : SHM snapshot + last exchange + metrics + memory stats."""
    return httpx.get(f"{BASE}/state", timeout=5).json()


@mcp.resource("bono://snapshot")
def res_snapshot() -> dict:
    """Live ACC SHM snapshot : track, car, speed, rpm, fuel, tyres, lap, gaps."""
    return httpx.get(f"{BASE}/snapshot", timeout=5).json()


@mcp.resource("bono://sessions")
def res_sessions() -> dict:
    """Recent sessions metadata."""
    return httpx.get(f"{BASE}/sessions?limit=20", timeout=5).json()


@mcp.resource("bono://exchanges")
def res_exchanges() -> dict:
    """Recent PTT exchanges (driver ↔ Bono)."""
    return httpx.get(f"{BASE}/exchanges?limit=20", timeout=5).json()


@mcp.resource("bono://events")
def res_events() -> dict:
    """Recent auto-events (lap_completed, fuel_critical, yellow_flag, ...)."""
    return httpx.get(f"{BASE}/events?limit=30", timeout=5).json()


@mcp.resource("bono://logs/core")
def res_logs_core() -> dict:
    return httpx.get(f"{BASE}/logs?service=core&lines=60", timeout=5).json()


@mcp.resource("bono://logs/input")
def res_logs_input() -> dict:
    return httpx.get(f"{BASE}/logs?service=input&lines=60", timeout=5).json()


@mcp.resource("bono://logs/playback")
def res_logs_playback() -> dict:
    return httpx.get(f"{BASE}/logs?service=playback&lines=60", timeout=5).json()


@mcp.resource("bono://costs")
def res_costs() -> dict:
    """LLM cost summary last 24h."""
    return httpx.get(f"{BASE}/costs?period_h=24", timeout=5).json()


@mcp.resource("bono://processes")
def res_processes() -> dict:
    return httpx.get(f"{BASE}/processes", timeout=5).json()


@mcp.tool()
def ask(text: str, speak: bool = True) -> dict:
    """Inject a question to Bono. If speak=True, Bono will also say the response."""
    return httpx.post(f"{BASE}/ask", json={"text": text, "speak": speak}, timeout=30).json()


@mcp.tool()
def say(text: str) -> dict:
    """Force Bono to speak arbitrary text (bypass LLM)."""
    return httpx.post(f"{BASE}/say", json={"text": text}, timeout=10).json()


@mcp.tool()
def stop_speak() -> dict:
    """Barge-in : stop current Bono playback."""
    return httpx.post(f"{BASE}/stop_speak", timeout=5).json()


@mcp.tool()
def get_telemetry() -> dict:
    """Get the current ACC SHM live snapshot (speed/rpm/fuel/tyres/laps/gaps/weather/flags)."""
    return httpx.get(f"{BASE}/snapshot", timeout=5).json()


if __name__ == "__main__":
    logger.add("logs/mcp_server.log", rotation="10 MB", retention=3)
    logger.info("=== Bono v2 MCP server start ===")
    # Default SSE transport on port 8767
    mcp.run(transport="sse", host="127.0.0.1", port=8767)
