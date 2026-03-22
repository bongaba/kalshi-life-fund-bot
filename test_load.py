# test_load.py
from pathlib import Path
from dotenv import load_dotenv
import os

# Force-load from the exact folder where this script lives
project_root = Path(__file__).parent.resolve()
env_path = project_root / '.env'

print(f"Looking for .env at: {env_path}")
print(f"File exists? {env_path.exists()}")

loaded = load_dotenv(env_path, override=True)
print(f"load_dotenv() returned: {loaded}")

# Test reading a variable
print(f"KALSHI_EMAIL from env: {os.getenv('KALSHI_EMAIL', 'NOT_FOUND')}")
print(f"XAI_API_KEY from env: {os.getenv('XAI_API_KEY', 'NOT_FOUND')}")
print(f"ACCOUNT_MODE from env: {os.getenv('ACCOUNT_MODE', 'NOT_FOUND')}")