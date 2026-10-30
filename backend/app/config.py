import os


class Settings:
    database_url = os.getenv("DATABASE_URL", "sqlite:///./apollocare.db")
    jwt_secret = os.getenv("JWT_SECRET", "dev-only-change-me")
    jwt_minutes = int(os.getenv("JWT_MINUTES", "480"))
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    ai_timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "20"))
    log_file = os.getenv("LOG_FILE", "logs/app.log")
    log_level = os.getenv("LOG_LEVEL", "INFO")


# Hosts like Render/Railway hand out postgres:// URLs; SQLAlchemy + psycopg3 needs this prefix.
for _old in ("postgres://", "postgresql://"):
    if settings_url := Settings.database_url:
        if settings_url.startswith(_old):
            Settings.database_url = "postgresql+psycopg://" + settings_url[len(_old):]

settings = Settings()
