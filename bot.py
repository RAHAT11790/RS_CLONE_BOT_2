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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "PUT_TOKEN_HERE")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # your Telegram user id

# Path to Python executable to spawn child bots with same interpreter
PY = sys.executable

# ---------- helpers: load/save ----------
def load_bots():
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_bots(data):
    DB_FILE.write_text(json.dumps(data, indent=2))

# ---------- global state ----------
bots = load_bots()  # structure: { "botname.py": {"path": "...", "pid": 123, "log": "logs/.."} }
user_state = {}     # temporary per-user upload state

# ---------- Launch/Stop functions ----------
def start_bot_process(bot_path: str, bot_name: str):
    """Start a bot.py as background process, redirect logs to files, return pid."""
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{bot_name}.log"
    err_file = LOGS_DIR / f"{bot_name}.err"

    # open files in append-binary mode and pass to Popen
    lf = open(log_file, "ab")
    ef = open(err_file, "ab")

    # spawn process; keep stdout/stderr to files
    proc = subprocess.Popen([PY, bot_path],
                            stdout=lf,
                            stderr=ef,
                            cwd=str(Path(bot_path).parent),
                            preexec_fn=os.setsid)  # create new process group
    return proc.pid, str(log_file), str(err_file), lf, ef

def stop_bot_process(pid: int):
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)  # kill process group
        return True
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            return False

# ---------- Auto-restart on start ----------
def auto_restart_bots():
    if not bots:
        print("No saved bots to restart.")
        return
    print("Auto-restarting saved bots...")
    for name, info in list(bots.items()):
        path = info.get("path")
        if path and Path(path).exists():
            try:
                pid, log, err, lf, ef = start_bot_process(path, name)
                info["pid"] = pid
                info["log"] = log
                info["err"] = err
                print(f"Started {name} -> PID {pid}")
            except Exception as e:
                print(f"Failed to start {name}: {e}")
        else:
            print(f"Bot file missing for {name}, removing from db.")
            bots.pop(name, None)
    save_bots(bots)

# ---------- Telegram handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("🚀 Deploy Bot", callback_data="deploy")],
        [InlineKeyboardButton("📋 Active Bots", callback_data="list")],
        [InlineKeyboardButton("📄 View Logs", callback_data="view_logs")],
        [InlineKeyboardButton("🛑 Stop All Bots", callback_data="stop_all")],
        [InlineKeyboardButton("🔁 Restart All", callback_data="restart_all")]
    ]
    await update.message.reply_text("Hello — Bot Deployer (Render)\nChoose:", reply_markup=InlineKeyboardMarkup(kb))

def is_admin(update: Update):
    try:
        uid = update.effective_user.id
        return uid == ADMIN_ID
    except Exception:
        return False

async def cb_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user.id
    if user != ADMIN_ID:
        await query.message.reply_text("❌ You are not authorized to use this deployer.")
        return

    data = query.data
    if data == "deploy":
        user_state[user] = {"step": "ask_bot"}
        await query.message.reply_text("📤 Please upload your bot.py file (as a Document).")
    elif data == "list":
        if not bots:
            await query.message.reply_text("📭 No active bots.")
        else:
            txt = "🤖 Active bots:\n"
            for name, info in bots.items():
                txt += f"• {name} — PID: {info.get('pid','?')}\n"
            await query.message.reply_text(txt)
    elif data == "stop_all":
        count = 0
        for name, info in list(bots.items()):
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
                pid, log, err, lf, ef = start_bot_process(path, name)
                info["pid"] = pid
                info["log"] = log
                info["err"] = err
                count += 1
        save_bots(bots)
        await query.message.reply_text(f"🔁 Restarted {count} bots.")
    elif data == "view_logs":
        if not bots:
            await query.message.reply_text("📭 No bots (no logs).")
            return
        # create buttons per bot to fetch logs
        kb = []
        for name in bots.keys():
            kb.append([InlineKeyboardButton(name, callback_data=f"log__{name}")])
        await query.message.reply_text("Select a bot to view recent logs:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("log__"):
        name = data.split("log__",1)[1]
        info = bots.get(name)
        if not info:
            await query.message.reply_text("Bot not found.")
            return
        logpath = info.get("log")
        if not logpath or not Path(logpath).exists():
            await query.message.reply_text("Log file not found.")
            return
        # send last 80 lines
        try:
            with open(logpath, "rb") as f:
                content = f.read().decode(errors="replace").splitlines()
                tail = "\n".join(content[-80:]) or "(empty)"
                if len(tail) > 3900:
                    tail = tail[-3900:]
                await query.message.reply_text(f"📄 Last lines of {name}:\n<pre>{tail}</pre>", parse_mode="HTML")
        except Exception as e:
            await query.message.reply_text(f"Could not read log: {e}")

async def doc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    if user != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized.")
        return

    if user not in user_state:
        await update.message.reply_text("⚠️ First press Deploy and follow prompts.")
        return

    step = user_state[user]["step"]
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send the file as Document.")
        return

    if step == "ask_bot":
        # save bot file
        filename = doc.file_name
        if not filename.endswith(".py"):
            await update.message.reply_text("⚠️ Please upload a .py file (bot.py).")
            return
        dest = UPLOAD_DIR / f"{user}_{filename}"
        await doc.download_to_drive(str(dest))
        user_state[user]["bot_path"] = str(dest)
        user_state[user]["step"] = "ask_req"
        await update.message.reply_text("✅ bot.py received. Now upload requirements.txt (or send 'none').")
    elif step == "ask_req":
        # accept requirements or the word 'none'
        filename = doc.file_name
        if filename.lower().endswith("requirements.txt") or filename.lower().endswith(".txt"):
            dest = UPLOAD_DIR / f"{user}_requirements.txt"
            await doc.download_to_drive(str(dest))
            user_state[user]["req_path"] = str(dest)
            await update.message.reply_text("📦 requirements received. Installing & deploying...")
            await finalize_deploy(update, user)
        else:
            await update.message.reply_text("Please upload a requirements.txt file (or send a message 'none').")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    if user != ADMIN_ID:
        return
    if user in user_state and user_state[user].get("step") == "ask_req":
        # allow sending 'none' to skip requirements
        txt = (update.message.text or "").strip().lower()
        if txt == "none":
            await update.message.reply_text("📦 Skipping requirements. Deploying...")
            await finalize_deploy(update, user)
        else:
            await update.message.reply_text("If you don't have requirements.txt, send 'none'. Otherwise upload the requirements.txt as Document.")

async def finalize_deploy(update: Update, user_id: int):
    info = user_state[user_id]
    bot_path = info.get("bot_path")
    req_path = info.get("req_path")
    if not bot_path or not Path(bot_path).exists():
        await update.message.reply_text("Bot file missing. Aborting.")
        user_state.pop(user_id, None)
        return

    # Install requirements if provided
    if req_path and Path(req_path).exists():
        try:
            await update.message.reply_text("Installing requirements (this may take a while)...")
            # run pip install -r <req> in a subprocess (blocking)
            proc = subprocess.run([PY, "-m", "pip", "install", "-r", req_path], capture_output=True, text=True)
            if proc.returncode != 0:
                await update.message.reply_text(f"⚠️ pip install failed:\n<pre>{proc.stderr[-1500:]}</pre>", parse_mode="HTML")
            else:
                await update.message.reply_text("✅ requirements installed.")
        except Exception as e:
            await update.message.reply_text(f"pip install error: {e}")

    # start bot process
    bot_name = Path(bot_path).name
    try:
        pid, log, err, lf, ef = start_bot_process(bot_path, bot_name)
        bots[bot_name] = {"path": str(bot_path), "pid": pid, "log": log, "err": err}
        save_bots(bots)
        await update.message.reply_text(f"🚀 {bot_name} started (PID: {pid}).")
    except Exception as e:
        await update.message.reply_text(f"Failed to start bot: {e}")

    user_state.pop(user_id, None)

# ---------- Setup Telegram Application (run in thread) ----------
def run_telegram():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CallbackQueryHandler(cb_query))
    application.add_handler(MessageHandler(filters.Document.ALL, doc_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("Starting Telegram bot (polling)...")
    application.run_polling(poll_interval=1.0)

# ---------- Flask app for healthcheck ----------
flask_app = Flask("deployer")

@flask_app.route("/")
def index():
    return jsonify({"status":"ok","bots_saved": len(bots)})

# ---------- Entrypoint ----------
if __name__ == "__main__":
    if TELEGRAM_TOKEN == "PUT_TOKEN_HERE" or ADMIN_ID == 0:
        print("ERROR: Set TELEGRAM_TOKEN and ADMIN_ID environment variables before running.")
        print("Example: export TELEGRAM_TOKEN=xxxxx; export ADMIN_ID=123456789")
        sys.exit(1)

    # auto-restart saved bots
    auto_restart_bots()

    # start telegram in thread
    t = threading.Thread(target=run_telegram, daemon=True)
    t.start()

    # run flask (main process). On Render you will use `python render_deployer.py`
    # Flask will keep process alive and serve healthcheck at '/'
    print("Starting Flask webserver for healthcheck on port 8000...")
    # Render sets PORT env var; fall back to 8000
    port = int(os.environ.get("PORT", "8000"))
    flask_app.run(host="0.0.0.0", port=port)
