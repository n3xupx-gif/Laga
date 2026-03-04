import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes
from config import settings
from database import *

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Create or update user
    await get_user(user.id) or await create_user({
        'id': user.id,
        'first_name': user.first_name,
        'username': user.username
    })
    
    # Check referral
    if context.args and context.args[0].startswith('ref_'):
        referrer_id = int(context.args[0][4:])
        if referrer_id != user.id:
            existing_user = await get_user(user.id)
            if not existing_user:
                await create_user({
                    'id': user.id,
                    'first_name': user.first_name,
                    'username': user.username,
                    'referred_by': referrer_id
                })
    
    keyboard = [
        [InlineKeyboardButton("🚀 Open App", web_app=WebAppInfo(url=f"{settings.WEBAPP_URL}"))],
        [InlineKeyboardButton("📢 Join Channel", url="https://t.me/your_channel")]
    ]
    
    await update.message.reply_text(
        f"👋 স্বাগতম বস!\n"
        f"এখানে কুইজ খেলুন, টাকা জিতুন, রেফার করুন আর ইনকাম তুলুন 💰🔥",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in settings.ADMIN_IDS:
        await update.message.reply_text("Not authorized")
        return
    
    message = ' '.join(context.args)
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    users = await get_all_users()
    sent = 0
    
    for user in users:
        try:
            await context.bot.send_message(user['id'], message)
            sent += 1
        except:
            pass
    
    await update.message.reply_text(f"Message sent to {sent} users")

def main():
    application = Application.builder().token(settings.BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("broadcast", broadcast))
    
    application.run_polling()

if __name__ == "__main__":
    asyncio.run(init_db())
    main()