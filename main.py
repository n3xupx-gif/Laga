import asyncio
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes
import aiosqlite
import hashlib
import hmac
import json
from datetime import datetime
from urllib.parse import parse_qs

# Local imports
from config import settings
from database import (
    init_db, get_user, create_user, update_user_balance, ban_user, 
    get_all_users, get_leaderboard, get_available_quiz, create_quiz, 
    answer_quiz, get_all_quizzes, delete_quiz, create_withdraw, 
    get_withdraw_requests, process_withdraw, get_setting, set_setting, 
    get_all_settings, get_force_channels, add_force_channel, remove_force_channel,
    DATABASE_PATH
)

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Telegram Bot Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Create or update user
    db_user = await get_user(user.id)
    if not db_user:
        referred_by = None
        # Check referral
        if context.args and context.args[0].startswith('ref_'):
            try:
                referred_by = int(context.args[0][4:])
                if referred_by == user.id:
                    referred_by = None
            except:
                pass
        
        await create_user({
            'id': user.id,
            'first_name': user.first_name,
            'username': user.username,
            'referred_by': referred_by
        })
        
        # Handle referral bonus
        if referred_by:
            ref_bonus = float(await get_setting('referral_bonus') or '5.0')
            await update_user_balance(referred_by, ref_bonus)
            async with aiosqlite.connect(DATABASE_PATH) as db:
                await db.execute(
                    'UPDATE users SET referral_count = referral_count + 1 WHERE id = ?',
                    (referred_by,)
                )
                await db.commit()
    
    keyboard = [
        [InlineKeyboardButton("🚀 Open App", web_app=WebAppInfo(url=settings.WEBAPP_URL))],
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
            await asyncio.sleep(0.05) # Avoid flood limits
        except Exception as e:
            logger.error(f"Failed to send to {user['id']}: {e}")
    
    await update.message.reply_text(f"Message sent to {sent} users")

# --- FastAPI App ---

app = FastAPI(title="Quiz & Earn Platform API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Telegram validation
async def validate_telegram_data(init_data: str) -> dict:
    try:
        parsed_data = dict(pair.split('=') for pair in init_data.split('&'))
        hash_value = parsed_data.pop('hash', None)
        
        if not hash_value:
            return None
        
        # Sort and create check string
        sorted_items = sorted(parsed_data.items())
        check_string = '\n'.join(f'{k}={v}' for k, v in sorted_items)
        
        # Create secret key
        secret_key = hmac.new(
            b'WebAppData',
            settings.BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()
        
        # Calculate hash
        calculated_hash = hmac.new(
            secret_key,
            check_string.encode(),
            hashlib.sha256
        ).hexdigest()
        
        if calculated_hash == hash_value:
            user_data = json.loads(parsed_data.get('user', '{}'))
            return user_data
        return None
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return None

def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS

async def get_current_user(request: Request) -> Dict:
    init_data = request.headers.get('X-Telegram-Init-Data', '')
    if not init_data:
        init_data = request.query_params.get('init_data', '')
    
    if not init_data:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    user_data = await validate_telegram_data(init_data)
    if not user_data:
        raise HTTPException(status_code=401, detail="Invalid authentication")
    
    return user_data

# --- Web Routes ---

@app.on_event("startup")
async def startup():
    # Initialize Database
    await init_db()
    logger.info("Database initialized")
    
    # Initialize Telegram Bot
    application = Application.builder().token(settings.BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("broadcast", broadcast))
    
    # Start polling in background
    # Note: For production scale, use webhooks instead of polling
    asyncio.create_task(application.run_polling(allowed_updates=Update.ALL_TYPES))
    logger.info("Bot started polling")

@app.get("/", response_class=HTMLResponse)
async def serve_app():
    # Make sure to create a public/index.html file for your frontend
    index_path = Path("public/index.html")
    if index_path.exists():
        return index_path.read_text(encoding='utf-8')
    return "<h1>Quiz Earn Backend Running</h1><p>Frontend file not found.</p>"

@app.get("/api/me")
async def get_me(request: Request):
    try:
        user_data = await get_current_user(request)
        user = await get_user(user_data['id'])
        
        if not user:
            user = await create_user({
                'id': user_data['id'],
                'first_name': user_data.get('first_name', 'User'),
                'username': user_data.get('username', ''),
            })
        
        return {
            "success": True,
            "user": user,
            "is_admin": is_admin(user['id'])
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/quiz/next")
async def api_get_next_quiz(request: Request):
    user_data = await get_current_user(request)
    user = await get_user(user_data['id'])
    
    if not user or user['is_banned']:
        raise HTTPException(status_code=403, detail="User not found or banned")
    
    quiz = await get_available_quiz(user_data['id'])
    if not quiz:
        return {"success": False, "message": "No more quizzes available"}
    
    timer = int(await get_setting('quiz_timer') or '15')
    
    return {
        "success": True,
        "quiz": {
            "id": quiz['id'],
            "question": quiz['question'],
            "options": [quiz['option1'], quiz['option2'], quiz['option3'], quiz['option4']],
            "timer": timer,
            "reward": quiz['reward']
        }
    }

@app.post("/api/quiz/answer")
async def api_answer_quiz(request: Request):
    user_data = await get_current_user(request)
    user = await get_user(user_data['id'])
    if not user or user['is_banned']:
        raise HTTPException(status_code=403, detail="Banned")

    body = await request.json()
    quiz_id = body.get('quiz_id')
    answer = body.get('answer')
    
    if quiz_id is None or answer is None:
        raise HTTPException(status_code=400, detail="Missing data")
    
    result = await answer_quiz(user_data['id'], quiz_id, answer)
    
    if result['success']:
        user = await get_user(user_data['id'])
        result['new_balance'] = user['balance']
    
    return result

@app.get("/api/profile")
async def api_get_profile(request: Request):
    user_data = await get_current_user(request)
    user = await get_user(user_data['id'])
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    leaderboard = await get_leaderboard(10)
    bot_username = "your_bot" # Fallback
    if settings.BOT_TOKEN:
        bot_username = settings.BOT_TOKEN.split(':')[0]
    
    return {
        "success": True,
        "user": user,
        "leaderboard": leaderboard,
        "referral_link": f"https://t.me/{bot_username}bot?start=ref_{user['id']}"
    }

@app.post("/api/withdraw")
async def api_create_withdraw(request: Request):
    user_data = await get_current_user(request)
    user = await get_user(user_data['id'])
    
    if not user or user['is_banned']:
        raise HTTPException(status_code=403, detail="User banned or missing")

    withdraw_enabled = await get_setting('withdraw_enabled')
    if withdraw_enabled == '0':
        raise HTTPException(status_code=400, detail="Withdraw disabled")

    body = await request.json()
    amount = float(body.get('amount', 0))
    method = body.get('method')
    number = body.get('number')
    
    min_withdraw = float(await get_setting('min_withdraw') or '10.0')
    fee = float(await get_setting('withdraw_fee') or '0.0')
    
    if amount < min_withdraw:
        raise HTTPException(status_code=400, detail=f"Minimum withdraw is {min_withdraw}")
    
    if user['balance'] < amount + fee:
        raise HTTPException(status_code=400, detail="Insufficient balance")
    
    await update_user_balance(user_data['id'], -(amount + fee))
    withdraw_id = await create_withdraw(user_data['id'], amount, method, number, fee)
    
    return {
        "success": True,
        "withdraw_id": withdraw_id,
        "new_balance": (await get_user(user_data['id']))['balance']
    }

# --- Admin API Endpoints ---

@app.get("/api/admin/stats")
async def api_admin_stats(request: Request):
    user_data = await get_current_user(request)
    if not is_admin(user_data['id']):
        raise HTTPException(status_code=403, detail="Not authorized")

    users = await get_all_users()
    withdraws = await get_withdraw_requests('pending')
    
    return {
        "success": True,
        "stats": {
            "total_users": len(users),
            "total_balance": sum(u['balance'] for u in users),
            "pending_withdraws": len(withdraws)
        }
    }

@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    user_data = await get_current_user(request)
    if not is_admin(user_data['id']):
        raise HTTPException(status_code=403, detail="Not authorized")
    return {"success": True, "users": await get_all_users()}

@app.post("/api/admin/quiz")
async def api_admin_create_quiz(request: Request):
    user_data = await get_current_user(request)
    if not is_admin(user_data['id']):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    body = await request.json()
    quiz_id = await create_quiz(body)
    return {"success": True, "quiz_id": quiz_id}

@app.get("/api/admin/quizzes")
async def api_admin_get_quizzes(request: Request):
    user_data = await get_current_user(request)
    if not is_admin(user_data['id']):
        raise HTTPException(status_code=403, detail="Not authorized")
    return {"success": True, "quizzes": await get_all_quizzes()}

@app.delete("/api/admin/quiz/{quiz_id}")
async def api_admin_delete_quiz(quiz_id: int, request: Request):
    user_data = await get_current_user(request)
    if not is_admin(user_data['id']):
        raise HTTPException(status_code=403, detail="Not authorized")
    await delete_quiz(quiz_id)
    return {"success": True}

@app.get("/api/admin/withdraws")
async def api_admin_withdraws(request: Request):
    user_data = await get_current_user(request)
    if not is_admin(user_data['id']):
        raise HTTPException(status_code=403, detail="Not authorized")
    return {"success": True, "withdraws": await get_withdraw_requests()}

@app.post("/api/admin/withdraw/{withdraw_id}/process")
async def api_admin_process_withdraw(withdraw_id: int, request: Request):
    user_data = await get_current_user(request)
    if not is_admin(user_data['id']):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    body = await request.json()
    status = body.get('status', 'approved')
    await process_withdraw(withdraw_id, status)
    return {"success": True}

@app.get("/api/settings")
async def api_get_settings(request: Request):
    user_data = await get_current_user(request)
    if not is_admin(user_data['id']):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    settings_data = await get_all_settings()
    channels = await get_force_channels()
    return {"success": True, "settings": settings_data, "channels": channels}

@app.post("/api/settings")
async def api_update_settings(request: Request):
    user_data = await get_current_user(request)
    if not is_admin(user_data['id']):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    body = await request.json()
    for key, value in body.items():
        await set_setting(str(key), str(value))
    return {"success": True}

if __name__ == "__main__":
    import uvicorn
    # Render sets the PORT environment variable
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)