import sys
import os
import pandas as pd
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'src')))

from corretoras.funcoes_bybit import carregar_dados_historicos
from datetime import datetime, timedelta

def calcular_rsi(df, periodo=14):
    delta = df['fechamento'].diff()
    ganho = delta.where(delta > 0, 0.0)
    perda = -delta.where(delta < 0, 0.0)
    media_ganho = ganho.ewm(alpha=1/periodo, adjust=False).mean()
    media_perda = perda.ewm(alpha=1/periodo, adjust=False).mean()
    rs = media_ganho / (media_perda + 1e-10)
    return 100 - (100 / (1 + rs))

def calcular_bandas_bollinger(df, periodo=20, desvio=2.0):
    sma = df['fechamento'].rolling(window=periodo).mean()
    std = df['fechamento'].rolling(window=periodo).std()
    return sma + (desvio * std), sma, sma - (desvio * std)

def calcular_adx(df, periodo=14):
    high = df['maxima']
    low = df['minima']
    close = df['fechamento']
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move
    atr_w = tr.ewm(alpha=1/periodo, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/periodo, adjust=False).mean() / (atr_w + 1e-10)
    minus_di = 100 * minus_dm.ewm(alpha=1/periodo, adjust=False).mean() / (atr_w + 1e-10)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    adx = dx.ewm(alpha=1/periodo, adjust=False).mean()
    return adx, plus_di, minus_di

def simular_3_dias():
    end_date = datetime.now()
    start_date = end_date - timedelta(days=3)
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')
    
    # carrega ultimos 3 dias + margem pra SMA
    df = carregar_dados_historicos('XRPUSDT', '30', [9, 21], start_str, end_str, pular_velas=100)
    
    df['rsi'] = calcular_rsi(df, 14)
    df['bb_sup'], df['bb_med'], df['bb_inf'] = calcular_bandas_bollinger(df, 20, 2.0)
    df['adx'], df['pdi'], df['mdi'] = calcular_adx(df, 14)
    
    # Parametros rigorosos atuais
    rsi_sob_v = 35
    rsi_sob_c = 65
    adx_lim_rigor = 30
    
    # Parametros frouxos
    rsi_sob_v_frouxo = 40
    rsi_sob_c_frouxo = 60
    adx_lim_frouxo = 40
    
    gatilhos_rigorosos = 0
    gatilhos_frouxos = 0
    
    for i in range(2, len(df)):
        # vela atual: i, anterior: i-1, penultima: i-2
        
        # Rigoroso Compra
        cond_bb_c = df['fechamento'].iloc[i-2] <= df['bb_inf'].iloc[i-2]
        cond_rsi_c = df['rsi'].iloc[i-2] <= rsi_sob_v
        cond_ret_c = df['fechamento'].iloc[i-1] > df['fechamento'].iloc[i-2]
        if cond_bb_c and cond_rsi_c and cond_ret_c:
            if df['adx'].iloc[i-1] < adx_lim_rigor:
                gatilhos_rigorosos += 1
                
        # Rigoroso Venda
        cond_bb_v = df['fechamento'].iloc[i-2] >= df['bb_sup'].iloc[i-2]
        cond_rsi_v = df['rsi'].iloc[i-2] >= rsi_sob_c
        cond_ret_v = df['fechamento'].iloc[i-1] < df['fechamento'].iloc[i-2]
        if cond_bb_v and cond_rsi_v and cond_ret_v:
            if df['adx'].iloc[i-1] < adx_lim_rigor:
                gatilhos_rigorosos += 1
                
        # Frouxo Compra
        cond_rsi_c_f = df['rsi'].iloc[i-2] <= rsi_sob_v_frouxo
        if cond_bb_c and cond_rsi_c_f and cond_ret_c:
            if df['adx'].iloc[i-1] < adx_lim_frouxo:
                gatilhos_frouxos += 1
                
        # Frouxo Venda
        cond_rsi_v_f = df['rsi'].iloc[i-2] >= rsi_sob_c_frouxo
        if cond_bb_v and cond_rsi_v_f and cond_ret_v:
            if df['adx'].iloc[i-1] < adx_lim_frouxo:
                gatilhos_frouxos += 1
                
    print(f"Gatilhos com configs Atuais (ADX<30, RSI 35/65): {gatilhos_rigorosos}")
    print(f"Gatilhos com configs Frouxas (ADX<40, RSI 40/60): {gatilhos_frouxos}")

if __name__ == "__main__":
    simular_3_dias()
