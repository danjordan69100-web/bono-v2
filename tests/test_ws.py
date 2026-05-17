"""Quick smoke test for WebSocket /ws/telemetry and MCP server."""
import asyncio
import json
import sys

import httpx

try:
    import websockets
except ImportError:
    print("[install] pip install websockets")
    sys.exit(1)


async def test_ws():
    print("=== WebSocket /ws/telemetry ===")
    try:
        async with websockets.connect("ws://127.0.0.1:8766/ws/telemetry") as ws:
            for i in range(3):
                msg = await asyncio.wait_for(ws.recv(), timeout=3)
                data = json.loads(msg)
                snap = data.get("snapshot", {})
                print(f"  [{i+1}] t={data.get('t'):.3f} track={snap.get('track')} status={snap.get('status')} speed={snap.get('speed_kmh', 0):.0f}")
            print("  WS OK (3 messages received @ 10Hz)")
    except Exception as e:
        print(f"  WS FAIL : {e}")


async def test_mcp():
    print()
    print("=== MCP SSE server (port 8767) ===")
    try:
        async with httpx.AsyncClient(timeout=3) as cli:
            async with cli.stream("GET", "http://127.0.0.1:8767/sse") as resp:
                print(f"  HTTP {resp.status_code}")
                # Read just a few bytes to confirm SSE stream opens
                chunks = 0
                async for line in resp.aiter_lines():
                    if line:
                        print(f"  recv: {line[:100]}")
                        chunks += 1
                    if chunks >= 2:
                        break
                print("  MCP SSE OK")
    except Exception as e:
        print(f"  MCP : {type(e).__name__} - {str(e)[:100]}")
        print("  (may be normal if MCP requires handshake)")


async def main():
    await test_ws()
    await test_mcp()

asyncio.run(main())
