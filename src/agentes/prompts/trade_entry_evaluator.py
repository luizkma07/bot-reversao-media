from textwrap import dedent
from .market_context import format_market_context
from .trades_pnl import format_trades_pnl
from .sentiment_context import format_sentiment_context
from datetime import datetime


def prompt_trade_entry_evaluator(
    saldo,
    tempo_grafico,
    rsi_periodo,
    rsi_sobrevenda,
    rsi_sobrecompra,
    bb_periodo,
    bb_desvio_padrao,
    adx_periodo,
    adx_limite_maximo,
    rsi_atual,
    bb_superior_atual,
    bb_media_atual,
    bb_inferior_atual,
    adx_atual,
    cripto,
    qtd_min_para_operar,
    subconta,
    lado,
    df,
    df_1w,
    df_1d,
    df_4h,
):
    df = df.reset_index()
    df_1w = df_1w.reset_index()
    df_1d = df_1d.reset_index()
    df_4h = df_4h.reset_index()

    preco_atual = df['fechamento'].iloc[-1]
    desvio_da_media_pct = abs(preco_atual - bb_media_atual) / bb_media_atual * 100

    if lado.lower() == "compra":
        direcao = "Compra (Long — Reversão de Sobrevenda)"
        status_rsi = f"SOBREVENDIDO (<= {rsi_sobrevenda})"
        zona_extrema = f"Banda Inferior: {bb_inferior_atual:.5f}"
        alvo_natural = f"BB_MEDIA (SMA {bb_periodo}): {bb_media_atual:.5f}"
        detalhes = (
            f"Vela anterior fechou abaixo/na Banda Inferior de Bollinger e o RSI atingiu "
            f"zona de sobrevenda no timeframe de {tempo_grafico} minutos. "
            f"Vela atual confirmou retorno (fechou acima da anterior)."
        )
    else:
        direcao = "Venda (Short — Reversão de Sobrecompra)"
        status_rsi = f"SOBRECOMPRADO (>= {rsi_sobrecompra})"
        zona_extrema = f"Banda Superior: {bb_superior_atual:.5f}"
        alvo_natural = f"BB_MEDIA (SMA {bb_periodo}): {bb_media_atual:.5f}"
        detalhes = (
            f"Vela anterior fechou acima/na Banda Superior de Bollinger e o RSI atingiu "
            f"zona de sobrecompra no timeframe de {tempo_grafico} minutos. "
            f"Vela atual confirmou retorno (fechou abaixo da anterior)."
        )

    return dedent(f"""
Sinal de entrada de REVERSÃO À MÉDIA identificado.

# Estratégia: Mean Reversion (Bot de Reversão à Média)
# Valor da carteira: {saldo} USDT

# Detalhes do sinal:
- Hora: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- Timeframe operacional: {tempo_grafico} minutos (1H)
- Símbolo: {cripto}
- Direção: {direcao}
- Subconta: {subconta}
- Casas decimais (qty_step): {qtd_min_para_operar}

# Indicadores de Mean Reversion:
- RSI ({rsi_periodo} períodos): {rsi_atual:.2f} → {status_rsi}
- Bollinger Bands ({bb_periodo}p, {bb_desvio_padrao}σ):
    * Banda Superior: {bb_superior_atual:.5f}
    * Banda Média / ALVO NATURAL: {bb_media_atual:.5f}
    * {zona_extrema}
- Distância do preço à BB_MEDIA: {desvio_da_media_pct:.2f}% (tensão do elástico)
- ADX ({adx_periodo} períodos): {adx_atual:.2f} → {'SEM TENDÊNCIA ✅' if adx_atual < adx_limite_maximo else '⚠️ TENDÊNCIA DETECTADA'}

# Detalhes do sinal:
{detalhes}

# Alvo natural desta estratégia:
{alvo_natural} — ponto de retorno à média

{format_market_context(tempo_grafico, df, df_1w, df_1d, df_4h)}

{format_sentiment_context(cripto, tempo_grafico)}

{format_trades_pnl(subconta)}
""")