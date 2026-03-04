import os
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "12345678").split(",")]
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./quiz.db")

# --- Database Setup ---
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- Models ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255))
    balance = Column(Float, default=0.0)
    referral_code = Column(String(50), unique=True, index=True)
    referred_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    referral_count = Column(Integer, default=0)
    total_quiz_played = Column(Integer, default=0)
    is_banned = Column(Boolean, default=False)
    joined_at = Column(DateTime, default=datetime.utcnow)

class Quiz(Base):
    __tablename__ = "quizzes"
    id = Column(Integer, primary_key=True, index=True)
    question = Column(Text)
    option1 = Column(String(255))
    option2 = Column(String(255))
    option3 = Column(String(255))
    option4 = Column(String(255))
    correct_option = Column(Integer) # 1, 2, 3, or 4
    reward = Column(Float, default=1.0)
    timer = Column(Integer, default=15)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class UserAnswer(Base):
    __tablename__ = "user_answers"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    quiz_id = Column(Integer, ForeignKey('quizzes.id'))
    answered_at = Column(DateTime, default=datetime.utcnow)
    # Unique constraint could be added to prevent re-answering, 
    # but logic handles it.

class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    method = Column(String(50))
    number = Column(String(50))
    amount = Column(Float)
    fee = Column(Float, default=0.0)
    status = Column(String(20), default='pending') # pending, approved, rejected
    requested_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(50), primary_key=True)
    value = Column(Text)

class ForceJoinChannel(Base):
    __tablename__ = "force_channels"
    id = Column(Integer, primary_key=True)
    channel_id = Column(String(50))
    channel_name = Column(String(100))

# Create tables
Base.metadata.create_all(bind=engine)

# --- Pydantic Schemas ---
class AnswerSubmit(BaseModel):
    quiz_id: int
    answer: int

class WithdrawRequest(BaseModel):
    method: str
    number: str
    amount: float

class QuizCreate(BaseModel):
    question: str
    option1: str
    option2: str
    option3: str
    option4: str
    correct_option: int
    reward: float = 1.0

class UserBalanceUpdate(BaseModel):
    amount: float

class UserBanUpdate(BaseModel):
    banned: bool

class WithdrawProcess(BaseModel):
    status: str

class SettingsUpdate(BaseModel):
    min_withdraw: Optional[str] = None
    withdraw_fee: Optional[str] = None
    referral_bonus: Optional[str] = None
    quiz_reward: Optional[str] = None
    quiz_timer: Optional[str] = None
    quiz_enabled: Optional[str] = None
    withdraw_enabled: Optional[str] = None
    ads_enabled: Optional[str] = None

# --- Utility Functions ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def verify_telegram_auth(init_data: str) -> Dict:
    """Validates Telegram WebApp init data."""
    try:
        # Parse the query string
        vals = {x.split('=')[0]: x.split('=')[1] for x in init_data.split('&')}
        hash_ = vals.pop('hash', None)
        
        if not hash_:
            return None

        # Create data check string
        data_check_items = [f"{k}={v}" for k, v in sorted(vals.items())]
        data_check_string = '\n'.join(data_check_items)

        # Calculate secret key
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()

        # Calculate hash
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if calculated_hash == hash_:
            # Check auth_date (optional security, prevent replay attacks)
            auth_date = int(vals.get('auth_date', 0))
            if time.time() - auth_date > 86400: # 24 hours
                return None
            
            # Parse user JSON
            user_data = json.loads(vals.get('user', '{}'))
            return user_data
        return None
    except Exception as e:
        print(f"Auth Error: {e}")
        return None

def get_current_user(init_data: str = Header(None, alias="X-Telegram-Init-Data"), db: Session = Depends(get_db)):
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing init data")
    
    user_data = verify_telegram_auth(init_data)
    if not user_data:
        raise HTTPException(status_code=401, detail="Invalid auth data")
    
    tg_id = user_data.get('id')
    
    # Check if user exists
    user = db.query(User).filter(User.id == tg_id).first()
    if not user:
        # Auto-register user
        ref_code = str(uuid.uuid4()).split('-')[0]
        user = User(
            id=tg_id,
            name=user_data.get('first_name', 'User'),
            referral_code=ref_code
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    
    return user

def get_setting(db: Session, key: str, default: str = "0"):
    setting = db.query(Setting).filter(Setting.key == key).first()
    return setting.value if setting else default

def set_setting(db: Session, key: str, value: str):
    setting = db.query(Setting).filter(Setting.key == key).first()
    if setting:
        setting.value = value
    else:
        setting = Setting(key=key, value=value)
        db.add(setting)
    db.commit()

# --- App Initialization ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic (initialize default settings)
    db = SessionLocal()
    default_settings = {
        "min_withdraw": "10",
        "withdraw_fee": "0",
        "referral_bonus": "5",
        "quiz_reward": "1",
        "quiz_timer": "15",
        "quiz_enabled": "1",
        "withdraw_enabled": "1",
        "ads_enabled": "1"
    }
    for k, v in default_settings.items():
        if not db.query(Setting).filter(Setting.key == k).first():
            db.add(Setting(key=k, value=v))
    db.commit()
    db.close()
    yield

app = FastAPI(title="Quiz & Earn API", lifespan=lifespan)

# CORS Middleware for local testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- API Routes ---

@app.get("/")
async def root():
    return {"status": "running", "message": "Quiz Earn API is live"}

# --- User Routes ---

@app.get("/api/me")
async def get_me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    is_admin = user.id in ADMIN_IDS
    
    # Check force join (skipping actual API check for simplicity in this demo, 
    # assuming frontend handles channel join logic or using mock)
    # In production, you would use Bot API to check ChatMember status.
    
    return {
        "success": True,
        "user": {
            "id": user.id,
            "name": user.name,
            "balance": user.balance,
            "referral_count": user.referral_count,
            "total_quiz_played": user.total_quiz_played,
            "is_banned": user.is_banned
        },
        "is_admin": is_admin
    }

@app.get("/api/profile")
async def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    base_url = "https://t.me/YourBotUsername" # Replace with actual logic or env var
    referral_link = f"{base_url}?start={user.referral_code}"
    
    # Leaderboard top 10
    leaders = db.query(User).order_by(User.balance.desc()).limit(10).all()
    
    return {
        "success": True,
        "referral_link": referral_link,
        "leaderboard": [{
            "name": l.name,
            "balance": l.balance,
            "total_quiz_played": l.total_quiz_played
        } for l in leaders]
    }

# --- Quiz Routes ---

@app.get("/api/quiz/next")
async def get_next_quiz(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.is_banned:
        raise HTTPException(status_code=403, detail="Banned")
    
    # Find a quiz the user hasn't answered
    answered_ids = db.query(UserAnswer.quiz_id).filter(UserAnswer.user_id == user.id).all()
    answered_ids = [a[0] for a in answered_ids]
    
    quiz = db.query(Quiz).filter(
        Quiz.is_active == True, 
        Quiz.id.notin_(answered_ids)
    ).first()
    
    if not quiz:
        return {"success": False, "message": "No quizzes available"}
    
    settings_timer = int(get_setting(db, "quiz_timer", "15"))
    
    return {
        "success": True,
        "quiz": {
            "id": quiz.id,
            "question": quiz.question,
            "options": [quiz.option1, quiz.option2, quiz.option3, quiz.option4],
            "reward": float(get_setting(db, "quiz_reward", str(quiz.reward))),
            "timer": settings_timer
        }
    }

@app.post("/api/quiz/answer")
async def submit_answer(
    data: AnswerSubmit, 
    user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    if user.is_banned:
        raise HTTPException(status_code=403, detail="Banned")
    
    # Check if already answered
    exists = db.query(UserAnswer).filter(
        UserAnswer.user_id == user.id, 
        UserAnswer.quiz_id == data.quiz_id
    ).first()
    if exists:
        raise HTTPException(status_code=400, detail="Already answered")
    
    quiz = db.query(Quiz).filter(Quiz.id == data.quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    
    is_correct = data.answer == quiz.correct_option
    reward_amount = 0.0
    
    if is_correct:
        reward_amount = float(get_setting(db, "quiz_reward", str(quiz.reward)))
        user.balance += reward_amount
    
    # Log answer
    answer_log = UserAnswer(user_id=user.id, quiz_id=quiz.id)
    user.total_quiz_played += 1
    
    db.add(answer_log)
    db.commit()
    db.refresh(user)
    
    return {
        "success": True,
        "is_correct": is_correct,
        "correct_option": quiz.correct_option,
        "reward": reward_amount,
        "new_balance": user.balance
    }

# --- Withdrawal Routes ---

@app.get("/api/settings")
async def get_settings(db: Session = Depends(get_db)):
    s = {
        "min_withdraw": get_setting(db, "min_withdraw"),
        "withdraw_fee": get_setting(db, "withdraw_fee"),
        "referral_bonus": get_setting(db, "referral_bonus"),
        "quiz_reward": get_setting(db, "quiz_reward"),
        "quiz_timer": get_setting(db, "quiz_timer"),
        "quiz_enabled": get_setting(db, "quiz_enabled"),
        "withdraw_enabled": get_setting(db, "withdraw_enabled"),
        "ads_enabled": get_setting(db, "ads_enabled")
    }
    return {"success": True, "settings": s}

@app.post("/api/withdraw")
async def create_withdraw(
    data: WithdrawRequest, 
    user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    if get_setting(db, "withdraw_enabled") == "0":
        raise HTTPException(status_code=400, detail="Withdrawals are disabled")
    
    min_w = float(get_setting(db, "min_withdraw", "10"))
    fee = float(get_setting(db, "withdraw_fee", "0"))
    
    if data.amount < min_w:
        raise HTTPException(status_code=400, detail=f"Minimum is {min_w}")
    
    total_deduction = data.amount + fee
    if user.balance < total_deduction:
        raise HTTPException(status_code=400, detail="Insufficient balance")
    
    user.balance -= total_deduction
    
    w = Withdrawal(
        user_id=user.id,
        method=data.method,
        number=data.number,
        amount=data.amount,
        fee=fee
    )
    db.add(w)
    db.commit()
    
    return {
        "success": True, 
        "new_balance": user.balance,
        "message": "Withdrawal request submitted"
    }

# --- Admin Routes ---

def admin_required(user: User = Depends(get_current_user)):
    if user.id not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

@app.get("/api/admin/stats")
async def admin_stats(admin: User = Depends(admin_required), db: Session = Depends(get_db)):
    total_users = db.query(User).count()
    total_dist = db.query(User).with_entities(User.balance).all()
    total_dist_sum = sum([u[0] for u in total_dist])
    
    pending_w = db.query(Withdrawal).filter(Withdrawal.status == 'pending').count()
    pending_amt = db.query(Withdrawal).filter(Withdrawal.status == 'pending').with_entities(Withdrawal.amount).all()
    pending_amt_sum = sum([w[0] for w in pending_amt])
    
    return {
        "success": True,
        "stats": {
            "total_users": total_users,
            "total_balance_distributed": total_dist_sum,
            "pending_withdraw_count": pending_w,
            "total_withdraw_pending": pending_amt_sum
        }
    }

@app.get("/api/admin/users")
async def admin_users(admin: User = Depends(admin_required), db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.joined_at.desc()).limit(50).all()
    return {
        "success": True,
        "users": [{
            "id": u.id,
            "name": u.name,
            "balance": u.balance,
            "is_banned": u.is_banned,
            "referral_count": u.referral_count,
            "total_quiz_played": u.total_quiz_played
        } for u in users]
    }

@app.post("/api/admin/user/{user_id}/balance")
async def admin_update_balance(
    user_id: int, 
    data: UserBalanceUpdate, 
    admin: User = Depends(admin_required), 
    db: Session = Depends(get_db)
):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Simple math: allow +100 or -50 etc.
    u.balance += data.amount
    if u.balance < 0: u.balance = 0
    db.commit()
    return {"success": True, "new_balance": u.balance}

@app.post("/api/admin/user/{user_id}/ban")
async def admin_ban_user(
    user_id: int, 
    data: UserBanUpdate, 
    admin: User = Depends(admin_required), 
    db: Session = Depends(get_db)
):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    
    u.is_banned = data.banned
    db.commit()
    return {"success": True}

@app.get("/api/admin/quizzes")
async def admin_get_quizzes(admin: User = Depends(admin_required), db: Session = Depends(get_db)):
    quizzes = db.query(Quiz).order_by(Quiz.created_at.desc()).limit(20).all()
    return {
        "success": True,
        "quizzes": [{
            "id": q.id,
            "question": q.question,
            "reward": q.reward,
            "is_active": q.is_active
        } for q in quizzes]
    }

@app.post("/api/admin/quiz")
async def admin_create_quiz(
    data: QuizCreate, 
    admin: User = Depends(admin_required), 
    db: Session = Depends(get_db)
):
    q = Quiz(
        question=data.question,
        option1=data.option1,
        option2=data.option2,
        option3=data.option3,
        option4=data.option4,
        correct_option=data.correct_option,
        reward=data.reward
    )
    db.add(q)
    db.commit()
    return {"success": True, "id": q.id}

@app.delete("/api/admin/quiz/{quiz_id}")
async def admin_delete_quiz(quiz_id: int, admin: User = Depends(admin_required), db: Session = Depends(get_db)):
    q = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(q)
    db.commit()
    return {"success": True}

@app.get("/api/admin/withdraws")
async def admin_get_withdraws(admin: User = Depends(admin_required), db: Session = Depends(get_db)):
    wdrs = db.query(Withdrawal).order_by(Withdrawal.requested_at.desc()).limit(20).all()
    return {
        "success": True,
        "withdraws": [{
            "id": w.id,
            "user_id": w.user_id,
            "method": w.method,
            "number": w.number,
            "amount": w.amount,
            "fee": w.fee,
            "status": w.status,
            "requested_at": w.requested_at
        } for w in wdrs]
    }

@app.post("/api/admin/withdraw/{w_id}/process")
async def admin_process_withdraw(
    w_id: int, 
    data: WithdrawProcess, 
    admin: User = Depends(admin_required), 
    db: Session = Depends(get_db)
):
    w = db.query(Withdrawal).filter(Withdrawal.id == w_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Not found")
    if w.status != 'pending':
        raise HTTPException(status_code=400, detail="Already processed")
    
    w.status = data.status
    w.processed_at = datetime.utcnow()
    
    # If rejected, refund balance
    if data.status == 'rejected':
        user = db.query(User).filter(User.id == w.user_id).first()
        if user:
            user.balance += (w.amount + w.fee)
    
    db.commit()
    return {"success": True}

@app.post("/api/settings")
async def admin_save_settings(
    data: SettingsUpdate, 
    admin: User = Depends(admin_required), 
    db: Session = Depends(get_db)
):
    if data.min_withdraw is not None: set_setting(db, "min_withdraw", data.min_withdraw)
    if data.withdraw_fee is not None: set_setting(db, "withdraw_fee", data.withdraw_fee)
    if data.referral_bonus is not None: set_setting(db, "referral_bonus", data.referral_bonus)
    if data.quiz_reward is not None: set_setting(db, "quiz_reward", data.quiz_reward)
    if data.quiz_timer is not None: set_setting(db, "quiz_timer", data.quiz_timer)
    if data.quiz_enabled is not None: set_setting(db, "quiz_enabled", data.quiz_enabled)
    if data.withdraw_enabled is not None: set_setting(db, "withdraw_enabled", data.withdraw_enabled)
    if data.ads_enabled is not None: set_setting(db, "ads_enabled", data.ads_enabled)
    
    return {"success": True}

# --- Referral Helper (Triggered on new user creation in get_current_user) ---
# Ideally, we need to handle the start parameter. 
# We'll add a specific endpoint for referral check or handle inside get_me.

@app.get("/api/referral/{code}")
async def check_referral(code: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # This is hypothetical; usually referral is handled on bot start, not webapp.
    # But we can implement logic if needed:
    if user.referred_by is None:
        referrer = db.query(User).filter(User.referral_code == code).first()
        if referrer and referrer.id != user.id:
            user.referred_by = referrer.id
            referrer.referral_count += 1
            
            # Bonus
            bonus = float(get_setting(db, "referral_bonus", "5"))
            referrer.balance += bonus
            # Optionally give user bonus too
            # user.balance += bonus 
            
            db.commit()
            return {"success": True, "message": "Referral applied!"}
    
    return {"success": False, "message": "Could not apply referral"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
