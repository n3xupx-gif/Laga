import aiosqlite
import json
from datetime import datetime
from typing import Optional, List, Dict, Any

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

# --- User Operations ---

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

async def ban_user(user_id: int, is_banned: bool = True) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            'UPDATE users SET is_banned = ? WHERE id = ?',
            (1 if is_banned else 0, user_id)
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

# --- Quiz Operations ---

async def get_quiz(quiz_id: int = None) -> Optional[Dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if quiz_id:
            cursor = await db.execute('SELECT * FROM quiz WHERE id = ? AND is_active = 1', (quiz_id,))
        else:
            cursor = await db.execute('SELECT * FROM quiz WHERE is_active = 1 ORDER BY RANDOM() LIMIT 1')
        row = await cursor.fetchone()
        return dict(row) if row else None

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

# --- Withdraw Operations ---

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

# --- Settings Operations ---

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

# --- Force Channels Operations ---

async def get_force_channels() -> List[Dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM force_channels WHERE is_active = 1')
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def add_force_channel(channel_id: str, channel_name: str = None, channel_link: str = None) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('''
            INSERT INTO force_channels (channel_id, channel_name, channel_link, is_active)
            VALUES (?, ?, ?, 1)
        ''', (channel_id, channel_name, channel_link))
        await db.commit()
        return True

async def remove_force_channel(channel_id: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute('DELETE FROM force_channels WHERE channel_id = ?', (channel_id,))
        await db.commit()
        return True