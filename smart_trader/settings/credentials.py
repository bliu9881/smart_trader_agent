"""
Secure credential loading from .env file.
Never hardcode API keys or secrets.
"""
import os
from pathlib import Path
from dotenv import load_dotenv


def load_credentials() -> dict:
    """Load credentials from .env file."""
    env_path = Path(__file__).parent.parent.parent / ".env"
    load_dotenv(env_path)

    return {
        "ibkr_host": os.getenv("IBKR_HOST", "127.0.0.1"),
        "ibkr_port": int(os.getenv("IBKR_PORT", "7497")),
        "ibkr_client_id": int(os.getenv("IBKR_CLIENT_ID", "1")),
        "ibkr_account": os.getenv("IBKR_ACCOUNT", ""),
        "email_password": os.getenv("EMAIL_PASSWORD", ""),
        "supabase_url": os.getenv("SUPABASE_URL", ""),
        "supabase_key": os.getenv("SUPABASE_KEY", ""),
        # Live-trading opt-in. Paper is always the default. Live trading
        # requires BOTH: allow_live_trading truthy AND the connected account
        # matching live_account_id — two independent gates so a single
        # stray flag can never put real money at risk by accident.
        "allow_live_trading": os.getenv("ALLOW_LIVE_TRADING", "").strip().lower()
        in ("1", "true", "yes", "on"),
        "live_account_id": os.getenv("LIVE_ACCOUNT_ID", "").strip(),
    }
