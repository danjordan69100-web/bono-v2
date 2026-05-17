"""Bono v2 — Benchmark LLM : Anthropic Haiku 4.5 vs Gemini 2.5 Flash.

Mesure :
- Latence first token (TTFT)
- Latence total
- Tool calling success (parse args correct)
- Coût relatif

Usage : python bench_llm.py
"""
import sys
import os
import time
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import get_secrets, ANTHROPIC_MODELS

SECRETS = get_secrets()
ANTHROPIC_KEY = SECRETS.get("anthropic", "")
GEMINI_KEY = SECRETS.get("gemini", "")

# Mock snapshot (typique mid-race)
SNAPSHOT = {
    "session_type": "RACE",
    "lap_count": 12,
    "speed_kmh": 180,
    "rpm": 7200,
    "gear": 5,
    "fuel_liters": 28.5,
    "fuel_estimated_laps": 8,
    "tyre_press_fl": 26.8, "tyre_press_fr": 26.9, "tyre_press_rl": 27.1, "tyre_press_rr": 27.0,
    "tyre_temp_fl": 92, "tyre_temp_fr": 94, "tyre_temp_rl": 88, "tyre_temp_rr": 90,
    "tyre_wear_fl": 0.45, "tyre_wear_fr": 0.48, "tyre_wear_rl": 0.42, "tyre_wear_rr": 0.44,
    "position": 4,
    "gap_ahead_ms": 1200,
    "gap_behind_ms": 2800,
    "track": "spa",
    "car": "ferrari_488_gt3_evo",
}

TOOL_SCHEMAS = [
    {"name": "query_fuel_strategy", "description": "Returns fuel estimated laps + save needed", "input_schema": {"type": "object", "properties": {}}},
    {"name": "query_tire_state", "description": "Returns tyre pressures/temps/wear + Pirelli DHF target", "input_schema": {"type": "object", "properties": {}}},
    {"name": "query_opponents", "description": "Returns gap_ahead/behind in ms", "input_schema": {"type": "object", "properties": {}}},
]

SYSTEM_PROMPT = (
    "You are Bono, race engineer for Dan in ACC GT3 sim racing. "
    "Speak calmly like Bonnington/Hamilton. 1-2 phrases max. No markdown. "
    "Use tools to fetch real data, never fabricate. Reply in English unless driver speaks French."
)

PROMPTS = [
    "Bono, give me fuel?",
    "What's my gap to the car ahead?",
    "Tyres OK?",
]


def mock_tool_response(name):
    if name == "query_fuel_strategy":
        return {"liters_left": 28.5, "laps_left": 8, "save_pct": 0}
    if name == "query_tire_state":
        return {"press": [26.8, 26.9, 27.1, 27.0], "temps": [92, 94, 88, 90], "wear_max": 0.48, "target": "26.6-27.0 PSI"}
    if name == "query_opponents":
        return {"gap_ahead_ms": 1200, "gap_behind_ms": 2800, "pos": 4}
    return {}


def bench_anthropic(prompt: str):
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_KEY)
    t0 = time.time()
    # First call (may include tool use)
    resp = client.messages.create(
        model=ANTHROPIC_MODELS["fast"],
        max_tokens=300,
        system=SYSTEM_PROMPT,
        tools=TOOL_SCHEMAS,
        messages=[{"role": "user", "content": prompt}],
    )
    t_first = time.time() - t0
    tools_called = []
    final_text = ""
    # Simulate tool hop if needed
    if resp.stop_reason == "tool_use":
        tool_blocks = [b for b in resp.content if b.type == "tool_use"]
        for tb in tool_blocks:
            tools_called.append(tb.name)
        # Second call with tool results
        tool_results = [
            {"type": "tool_result", "tool_use_id": tb.id, "content": json.dumps(mock_tool_response(tb.name))}
            for tb in tool_blocks
        ]
        resp2 = client.messages.create(
            model=ANTHROPIC_MODELS["fast"],
            max_tokens=300,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": tool_results},
            ],
        )
        final_text = "".join(b.text for b in resp2.content if b.type == "text")
    else:
        final_text = "".join(b.text for b in resp.content if b.type == "text")
    t_total = time.time() - t0
    return {
        "provider": "anthropic",
        "model": ANTHROPIC_MODELS["fast"],
        "t_first_ms": int(t_first * 1000),
        "t_total_ms": int(t_total * 1000),
        "tools_called": tools_called,
        "text": final_text[:200],
    }


def bench_gemini(prompt: str):
    """Gemini 2.5 Flash via google-generativeai SDK."""
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_KEY)
    # Build function declarations from tool schemas
    func_decls = [
        {"name": t["name"], "description": t["description"], "parameters": {"type": "object", "properties": {}}}
        for t in TOOL_SCHEMAS
    ]
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=SYSTEM_PROMPT,
        tools=[{"function_declarations": func_decls}],
    )
    t0 = time.time()
    resp = model.generate_content(prompt, generation_config={"max_output_tokens": 300})
    t_first = time.time() - t0
    tools_called = []
    final_text = ""
    try:
        # Inspect function calls
        for part in resp.candidates[0].content.parts:
            if hasattr(part, "function_call") and part.function_call and part.function_call.name:
                tools_called.append(part.function_call.name)
        if tools_called:
            # Send back tool results, get final text
            tool_responses = []
            for tc in tools_called:
                tool_responses.append({"function_response": {"name": tc, "response": mock_tool_response(tc)}})
            # Build follow-up
            chat = model.start_chat(history=[
                {"role": "user", "parts": [prompt]},
                {"role": "model", "parts": resp.candidates[0].content.parts},
            ])
            resp2 = chat.send_message(tool_responses, generation_config={"max_output_tokens": 300})
            final_text = resp2.text if hasattr(resp2, "text") else ""
        else:
            final_text = resp.text if hasattr(resp, "text") else ""
    except Exception as e:
        final_text = f"(parse err: {e})"
    t_total = time.time() - t0
    return {
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "t_first_ms": int(t_first * 1000),
        "t_total_ms": int(t_total * 1000),
        "tools_called": tools_called,
        "text": final_text[:200],
    }


def main():
    print(f"Anthropic key: {'OK' if ANTHROPIC_KEY else 'MISS'} | Gemini key: {'OK' if GEMINI_KEY else 'MISS'}")
    print(f"\n=== Benchmark Anthropic Haiku 4.5 vs Gemini 2.5 Flash ===\n")
    results = []
    for prompt in PROMPTS:
        print(f"PROMPT: {prompt}")
        # Anthropic
        try:
            r_a = bench_anthropic(prompt)
            print(f"  [A] {r_a['model']}: tools={r_a['tools_called']} | first={r_a['t_first_ms']}ms total={r_a['t_total_ms']}ms")
            print(f"      \"{r_a['text']}\"")
            results.append(r_a)
        except Exception as e:
            print(f"  [A] FAIL: {e}")
        # Gemini
        try:
            r_g = bench_gemini(prompt)
            print(f"  [G] {r_g['model']}: tools={r_g['tools_called']} | first={r_g['t_first_ms']}ms total={r_g['t_total_ms']}ms")
            print(f"      \"{r_g['text']}\"")
            results.append(r_g)
        except Exception as e:
            print(f"  [G] FAIL: {e}")
        print()
    # Summary
    print("=== SUMMARY ===")
    by_provider = {}
    for r in results:
        by_provider.setdefault(r["provider"], []).append(r)
    for prov, rs in by_provider.items():
        avg_total = sum(r["t_total_ms"] for r in rs) / len(rs)
        tools_ok = sum(1 for r in rs if r["tools_called"])
        print(f"{prov}: avg_total={avg_total:.0f}ms | tool_use {tools_ok}/{len(rs)}")


if __name__ == "__main__":
    main()
