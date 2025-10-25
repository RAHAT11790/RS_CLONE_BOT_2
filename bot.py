# render_deployer.py
import os
import sys
import json
import signal
import subprocess
import threading
from pathlib import Path
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

# ---------- CONFIG ----------
UPLOAD_DIR = Path("uploaded_bots")
DB_FILE = Path("bots_data.json")
LOGS_DIR = Path("bot_logs")

UPLOAD_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# IMPORTANT: set these as environment variables on Render
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")

if not TELEGRAM_TOKEN or not ADMIN_ID:
    print("❌ ERROR: Missing TELEGRAM_TOKEN or ADMIN_ID environment variables.")
    print("Set them in Render → Environment → Environment Variables")
    sys.exit(1)

ADMIN_ID = int(ADMIN_ID)
PY = sys.executable  # Python executable

# ---------- Helper Functions ----------
def load_bots():
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_bots(data):
    DB_FILE.write_text(json.dumps(data, indent=2))

bots = load_bots()
user_state = {}

# ---------- Process Control ----------
def start_bot_process(bot_path: str, bot_name: str):
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{bot_name}.log"
    err_file = LOGS_DIR / f"{bot_name}.err"

    with open(log_file, "ab") as lf, open(err_file, "ab") as ef:
        proc = subprocess.Popen(
            [PY, bot_path],
            stdout=lf,
            stderr=ef,
            cwd=str(Path(bot_path).parent),
            preexec_fn=os.setsid
        )
    return proc.pid, str(log_file), str(err_file)

def stop_bot_process(pid: int):
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            return False

def auto_restart_bots():
    if not bots:
        print("No saved bots to restart.")
        return
    print("🔁 Auto-restarting saved bots...")
    for name, info in list(bots.items()):
        path = info.get("path")
        if path and Path(path).exists():
            try:
                pid, log, err = start_bot_process(path, name)
                info.update({"pid": pid, "log": log, "err": err})
                print(f"✅ Started {name} → PID {pid}")
            except Exception as e:
                print(f"❌ Failed to start {name}: {e}")
        else:
            print(f"⚠️ Missing file for {name}, removing from db.")
            bots.pop(name, None)
    save_bots(bots)

# ---------- Telegram Handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("❌ You are not authorized to use this bot.")

    kb = [
        [InlineKeyboardButton("🚀 Deploy Bot", callback_data="deploy")],
        [InlineKeyboardButton("📋 Active Bots", callback_data="list")],
        [InlineKeyboardButton("📄 View Logs", callback_data="view_logs")],
        [InlineKeyboardButton("🛑 Stop All Bots", callback_data="stop_all")],
        [InlineKeyboardButton("🔁 Restart All", callback_data="restart_all")]
    ]
    await update.message.reply_text("🤖 Render Bot Deployer\nChoose an option:", reply_markup=InlineKeyboardMarkup(kb))

def is_admin(update: Update):
    try:
        return update.effective_user.id == ADMIN_ID
    except Exception:
        return False

async def cb_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return await query.message.reply_text("❌ Not authorized.")

    data = query.data
    if data == "deploy":
        user_state[ADMIN_ID] = {"step": "ask_bot"}
        await query.message.reply_text("📤 Please upload your bot.py file (as a Document).")

    elif data == "list":
        if not bots:
            return await query.message.reply_text("📭 No active bots.")
        msg = "\n".join(f"• {n} — PID: {b.get('pid')}" for n, b in bots.items())
        await query.message.reply_text(f"🤖 Active Bots:\n{msg}")

    elif data == "stop_all":
        count = 0
        for _, info in list(bots.items()):
            pid = info.get("pid")
            if pid and stop_bot_process(pid):
                count += 1
        bots.clear()
        save_bots(bots)
        await query.message.reply_text(f"🛑 Stopped {count} bots.")

    elif data == "restart_all":
        count = 0
        for name, info in list(bots.items()):
            path = info.get("path")
            if path and Path(path).exists():
                pid, log, err = start_bot_process(path, name)
                info.update({"pid": pid, "log": log, "err": err})
                count += 1
        save_bots(bots)
        await query.message.reply_text(f"🔁 Restarted {count} bots.")

    elif data == "view_logs":
        if not bots:
            return await query.message.reply_text("📭 No bots running.")
        kb = [[InlineKeyboardButton(name, callback_data=f"log__{name}")] for name in bots.keys()]
        await query.message.reply_text("Select a bot to view logs:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("log__"):
        name = data.split("log__", 1)[1]
        info = bots.get(name)
        if not info or not Path(info.get("log", "")).exists():
            return await query.message.reply_text("⚠️ Log not found.")
        lines = Path(info["log"]).read_text(errors="replace").splitlines()
        text = "\n".join(lines[-80:]) or "(empty)"
        if len(text) > 3900:
            text = text[-3900:]
        await query.message.reply_text(f"<pre>{text}</pre>", parse_mode="HTML")

async def doc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("❌ Not authorized.")

    doc = update.message.document
    if not doc:
        return await update.message.reply_text("Please send the file as Document.")

    state = user_state.get(ADMIN_ID, {})
    if state.get("step") == "ask_bot":
        dest = UPLOAD_DIR / f"{ADMIN_ID}_{doc.file_name}"
        await doc.download_to_drive(str(dest))
        user_state[ADMIN_ID].update({"bot_path": str(dest), "step": "ask_req"})
        await update.message.reply_text("✅ bot.py uploaded. Now upload requirements.txt (or send 'none').")

    elif state.get("step") == "ask_req":
        if doc.file_name.endswith(".txt"):
            dest = UPLOAD_DIR / f"{ADMIN_ID}_requirements.txt"
            await doc.download_to_drive(str(dest))
            user_state[ADMIN_ID]["req_path"] = str(dest)
            await update.message.reply_text("📦 Installing requirements...")
            await finalize_deploy(update)
        else:
            await update.message.reply_text("Upload requirements.txt or send 'none'.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    txt = update.message.text.strip().lower()
    if txt == "none" and ADMIN_ID in user_state:
        await finalize_deploy(update)

async def finalize_deploy(update: Update):
    info = user_state[ADMIN_ID]
    bot_path = info.get("bot_path")
    req_path = info.get("req_path")

    if req_path and Path(req_path).exists():
        proc = subprocess.run([PY, "-m", "pip", "install", "-r", req_path], capture_output=True, text=True)
        if proc.returncode == 0:
            await update.message.reply_text("✅ Requirements installed.")
        else:
            await update.message.reply_text(f"⚠️ pip error:\n<pre>{proc.stderr[-1500:]}</pre>", parse_mode="HTML")

    bot_name = Path(bot_path).name
    pid, log, err = start_bot_process(bot_path, bot_name)
    bots[bot_name] = {"path": str(bot_path), "pid": pid, "log": log, "err": err}
    save_bots(bots)
    user_state.pop(ADMIN_ID, None)
    await update.message.reply_text(f"🚀 {bot_name} started successfully! (PID: {pid})")

# ---------- Threads ----------
def run_telegram():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(cb_query))
    app.add_handler(MessageHandler(filters.Document.ALL, doc_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("✅ Telegram polling started.")
    app.run_polling(poll_interval=1.0)

# ---------- Flask ----------
flask_app = Flask("deployer")

@flask_app.route("/")
def index():
    return jsonify({"status": "ok", "active_bots": len(bots)})

# ---------- MAIN ----------
if __name__ == "__main__":
    auto_restart_bots()
    threading.Thread(target=run_telegram, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print(f"🌐 Flask running on port {port}")
    flask_app.run(host="0.0.0.0", port=port)
