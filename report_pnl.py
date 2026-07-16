import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'src')))

from corretoras.funcoes_bybit import busca_pnl

def report():
    print("Buscando PnL dos ultimos 3 dias...")
    for subconta in [1, 2, 3, 4, 5]:
        print(f"--- Subconta {subconta} ---")
        try:
            df = busca_pnl(subconta, dias=3)
            if not df.empty:
                print(df)
                total_pnl = df['pnl'].sum()
                print(f"Total Trades: {len(df)}")
                print(f"Total PnL: {total_pnl:.2f} USDT")
            else:
                print("Nenhum trade finalizado.")
        except Exception as e:
            print(f"Erro: {e}")

if __name__ == "__main__":
    report()
