import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Essential: Set these in Render Environment Variables
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS: list[int] = [123456789]  # Replace with your actual Telegram ID
    
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