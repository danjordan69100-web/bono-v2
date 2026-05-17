"""Bono v2 — Telegram bot remote control mobile.

Commandes :
  /state        — Snapshot live (track, car, speed, fuel, lap, position)
  /tyres        — Tyre pressures + temps + wear
  /fuel         — Fuel laps remaining
  /weather      — Weather + rain forecast
  /session      — Session state (Practice/Race/etc)
  /say <text>   — Force Bono speak in casque
  /ask <text>   — Ask Bono a question (full LLM + tools), reply text + speaks
  /events       — Last 10 auto-events
  /exchanges    — Last 5 PTT exchanges
  /processes    — Bono process status (PID/RAM/uptime)
  /costs        — LLM cost last 24h
  /stop         — Barge-in stop playback
  /help         — This message

Push notifications :
  Si TELEGRAM_PUSH_EVENTS=1 dans .env, le bot push les events auto (PB, fuel_low, yellow flag) vers TELEGRAM_BONO_CHAT_ID.
"""
import os
import sys
import asyncio
import time
import json
from pathlib import Path

# Set HOME env var if missing (python-telegram-bot needs it)
os.environ.setdefault("HOME", os.path.expanduser("~"))

sys.path.insert(0, str(Path(__file__).parent))
from config import HTTP_PORT, ZMQ_EVENTS
from zmq_bus import make_sub, recv_json
from loguru import logger

import httpx

# python-telegram-bot v21+
try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
except Exception as e:
    print(f"[FATAL] python-telegram-bot missing : {e}")
    print("Run : pip install python-telegram-bot")
    sys.exit(1)


# NOTE 15/05 : TELEGRAM_BONO_TOKEN DÉDIÉ obligatoire (créer via BotFather, ne pas réutiliser TELEGRAM_BOT_TOKEN
# partagé avec bot Polymarket — sinon conflit polling).
TOKEN = os.environ.get("TELEGRAM_BONO_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_BONO_CHAT_ID", "").strip() or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
PUSH_EVENTS = os.environ.get("TELEGRAM_PUSH_EVENTS", "0") == "1"
BASE = f"http://127.0.0.1:{HTTP_PORT}"


def _is_authorized(update: Update) -> bool:
    if not CHAT_ID: return True  # no restriction set
    return str(update.effective_chat.id) == CHAT_ID


async def cmd_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    msg = (
        "Bono v2 remote control\n\n"
        "/state — snapshot live\n"
        "/tyres — pressions + temps\n"
        "/fuel — laps restants\n"
        "/weather — météo + forecast\n"
        "/session — état session ACC\n"
        "/say <text> — force speak\n"
        "/ask <text> — question (LLM+tools+speak)\n"
        "/events — derniers events\n"
        "/exchanges — derniers PTT\n"
        "/processes — PID/RAM\n"
        "/costs — coût LLM 24h\n"
        "/stop — barge-in\n"
    )
    await u.message.reply_text(msg)


async def cmd_state(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    try:
        s = httpx.get(f"{BASE}/snapshot", timeout=5).json()
        if not s.get("shm_ok"):
            await u.message.reply_text(f"SHM: {s.get('error', 'no data')}"); return
        txt = (f"🏁 {s.get('track')} / {s.get('car')}\n"
               f"Session: {s.get('session')} ({s.get('status')})\n"
               f"Lap {s.get('completed_laps')} / Pos P{s.get('position')}\n"
               f"Speed {s.get('speed_kmh', 0):.0f} kmh | RPM {s.get('rpm')} | Gear {s.get('gear')}\n"
               f"Fuel {s.get('fuel_l', 0):.1f} L ({s.get('fuel_estimated_laps', 0):.1f} laps)\n"
               f"BB {s.get('brake_bias', 0):.1f}% | TC {s.get('tc_level')} ABS {s.get('abs_level')}")
        await u.message.reply_text(txt)
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


async def cmd_tyres(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    try:
        s = httpx.get(f"{BASE}/snapshot", timeout=5).json()
        p = [s.get(f"tyre_press_{x}", 0) for x in ("fl","fr","rl","rr")]
        t = [s.get(f"tyre_temp_{x}", 0) for x in ("fl","fr","rl","rr")]
        w = [s.get(f"tyre_wear_{x}", 0) for x in ("fl","fr","rl","rr")]
        txt = (f"🛞 {s.get('tyre_compound')}\n"
               f"FL: {p[0]:.1f}psi / {t[0]:.0f}°C / {w[0]*100:.0f}% wear\n"
               f"FR: {p[1]:.1f}psi / {t[1]:.0f}°C / {w[1]*100:.0f}% wear\n"
               f"RL: {p[2]:.1f}psi / {t[2]:.0f}°C / {w[2]*100:.0f}% wear\n"
               f"RR: {p[3]:.1f}psi / {t[3]:.0f}°C / {w[3]*100:.0f}% wear")
        await u.message.reply_text(txt)
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


async def cmd_fuel(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    try:
        s = httpx.get(f"{BASE}/snapshot", timeout=5).json()
        await u.message.reply_text(f"⛽ {s.get('fuel_l', 0):.2f} L / lap: {s.get('fuel_per_lap', 0):.2f} L / est: {s.get('fuel_estimated_laps', 0):.1f} laps")
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


async def cmd_weather(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    try:
        s = httpx.get(f"{BASE}/snapshot", timeout=5).json()
        await u.message.reply_text(f"🌦 Air {s.get('air_temp', 0):.1f}°C / Track {s.get('road_temp', 0):.1f}°C\nRain now={s.get('rain_intensity')} 10min={s.get('rain_intensity_in_10min')} 30min={s.get('rain_intensity_in_30min')}\nWind {s.get('wind_speed', 0):.1f} kmh")
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


async def cmd_session(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    try:
        s = httpx.get(f"{BASE}/snapshot", timeout=5).json()
        await u.message.reply_text(f"Session: {s.get('session')} / status {s.get('status')}\nLap {s.get('completed_laps')} / Pos P{s.get('position')}\nValid lap: {s.get('valid_lap')}\nFlags: yellow={s.get('global_yellow')} red={s.get('global_red')} chequered={s.get('global_chequered')}")
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


async def cmd_say(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    text = " ".join(c.args).strip()
    if not text:
        await u.message.reply_text("Usage: /say <texte>"); return
    try:
        r = httpx.post(f"{BASE}/say", json={"text": text}, timeout=5).json()
        await u.message.reply_text(f"Queued: {r.get('text', '')[:80]}")
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


async def cmd_ask(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    text = " ".join(c.args).strip()
    if not text:
        await u.message.reply_text("Usage: /ask <question>"); return
    await u.message.reply_text("⏳ Bono réfléchit...")
    try:
        r = httpx.post(f"{BASE}/ask", json={"text": text, "speak": True}, timeout=45).json()
        if r.get("error"):
            await u.message.reply_text(f"Err: {r['error']}"); return
        await u.message.reply_text(f"💬 {r.get('response', '')}\n\n_tools: {','.join(r.get('tools_used', [])) or 'aucun'} / {r.get('total_ms', 0)}ms_", parse_mode="Markdown")
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


async def cmd_events(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    try:
        r = httpx.get(f"{BASE}/events?limit=10", timeout=5).json()
        lines = []
        for ev in r.get("events", [])[:10]:
            payload = json.loads(ev.get("payload") or "{}")
            msg = payload.get("msg", ev.get("type"))
            lines.append(f"[{time.strftime('%H:%M:%S', time.localtime(ev['ts']))}] {ev['severity']} {ev['type']}: {msg}")
        await u.message.reply_text("\n".join(lines) or "Aucun event")
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


async def cmd_exchanges(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    try:
        r = httpx.get(f"{BASE}/exchanges?limit=5", timeout=5).json()
        lines = []
        for ex in r.get("exchanges", [])[:5]:
            t = time.strftime("%H:%M:%S", time.localtime(ex["ts"]))
            lines.append(f"[{t}] TU: {ex.get('driver_msg', '')[:60]}\n       BONO: {ex.get('bono_msg', '')[:80]}")
        await u.message.reply_text("\n\n".join(lines) or "Aucun exchange")
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


async def cmd_processes(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    try:
        r = httpx.get(f"{BASE}/processes", timeout=5).json()
        lines = [f"{p['name']:20} PID={p['pid']} RAM={p['ram_mb']}MB up={p['uptime_s']}s" for p in r.get("processes", [])]
        await u.message.reply_text("```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown")
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


async def cmd_costs(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    try:
        r = httpx.get(f"{BASE}/costs?period_h=24", timeout=5).json()
        lines = [f"Total 24h: ${r.get('total_usd', 0):.4f}"]
        for m in r.get("by_model", []):
            lines.append(f"  {m['model']}: {m['n']} calls / in={m['tin']} out={m['tout']} / ${m['cost']:.4f}")
        await u.message.reply_text("\n".join(lines))
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


async def cmd_stop(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(u): return
    try:
        r = httpx.post(f"{BASE}/stop_speak", timeout=5).json()
        await u.message.reply_text(f"🔇 {r.get('status', '?')}")
    except Exception as e:
        await u.message.reply_text(f"Err: {e}")


# Push events listener (background task)
async def events_pusher(app):
    """Subscribe to ZMQ EVENTS bus and push notifications to Telegram."""
    if not PUSH_EVENTS or not CHAT_ID:
        logger.info("[push] disabled (TELEGRAM_PUSH_EVENTS!=1 or no CHAT_ID)")
        return
    sub = make_sub(ZMQ_EVENTS, topic="play.status", conflate=False)
    logger.info(f"[push] subscribed {ZMQ_EVENTS}")
    while True:
        try:
            res = recv_json(sub, timeout_ms=1000)
            if res is None:
                await asyncio.sleep(0.1); continue
            topic, payload = res
            if topic == "play.status" and payload.get("state") == "playing":
                txt = payload.get("text", "")
                if txt:
                    await app.bot.send_message(chat_id=CHAT_ID, text=f"🔊 {txt}")
        except Exception as e:
            logger.warning(f"[push] err : {e}")
            await asyncio.sleep(1)


async def post_init(app):
    """Spawn background tasks after Application init."""
    asyncio.create_task(events_pusher(app))


def main():
    logger.add("logs/telegram_bot.log", rotation="10 MB", retention=3)
    logger.info("=== Bono v2 telegram_bot start ===")
    if not TOKEN:
        logger.warning("No TELEGRAM_BONO_TOKEN defined — skipping Telegram bot start (create dedicated bot via @BotFather and set TELEGRAM_BONO_TOKEN in .env)")
        # idle loop so launcher doesn't restart-loop
        while True:
            time.sleep(60)
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("state", cmd_state))
    app.add_handler(CommandHandler("tyres", cmd_tyres))
    app.add_handler(CommandHandler("fuel", cmd_fuel))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("say", cmd_say))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("exchanges", cmd_exchanges))
    app.add_handler(CommandHandler("processes", cmd_processes))
    app.add_handler(CommandHandler("costs", cmd_costs))
    app.add_handler(CommandHandler("stop", cmd_stop))
    logger.info(f"Telegram bot polling start, push_events={PUSH_EVENTS}")
    app.run_polling()


if __name__ == "__main__":
    main()
