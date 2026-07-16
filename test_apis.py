import os
from dotenv import load_dotenv

load_dotenv()

# Test Bybit API
from pybit.unified_trading import HTTP
try:
    session = HTTP(
        testnet=False,
        api_key=os.environ.get("BYBIT_API_KEY"),
        api_secret=os.environ.get("BYBIT_API_SECRET"),
    )
    res = session.get_wallet_balance(accountType="UNIFIED")
    balance = res['result']['list'][0]['totalEquity']
    print(f"Bybit API OK! Saldo: {balance} USDT")
except Exception as e:
    print(f"Bybit API Error: {e}")

# Test Google Gemini API
import google.generativeai as genai
try:
    genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))
    model = genai.GenerativeModel('gemini-1.5-flash')
    response = model.generate_content("Responda com 'API Google OK'")
    print(f"Google API OK! Resposta: {response.text.strip()}")
except Exception as e:
    print(f"Google API Error: {e}")
