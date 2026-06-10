import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from supabase import create_client

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот охраны работает ✅\nКоманда: /shift")

async def shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if GROUP_ID and str(update.effective_chat.id) != GROUP_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    result = (
        supabase.table("attendance_status")
        .select("clock_in_time, employees(full_name, position, location)")
        .eq("on_shift", True)
        .execute()
    )

    data = result.data or []

    if not data:
        await update.message.reply_text("🔴 Сейчас никто не отмечен на смене.")
        return

    text = f"🟢 Сейчас на смене: {len(data)}\n\n"
    for item in data:
        e = item.get("employees") or {}
        text += f"• {e.get('full_name', 'Без имени')}\n"

    await update.message.reply_text(text)

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("shift", shift))
app.run_polling()
