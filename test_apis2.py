import os
import time
from dotenv import load_dotenv

load_dotenv()

# Test Bybit API with time_sync=True
from pybit.unified_trading import HTTP
try:
    session = HTTP(
        testnet=False,
        api_key=os.environ.get("BYBIT_API_KEY"),
        api_secret=os.environ.get("BYBIT_API_SECRET"),
        recv_window=20000,
    )
    res = session.get_wallet_balance(accountType="UNIFIED")
    balance = res['result']['list'][0]['totalEquity']
    print(f"Bybit API OK! Saldo: {balance} USDT")
except Exception as e:
    print(f"Bybit API Error: {e}")
