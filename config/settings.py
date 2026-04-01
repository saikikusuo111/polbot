import os
from dotenv import load_dotenv

load_dotenv()

def _get_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}

def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return default

def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default

PAPER_MODE = _get_bool("PAPER_MODE", True)

SCAN_INTERVAL_SEC = _get_int("SCAN_INTERVAL_SEC", 20)

MIN_PRICE = _get_float("MIN_PRICE", 0.001)
MAX_PRICE = _get_float("MAX_PRICE", 0.03)
MIN_LIQUIDITY = _get_float("MIN_LIQUIDITY", 50.0)
MIN_MINUTES_TO_END = _get_int("MIN_MINUTES_TO_END", 30)

PAPER_START_BALANCE = _get_float("PAPER_START_BALANCE", 100.0)
PAPER_BET_SIZE_USD = _get_float("PAPER_BET_SIZE_USD", 1.0)
MAX_OPEN_PAPER_POSITIONS = _get_int("MAX_OPEN_PAPER_POSITIONS", 200)

DATA_DIR = os.getenv("DATA_DIR", "/root/anomaly_hunter/data")

# future live
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
RPC_URL = os.getenv("RPC_URL", "")