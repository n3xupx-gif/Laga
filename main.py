import asyncio
import logging
import os
import sys
import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

import aiosqlite
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic_settings import BaseSettings
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Configuration ---

class Settings(BaseSettings):
    # Essential: Set these in Render Environment Variables
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS: List[int] = [123456789]  # Replace with your actual Telegram ID
    
    # Render provides a PORT variable automatically
    PORT: int = int(os.getenv("PORT", 8000))
    
    # Database (SQLite is file-based, suitable for simple Render deployments)
    DATABASE_URL: str = "sqlite+aiosqlite:///./quiz_earn.db"
    
    # Default Settings
    MIN_WITHDRAW: float = 10.0
    WITHDRAW_FEE: float = 0.0
    REFERRAL_BONUS: float = 5.0
    QUIZ_REWARD: float = 1.0
    QUIZ_TIMER: int = 15
    MONETAG_SCRIPT: str = ""
    
    # MUST set this in Render Env Vars (e.g., https://your-app.onrender.com)
    WEBAPP_URL: str = os.getenv("WEBAPP_URL", "http://localhost:8000")

    class Config:
        env_file = ".env"

settings = Settings()

# --- Logging ---

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Database Operations ---

DATABASE_PATH = "quiz_earn.db"

async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Users table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                username TEXT,
                balance REAL DEFAULT 0.0,
                referral_count INTEGER DEFAULT 0,
                referred_by INTEGER,
                is_banned INTEGER DEFAULT 0,
                total_quiz_played INTEGER DEFAULT 0,
                join_date TEXT NOT NULL,
                last_active TEXT
            )
        ''')
        
        # Quiz table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS quiz (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                option1 TEXT NOT NULL,
                option2 TEXT NOT NULL,
                option3 TEXT NOT NULL,
                option4 TEXT NOT NULL,
                correct_option INTEGER NOT NULL,
                reward REAL DEFAULT 1.0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
        ''')
        
        # User quiz history
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_quiz_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                quiz_id INTEGER NOT NULL,
                is_correct INTEGER NOT NULL,
                earned REAL DEFAULT 0.0,
                answered_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (quiz_id) REFERENCES quiz(id),
                UNIQUE(user_id, quiz_id)
            )
        ''')
        
        # Withdraw requests
        await db.execute('''
            CREATE TABLE IF NOT EXISTS withdraw_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                number TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                fee REAL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                processed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        
        # Settings table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        
        # Force channels table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS force_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                channel_name TEXT,
                channel_link TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')
        
        await db.commit()
        
        # Insert default settings
        default_settings = {
            'min_withdraw': '10.0',
            'withdraw_fee': '0.0',
            'referral_bonus': '5.0',
            'quiz_reward': '1.0',
            'quiz_timer': '15',
            'ads_enabled': '1',
            'withdraw_enabled': '1',
            'quiz_enabled': '1',
            'monetag_script': ''
        }
        
        for key, value in default_settings.items():
            await db.execute(
                'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
                (key, value)
            )
        
        await db.commit()
    logger.info("Database initialized")

async def get_user(user_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def create_user(user_data: Dict) -> Dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        now = datetime.utcnow().isoformat()
        await db.execute('''
            INSERT OR REPLACE INTO users 
            (id, name, username, balance, referral_count, referred_by, is_banned, total_quiz_played, join_date, last_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_data['id'],
            user_data.get('first_name', 'User'),
            user_data.get('username', ''),
            user_data.get('balance', 0.0),
            0,
            user_data.get('referred_by'),
            0,
            0,
            now,
            now
        ))
        await db.commit()
        return await get_user(user_data['id'])

async def update_user_balance(user_id: int, amount: float) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            'UPDATE users SET balance = balance + ?, last_active = ? WHERE id = ?',
            (amount, datetime.utcnow().isoformat(), user_id)
        )
        await db.commit()
        return True

async def get_all_users() -> List[Dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM users ORDER BY join_date DESC')
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_leaderboard(limit: int = 10) -> List[Dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT id, name, balance, referral_count, total_quiz_played 
            FROM users 
            WHERE is_banned = 0 
            ORDER BY balance DESC 
            LIMIT ?
        ''', (limit,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_available_quiz(user_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM quiz 
            WHERE is_active = 1 AND id NOT IN (
                SELECT quiz_id FROM user_quiz_history WHERE user_id = ?
            )
            ORDER BY RANDOM() LIMIT 1
        ''', (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def create_quiz(quiz_data: Dict) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute('''
            INSERT INTO quiz (question, option1, option2, option3, option4, correct_option, reward, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            quiz_data['question'],
            quiz_data['option1'],
            quiz_data['option2'],
            quiz_data['option3'],
            quiz_data['option4'],
            quiz_data['correct_option'],
            quiz_data.get('reward', 1.0),
            1,
            datetime.utcnow().isoformat()
        ))
        await db.commit()
        return cursor.lastrowid

async def answer_quiz(user_id: int, quiz_id: int, answer: int) -> Dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # Check if already answered
        cursor = await db.execute(
            'SELECT * FROM user_quiz_history WHERE user_id = ? AND quiz_id = ?',
            (user_id, quiz_id)
        )
        if await cursor.fetchone():
            return {'success': False, 'message': 'Already answered'}
        
        # Get quiz
        cursor = await db.execute('SELECT * FROM quiz WHERE id = ?', (quiz_id,))
        quiz = await cursor.fetchone()
        if not quiz:
            return {'success': False, 'message': 'Quiz not found'}
        
        quiz = dict(quiz)
        is_correct = answer == quiz['correct_option']
        reward = quiz['reward'] if is_correct else 0
        
        # Save answer
        await db.execute('''
            INSERT INTO user_quiz_history (user_id, quiz_id, is_correct, earned, answered_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, quiz_id, is_correct, reward, datetime.utcnow().isoformat()))
        
        # Update user balance and stats
        if is_correct:
            await db.execute(
                'UPDATE users SET balance = balance + ?, total_quiz_played = total_quiz_played + 1 WHERE id = ?',
                (reward, user_id)
            )
        else:
            await db.execute(
                'UPDATE users SET total_quiz_played = total_quiz_played + 1 WHERE id = ?',
                (user_id,)
            )
        
        await db.commit()
        
        return {
            'success': True,
            'is_correct': is_correct,
            'correct_option': quiz['correct_option'],
            'reward': reward
        }

async def get_all_quizzes() -> List[Dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM quiz ORDER BY created_at DESC')
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def delete_quiz(quiz_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('DELETE FROM quiz WHERE id = ?', (quiz_id,))
        await db.commit()
        return True

async def create_withdraw(user_id: int, amount: float, method: str, number: str, fee: float = 0.0) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute('''
            INSERT INTO withdraw_requests (user_id, amount, method, number, fee, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, amount, method, number, fee, 'pending', datetime.utcnow().isoformat()))
        await db.commit()
        return cursor.lastrowid

async def get_withdraw_requests(status: str = None) -> List[Dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cursor = await db.execute(
                'SELECT * FROM withdraw_requests WHERE status = ? ORDER BY created_at DESC',
                (status,)
            )
        else:
            cursor = await db.execute('SELECT * FROM withdraw_requests ORDER BY created_at DESC')
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def process_withdraw(withdraw_id: int, status: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            'UPDATE withdraw_requests SET status = ?, processed_at = ? WHERE id = ?',
            (status, datetime.utcnow().isoformat(), withdraw_id)
        )
        await db.commit()
        return True

async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute('SELECT value FROM settings WHERE key = ?', (key,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def set_setting(key: str, value: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
            (key, value)
        )
        await db.commit()
        return True

async def get_all_settings() -> Dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM settings')
        rows = await cursor.fetchall()
        return {row['key']: row['value'] for row in rows}

async def get_force_channels() -> List[Dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM force_channels WHERE is_active = 1')
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

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

# --- FastAPI App & Utilities ---

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
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
