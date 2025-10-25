# ---------- render_deployer.py ----------
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

# ---------- ENVIRONMENT ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")

if not TELEGRAM_TOKEN or not ADMIN_ID:
    print("❌ ERROR: Missing TELEGRAM_TOKEN or ADMIN_ID environment variables.")
    print("➡️  Set them in Render → Environment → Environment Variables")
    sys.exit(1)

try:
    ADMIN_ID = int(ADMIN_ID)
except ValueError:
    print("⚠️ ADMIN_ID must be numeric!")
    sys.exit(1)

PY = sys.executable  # current Python interpreter

# ---------- helpers ----------
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

# ---------- Launch / Stop ----------
def start_bot_process(bot_path: str, bot_name: str):
    """Start a bot as background process with log redirection."""
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{bot_name}.log"
    err_file = LOGS_DIR / f"{bot_name}.err"

    lf = open(log_file, "ab")
    ef = open(err_file, "ab")

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

# ---------- Auto restart ----------
def auto_restart_bots():
    if not bots:
        print("ℹ️ No saved bots to restart.")
        return
    print("🔁 Auto-restarting saved bots...")
    for name, info in list(bots.items()):
        path = info.get("path")
        if path and Path(path).exists():
            try:
                pid, log, err = start_bot_process(path, name)
                info.update({"pid": pid, "log": log, "err": err})
                print(f"✅ Restarted {name} → PID {pid}")
            except Exception as e:
                print(f"⚠️ Failed to restart {name}: {e}")
        else:
            print(f"🗑️ Missing file for {name}, removing from DB.")
            bots.pop(name, None)
    save_bots(bots)

# ---------- Telegram handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorized.")
        return
    kb = [
        [InlineKeyboardButton("🚀 Deploy Bot", callback_data="deploy")],
        [InlineKeyboardButton("📋 Active Bots", callback_data="list")],
        [InlineKeyboardButton("📄 View Logs", callback_data="view_logs")],
        [InlineKeyboardButton("🛑 Stop All Bots", callback_data="stop_all")],
        [InlineKeyboardButton("🔁 Restart All", callback_data="restart_all")]
    ]
    await update.message.reply_text(
        "🤖 Welcome to Render Deployer Bot\nSelect an option:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cb_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user.id
    if user != ADMIN_ID:
        await query.message.reply_text("❌ Unauthorized.")
        return

    data = query.data

    if data == "deploy":
        user_state[user] = {"step": "ask_bot"}
        await query.message.reply_text("📤 Please upload your bot.py file.")
        return

    if data == "list":
        if not bots:
            await query.message.reply_text("📭 No active bots.")
        else:
            txt = "\n".join([f"• {k} — PID {v.get('pid','?')}" for k,v in bots.items()])
            await query.message.reply_text("🤖 Active Bots:\n" + txt)
        return

    if data == "stop_all":
        count = 0
        for name, info in list(bots.items()):
            pid = info.get("pid")
            if pid and stop_bot_process(pid):
                count += 1
        bots.clear()
        save_bots(bots)
        await query.message.reply_text(f"🛑 Stopped {count} bots.")
        return

    if data == "restart_all":
        count = 0
        for name, info in list(bots.items()):
            path = info.get("path")
            if path and Path(path).exists():
                pid, log, err = start_bot_process(path, name)
                info.update({"pid": pid, "log": log, "err": err})
                count += 1
        save_bots(bots)
        await query.message.reply_text(f"🔁 Restarted {count} bots.")
        return

    if data == "view_logs":
        if not bots:
            await query.message.reply_text("📭 No bots running.")
            return
        kb = [[InlineKeyboardButton(k, callback_data=f"log__{k}")] for k in bots.keys()]
        await query.message.reply_text("Select bot:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("log__"):
        name = data.split("log__", 1)[1]
        info = bots.get(name)
        if not info:
            await query.message.reply_text("❌ Not found.")
            return
        logpath = info.get("log")
        if not logpath or not Path(logpath).exists():
            await query.message.reply_text("⚠️ No log found.")
            return
        text = Path(logpath).read_text(errors="ignore").splitlines()[-50:]
        await query.message.reply_text(f"<pre>{'\n'.join(text)}</pre>", parse_mode="HTML")

async def doc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    if user != ADMIN_ID:
        await update.message.reply_text("❌ Unauthorized.")
        return

    if user not in user_state:
        await update.message.reply_text("⚠️ Press Deploy first.")
        return

    step = user_state[user]["step"]
    doc = update.message.document

    if step == "ask_bot":
        if not doc.file_name.endswith(".py"):
            await update.message.reply_text("Please upload a .py file.")
            return
        dest = UPLOAD_DIR / f"{user}_{doc.file_name}"
        await doc.download_to_drive(str(dest))
        user_state[user].update({"bot_path": str(dest), "step": "ask_req"})
        await update.message.reply_text("✅ bot.py received. Now send requirements.txt or type 'none'.")

    elif step == "ask_req":
        if doc.file_name.lower().endswith("requirements.txt"):
            dest = UPLOAD_DIR / f"{user}_requirements.txt"
            await doc.download_to_drive(str(dest))
            user_state[user]["req_path"] = str(dest)
            await update.message.reply_text("📦 requirements received, installing...")
            await finalize_deploy(update, user)
        else:
            await update.message.reply_text("Please upload requirements.txt or send 'none'.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    if user != ADMIN_ID:
        return
    if user in user_state and user_state[user].get("step") == "ask_req":
        if update.message.text.strip().lower() == "none":
            await update.message.reply_text("📦 Skipping requirements. Deploying...")
            await finalize_deploy(update, user)

async def finalize_deploy(update: Update, user_id: int):
    info = user_state[user_id]
    bot_path = info.get("bot_path")
    req_path = info.get("req_path")

    if req_path and Path(req_path).exists():
        proc = subprocess.run([PY, "-m", "pip", "install", "-r", req_path],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            await update.message.reply_text(f"⚠️ pip install failed:\n{proc.stderr[-1500:]}")
        else:
            await update.message.reply_text("✅ Requirements installed.")

    if not bot_path or not Path(bot_path).exists():
        await update.message.reply_text("❌ Bot file missing.")
        user_state.pop(user_id, None)
        return

    bot_name = Path(bot_path).name
    pid, log, err = start_bot_process(bot_path, bot_name)
    bots[bot_name] = {"path": str(bot_path), "pid": pid, "log": log, "err": err}
    save_bots(bots)
    user_state.pop(user_id, None)
    await update.message.reply_text(f"🚀 {bot_name} started (PID: {pid}).")

# ---------- Flask ----------
flask_app = Flask("deployer")

@flask_app.route("/")
def index():
    return jsonify({"status": "ok", "active_bots": len(bots)})

# ---------- MAIN ----------
def run_telegram():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(cb_query))
    app.add_handler(MessageHandler(filters.Document.ALL, doc_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("📡 Starting Telegram bot...")
    app.run_polling()

if __name__ == "__main__":
    print("🚀 Starting Render Deployer Bot")
    auto_restart_bots()

    t = threading.Thread(target=run_telegram, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", "8000"))
    print(f"🌐 Running Flask healthcheck on port {port}")
    flask_app.run(host="0.0.0.0", port=port)
