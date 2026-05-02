import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import time
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from entidades.estado_trade import EstadoDeTrade
from entidades.lado_operacao import LadoOperacao
from entidades.risco_operacao import RiscoOperacao
from corretoras.funcoes_bybit import busca_velas, tem_trade_aberto, saldo_da_conta, quantidade_minima_para_operar
if __name__ == '__main__':
    from agent_execution_with_parser import executar_trade_conductor_se_necessario
else:
    from live_trading.agent_execution_with_parser import executar_trade_conductor_se_necessario
from agentes.trade_entry_evaluator import trade_entry_evaluator
from agentes.prompts.trade_entry_evaluator import prompt_trade_entry_evaluator
from agentes.parsers.trade_entry_evaluator_parser import TradeEntryEvaluatorParser
from managers.data_manager import prepare_multi_timeframe_technical_data, prepare_market_data
from utils.utilidades import calcular_risco_retorno_compra, calcular_risco_retorno_venda
from utils.logging import get_logger, LogCategory

MODULE_NAME = Path(__file__).stem

# ═══════════════════════════════════════════════════════════════════
# PARÂMETROS DO BOT — MEAN REVERSION
# ═══════════════════════════════════════════════════════════════════
subconta = 2                          # [MR] Subconta dedicada (atualizar quando criar na Bybit)
cripto = 'XRPUSDT'                    # [MR] Ativo alvo
tempo_grafico = '60'                  # [MR] 1H — mais confiável para Mean Reversion
frequencia_agente_horas = 4
executar_agente_no_start = False
lado_operacao = LadoOperacao.AMBOS    # [MR] Opera compra E venda (toque em ambas as bandas)

# [MR] Risco conservador para validação inicial da estratégia
risco_por_operacao = RiscoOperacao.BAIXO   # 1%

# [MR] Parâmetros RSI
rsi_periodo = 14
rsi_sobrevenda = 35     # Limite inferior (mais realista que 30 para cripto)
rsi_sobrecompra = 65    # Limite superior (mais realista que 70 para cripto)

# [MR] Parâmetros Bandas de Bollinger
bb_periodo = 20         # SMA 20 = média natural e alvo das operações
bb_desvio_padrao = 2.0  # 2σ = cobre ~95% do range normal

# [MR] Parâmetros ADX — Filtro Mestre de Ausência de Tendência
adx_periodo = 14
adx_limite_maximo = 25  # ADX < 25 = sem tendência = ambiente ideal para MR

# Constantes do Circuit Breaker (herdadas intactas)
MAX_STOPS_CONSECUTIVOS = 3
PAUSA_CIRCUIT_BREAKER_HORAS = 2


# ═══════════════════════════════════════════════════════════════════
# FUNÇÕES DE CÁLCULO — INDICADORES DE MEAN REVERSION
# ═══════════════════════════════════════════════════════════════════

def calcular_rsi(df, periodo=14):
    """
    Calcula o RSI via suavização de Wilder (EWM).
    RSI <= 35: sobrevendido — potencial entrada de compra (MR)
    RSI >= 65: sobrecomprado — potencial entrada de venda (MR)
    """
    delta = df['fechamento'].diff()
    ganho = delta.where(delta > 0, 0.0)
    perda = -delta.where(delta < 0, 0.0)
    media_ganho = ganho.ewm(alpha=1 / periodo, adjust=False).mean()
    media_perda = perda.ewm(alpha=1 / periodo, adjust=False).mean()
    rs = media_ganho / media_perda
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calcular_bandas_bollinger(df, periodo=20, desvio=2.0):
    """
    Calcula as Bandas de Bollinger.
    A Banda do Meio (SMA periodo) é o ALVO NATURAL de todas as operações MR.
    Retorna: (banda_superior, banda_media, banda_inferior)
    """
    sma = df['fechamento'].rolling(window=periodo).mean()
    std = df['fechamento'].rolling(window=periodo).std()
    banda_superior = sma + (desvio * std)
    banda_inferior = sma - (desvio * std)
    return banda_superior, sma, banda_inferior


def calcular_adx(df, periodo=14):
    """
    Calcula o ADX (Average Directional Index) via suavização de Wilder.
    ADX < 25: mercado sem tendência = ambiente IDEAL para Mean Reversion.
    ADX >= 25: tendência estabelecida = EVITAR entradas MR.
    """
    high = df['maxima']
    low = df['minima']
    close = df['fechamento']
    prev_close = close.shift(1)

    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move

    atr_w = tr.ewm(alpha=1 / periodo, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / periodo, adjust=False).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=1 / periodo, adjust=False).mean() / atr_w

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    adx = dx.ewm(alpha=1 / periodo, adjust=False).mean()
    return adx


def mercado_sem_tendencia(df, periodo=14, limite=25, logger=None, symbol='', module=''):
    """
    Filtro Mestre de Mean Reversion.
    Retorna True se ADX < limite (sem tendência = ambiente de range favorável).
    Retorna False se ADX >= limite (tendência estabelecida = bloquear entrada MR).

    É o inverso do mercado_tem_volatilidade_suficiente() do Bot Agressivo:
    o Bot Agressivo QUER volatilidade; o Bot MR EVITA tendência.
    """
    try:
        adx = calcular_adx(df, periodo)
        adx_atual = adx.iloc[-1]
        sem_tendencia = adx_atual < limite

        if logger:
            if sem_tendencia:
                logger.info(LogCategory.TRADE_SEARCH,
                    f"✅ Mercado em Range — ADX: {adx_atual:.2f} < {limite}. Ambiente MR favorável.",
                    module, symbol=symbol)
            else:
                logger.info(LogCategory.TRADE_SEARCH,
                    f"⏸️ Tendência detectada — ADX: {adx_atual:.2f} >= {limite}. Bloqueando entrada MR.",
                    module, symbol=symbol)
        return sem_tendencia
    except Exception:
        return True


def start_live_trading_bot(
    subconta=subconta,
    cripto=cripto,
    tempo_grafico=tempo_grafico,
    lado_operacao=lado_operacao,
    frequencia_agente_horas=frequencia_agente_horas,
    executar_agente_no_start=executar_agente_no_start,
    risco_por_operacao=risco_por_operacao,
    bot_id=None,
    stop_flag=None,
):
    if bot_id is None:
        bot_id = f"{datetime.now().timestamp():.0f}"

    compras_habilitadas = lado_operacao in [LadoOperacao.AMBOS, LadoOperacao.APENAS_COMPRA]
    vendas_habilitadas = lado_operacao in [LadoOperacao.AMBOS, LadoOperacao.APENAS_VENDA]

    logger = get_logger(bot_id)

    logger.info(LogCategory.BOT_START, "?? Bot de REVERS�O � M�DIA iniciado", MODULE_NAME,
        subconta=subconta, symbol=cripto, tempo_grafico=tempo_grafico,
        lado_operacao=lado_operacao.value, risco_por_operacao=risco_por_operacao.value,
        rsi_periodo=rsi_periodo, rsi_sobrevenda=rsi_sobrevenda, rsi_sobrecompra=rsi_sobrecompra,
        bb_periodo=bb_periodo, bb_desvio_padrao=bb_desvio_padrao,
        adx_periodo=adx_periodo, adx_limite_maximo=adx_limite_maximo)

    for tentativa in range(5):
        try:
            estado_de_trade, preco_entrada, preco_stop, preco_alvo, tamanho_posicao, trailing_stop = tem_trade_aberto(cripto, subconta)
            qtd_min_para_operar = quantidade_minima_para_operar(cripto, subconta)

            if estado_de_trade in [EstadoDeTrade.COMPRADO, EstadoDeTrade.VENDIDO]:
                if estado_de_trade == EstadoDeTrade.COMPRADO and preco_stop < preco_entrada:
                    risco_retorno = calcular_risco_retorno_compra(preco_entrada, preco_stop, preco_alvo)
                elif estado_de_trade == EstadoDeTrade.VENDIDO and preco_stop > preco_entrada:
                    risco_retorno = calcular_risco_retorno_venda(preco_entrada, preco_stop, preco_alvo)
                else:
                    risco_retorno = None

                emoji = "??" if estado_de_trade == EstadoDeTrade.COMPRADO else "??"
                logger.trading(LogCategory.POSITION_STATUS, f"{emoji} Posi��o {estado_de_trade.value} ativa", MODULE_NAME,
                    symbol=cripto, estado_de_trade=estado_de_trade, preco_entrada=preco_entrada,
                    preco_stop=preco_stop, preco_alvo=preco_alvo, tamanho_posicao=tamanho_posicao,
                    trailing_stop=trailing_stop, risco_retorno=risco_retorno)
            else:
                logger.info(LogCategory.POSITION_STATUS, "?? Sem posi��o aberta � buscando setup MR", MODULE_NAME,
                    symbol=cripto, estado_de_trade=estado_de_trade.value)
            break

        except Exception as e:
            logger.error(LogCategory.TRADE_STATUS_ERROR, "Erro ao buscar trade aberto", MODULE_NAME,
                symbol=cripto, tentativa=tentativa+1, erro_message=str(e), exception=e)
            if tentativa < 4:
                time.sleep(2)
            elif tentativa == 4:
                logger.critical(LogCategory.FATAL_ERROR, "N�o foi poss�vel buscar trade aberto. Encerrando.", MODULE_NAME,
                    symbol=cripto, total_tentativas=5, erro_message=str(e))
                exit()

    vela_abertura_trade = None
    vela_fechou_trade = None
    vela_executou_trade_entry_evaluator = None
    ultima_execucao_trade_conductor = None
    stops_consecutivos = 0
    bloqueio_ate = 0

    if estado_de_trade in [EstadoDeTrade.COMPRADO, EstadoDeTrade.VENDIDO]:
        if not executar_agente_no_start:
            ultima_execucao_trade_conductor = datetime.now()
            next_execution = ultima_execucao_trade_conductor + timedelta(hours=frequencia_agente_horas)
            logger.info(LogCategory.AGENT_SCHEDULE, "Agente condutor n�o ser� executado no start", MODULE_NAME,
                symbol=cripto, proxima_execucao=next_execution.strftime("%Y-%m-%d %H:%M:%S"))
    else:
        logger.info(LogCategory.TRADE_SEARCH, "?? Monitorando bandas de Bollinger para setup MR", MODULE_NAME,
            subconta=subconta, symbol=cripto, tempo_grafico=tempo_grafico)

    while True:
        if stop_flag and stop_flag.is_set():
            logger.info(LogCategory.BOT_STOP, "?? Bot recebeu sinal de parada da API", MODULE_NAME,
                symbol=cripto, bot_id=bot_id)
            break

        if time.time() < bloqueio_ate:
            restante_segundos = int(bloqueio_ate - time.time())
            minutos, segundos = divmod(restante_segundos, 60)
            horas, minutos = divmod(minutos, 60)
            logger.warning(LogCategory.TRADE_SEARCH,
                f"?? CIRCUIT BREAKER ATIVO. Restam {horas:02d}h{minutos:02d}m{segundos:02d}s.",
                MODULE_NAME, symbol=cripto)
            time.sleep(30)
            continue

        try:
            # ---------------------------------------------------------
            # DATAFEED � uma �nica chamada base para o timeframe 1H
            # EMAs 9/21 fornecidas para contexto do LLM via market_context
            # ---------------------------------------------------------
            df = busca_velas(cripto, tempo_grafico, [9, 21])

            if df.empty:
                logger.warning(LogCategory.EMPTY_DATA, "DataFrame vazio", MODULE_NAME,
                    symbol=cripto, tempo_grafico=tempo_grafico)
            else:
                if estado_de_trade == EstadoDeTrade.COMPRADO:
                    estado_de_trade, preco_entrada, preco_stop, preco_alvo, tamanho_posicao, trailing_stop = tem_trade_aberto(cripto, subconta)

                    ultima_execucao_trade_conductor = executar_trade_conductor_se_necessario(
                        ultima_execucao_trade_conductor, frequencia_agente_horas, df, cripto, subconta,
                        tempo_grafico, estado_de_trade, preco_entrada, preco_alvo, preco_stop,
                        tamanho_posicao, qtd_min_para_operar, trailing_stop, vela_abertura_trade, logger)

                    if df['maxima'].iloc[-1] >= preco_alvo and preco_alvo != 0:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.TARGET_HIT, "?? Alvo (BB_Media) atingido", MODULE_NAME,
                            symbol=cripto, preco_alvo=preco_alvo, preco_atual=df['maxima'].iloc[-1])
                        stops_consecutivos = 0

                    elif df['minima'].iloc[-1] <= preco_stop:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.STOP_HIT, "Stop loss atingido", MODULE_NAME,
                            symbol=cripto, preco_stop=preco_stop, preco_atual=df['minima'].iloc[-1])
                        stops_consecutivos += 1
                        logger.warning(LogCategory.TRADE_STATUS_ERROR,
                            f"?? Stop Consecutivo {stops_consecutivos}/{MAX_STOPS_CONSECUTIVOS}", MODULE_NAME, symbol=cripto)
                        if stops_consecutivos >= MAX_STOPS_CONSECUTIVOS:
                            bloqueio_ate = time.time() + (PAUSA_CIRCUIT_BREAKER_HORAS * 3600)
                            stops_consecutivos = 0
                            logger.critical(LogCategory.FATAL_ERROR,
                                f"?? Circuit Breaker ativado at� {datetime.fromtimestamp(bloqueio_ate).strftime('%Y-%m-%d %H:%M:%S')}.",
                                MODULE_NAME, symbol=cripto)

                    elif estado_de_trade == EstadoDeTrade.DE_FORA:
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.MANUAL_CLOSE, "Trade fechado manualmente na corretora", MODULE_NAME, symbol=cripto)

                elif estado_de_trade == EstadoDeTrade.VENDIDO:
                    estado_de_trade, preco_entrada, preco_stop, preco_alvo, tamanho_posicao, trailing_stop = tem_trade_aberto(cripto, subconta)

                    ultima_execucao_trade_conductor = executar_trade_conductor_se_necessario(
                        ultima_execucao_trade_conductor, frequencia_agente_horas, df, cripto, subconta,
                        tempo_grafico, estado_de_trade, preco_entrada, preco_alvo, preco_stop,
                        tamanho_posicao, qtd_min_para_operar, trailing_stop, vela_abertura_trade, logger)

                    if df['minima'].iloc[-1] <= preco_alvo:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.TARGET_HIT, "?? Alvo (BB_Media) atingido", MODULE_NAME,
                            symbol=cripto, preco_alvo=preco_alvo, preco_atual=df['minima'].iloc[-1])
                        stops_consecutivos = 0

                    elif df['maxima'].iloc[-1] >= preco_stop and preco_stop != 0:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.STOP_HIT, "Stop loss atingido", MODULE_NAME,
                            symbol=cripto, preco_stop=preco_stop, preco_atual=df['maxima'].iloc[-1])
                        stops_consecutivos += 1
                        logger.warning(LogCategory.TRADE_STATUS_ERROR,
                            f"?? Stop Consecutivo {stops_consecutivos}/{MAX_STOPS_CONSECUTIVOS}", MODULE_NAME, symbol=cripto)
                        if stops_consecutivos >= MAX_STOPS_CONSECUTIVOS:
                            bloqueio_ate = time.time() + (PAUSA_CIRCUIT_BREAKER_HORAS * 3600)
                            stops_consecutivos = 0
                            logger.critical(LogCategory.FATAL_ERROR,
                                f"?? Circuit Breaker ativado at� {datetime.fromtimestamp(bloqueio_ate).strftime('%Y-%m-%d %H:%M:%S')}.",
                                MODULE_NAME, symbol=cripto)

                    elif estado_de_trade == EstadoDeTrade.DE_FORA:
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.MANUAL_CLOSE, "Trade fechado manualmente", MODULE_NAME, symbol=cripto)

                elif estado_de_trade == EstadoDeTrade.DE_FORA and df.index[-1] != vela_fechou_trade:
                    # -------------------------------------------------------
                    # DETEC��O DE SINAL � MEAN REVERSION
                    # Calcula RSI, Bollinger e ADX sobre o df base (1H)
                    # Enriquece o df com as colunas para o LLM ver no contexto
                    # -------------------------------------------------------
                    rsi = calcular_rsi(df, rsi_periodo)
                    bb_superior, bb_media, bb_inferior = calcular_bandas_bollinger(df, bb_periodo, bb_desvio_padrao)
                    adx = calcular_adx(df, adx_periodo)

                    # Adiciona indicadores ao df para enriquecer contexto do LLM automaticamente
                    df['RSI'] = rsi.round(2)
                    df['BB_SUP'] = bb_superior.round(5)
                    df['BB_MED'] = bb_media.round(5)
                    df['BB_INF'] = bb_inferior.round(5)
                    df['ADX'] = adx.round(2)

                    if compras_habilitadas:
                        # ---------------------------------------------
                        # SINAL DE COMPRA (Long MR):
                        # 1) Vela anterior fechou abaixo/na BB Inferior
                        # 2) RSI anterior sobrevendido (<= 35)
                        # 3) Vela atual confirmou retorno (fechou acima)
                        # ---------------------------------------------
                        cond_bb_compra = df['fechamento'].iloc[-2] <= bb_inferior.iloc[-2]
                        cond_rsi_compra = rsi.iloc[-2] <= rsi_sobrevenda
                        cond_retorno_compra = df['fechamento'].iloc[-1] > df['fechamento'].iloc[-2]
                        sinal_compra = cond_bb_compra and cond_rsi_compra and cond_retorno_compra

                        if sinal_compra:
                            if not mercado_sem_tendencia(df, adx_periodo, adx_limite_maximo, logger, cripto, MODULE_NAME):
                                pass  # Bloqueado por ADX � log j� feito dentro da fun��o
                            else:
                                estado_de_trade, _, _, _, _, _ = tem_trade_aberto(cripto, subconta)
                                if estado_de_trade == EstadoDeTrade.DE_FORA:
                                    if df.index[-1] != vela_executou_trade_entry_evaluator:
                                        # --- UMA �NICA CHAMADA MULTI-TF ---------------
                                        df_1w, df_1d, df = prepare_multi_timeframe_technical_data(df, cripto)

                                        logger.agent(LogCategory.AGENT_EXECUTION,
                                            "?? Sinal MR de COMPRA � Iniciando an�lise Entry Evaluator", MODULE_NAME,
                                            agent_name="Entry Evaluator MR", symbol=cripto,
                                            rsi_vela_sinal=round(rsi.iloc[-2], 2),
                                            bb_inferior=round(bb_inferior.iloc[-2], 5),
                                            adx_atual=round(adx.iloc[-1], 2))

                                        vela_executou_trade_entry_evaluator = df.index[-1]
                                        saldo = saldo_da_conta(subconta)
                                        df_4h = busca_velas(cripto, '240', [9, 21])
                                        df_4h = prepare_market_data(df_4h, use_emas=True, emas_periods=[200], use_peaks=True, peaks_distance=21)

                                        resposta = trade_entry_evaluator.run(prompt_trade_entry_evaluator(
                                            saldo, tempo_grafico,
                                            rsi_periodo, rsi_sobrevenda, rsi_sobrecompra,
                                            bb_periodo, bb_desvio_padrao,
                                            adx_periodo, adx_limite_maximo,
                                            rsi.iloc[-1], bb_superior.iloc[-1], bb_media.iloc[-1], bb_inferior.iloc[-1],
                                            adx.iloc[-1],
                                            cripto, qtd_min_para_operar, subconta, 'compra',
                                            df, df_1w, df_1d, df_4h
                                        ))

                                        logger.agent(LogCategory.AGENT_RESPONSE, "Resposta Entry Evaluator recebida", MODULE_NAME,
                                            agent_name="Entry Evaluator MR", symbol=cripto,
                                            response_length=len(resposta.content), response_content=resposta.content)

                                        abriu_trade = TradeEntryEvaluatorParser.processar_resposta(
                                            resposta, cripto, subconta, tempo_grafico, risco_por_operacao.value, logger)

                                        if abriu_trade:
                                            vela_abertura_trade = df.index[-1]
                                            ultima_execucao_trade_conductor = datetime.now()
                                            next_execution = (ultima_execucao_trade_conductor + timedelta(hours=frequencia_agente_horas)).strftime('%Y-%m-%d %H:%M:%S')
                                            logger.agent(LogCategory.AGENT_SCHEDULE, "Condutor MR programado", MODULE_NAME,
                                                agent_name="Trade Conductor MR", symbol=cripto, proxima_execucao=next_execution)
                                        else:
                                            logger.info(LogCategory.TRADE_SEARCH, "?? Aguardando pr�ximo setup MR", MODULE_NAME,
                                                subconta=subconta, symbol=cripto)

                    if vendas_habilitadas:
                        # ---------------------------------------------
                        # SINAL DE VENDA (Short MR):
                        # 1) Vela anterior fechou acima/na BB Superior
                        # 2) RSI anterior sobrecomprado (>= 65)
                        # 3) Vela atual confirmou retorno (fechou abaixo)
                        # ---------------------------------------------
                        cond_bb_venda = df['fechamento'].iloc[-2] >= bb_superior.iloc[-2]
                        cond_rsi_venda = rsi.iloc[-2] >= rsi_sobrecompra
                        cond_retorno_venda = df['fechamento'].iloc[-1] < df['fechamento'].iloc[-2]
                        sinal_venda = cond_bb_venda and cond_rsi_venda and cond_retorno_venda

                        if sinal_venda:
                            if not mercado_sem_tendencia(df, adx_periodo, adx_limite_maximo, logger, cripto, MODULE_NAME):
                                pass  # Bloqueado por ADX � log j� feito dentro da fun��o
                            else:
                                estado_de_trade, _, _, _, _, _ = tem_trade_aberto(cripto, subconta)
                                if estado_de_trade == EstadoDeTrade.DE_FORA:
                                    if df.index[-1] != vela_executou_trade_entry_evaluator:
                                        # --- UMA �NICA CHAMADA MULTI-TF ---------------
                                        df_1w, df_1d, df = prepare_multi_timeframe_technical_data(df, cripto)

                                        logger.agent(LogCategory.AGENT_EXECUTION,
                                            "?? Sinal MR de VENDA � Iniciando an�lise Entry Evaluator", MODULE_NAME,
                                            agent_name="Entry Evaluator MR", symbol=cripto,
                                            rsi_vela_sinal=round(rsi.iloc[-2], 2),
                                            bb_superior=round(bb_superior.iloc[-2], 5),
                                            adx_atual=round(adx.iloc[-1], 2))

                                        vela_executou_trade_entry_evaluator = df.index[-1]
                                        saldo = saldo_da_conta(subconta)
                                        df_4h = busca_velas(cripto, '240', [9, 21])
                                        df_4h = prepare_market_data(df_4h, use_emas=True, emas_periods=[200], use_peaks=True, peaks_distance=21)

                                        resposta = trade_entry_evaluator.run(prompt_trade_entry_evaluator(
                                            saldo, tempo_grafico,
                                            rsi_periodo, rsi_sobrevenda, rsi_sobrecompra,
                                            bb_periodo, bb_desvio_padrao,
                                            adx_periodo, adx_limite_maximo,
                                            rsi.iloc[-1], bb_superior.iloc[-1], bb_media.iloc[-1], bb_inferior.iloc[-1],
                                            adx.iloc[-1],
                                            cripto, qtd_min_para_operar, subconta, 'venda',
                                            df, df_1w, df_1d, df_4h
                                        ))

                                        logger.agent(LogCategory.AGENT_RESPONSE, "Resposta Entry Evaluator recebida", MODULE_NAME,
                                            agent_name="Entry Evaluator MR", symbol=cripto,
                                            response_length=len(resposta.content), response_content=resposta.content)

                                        abriu_trade = TradeEntryEvaluatorParser.processar_resposta(
                                            resposta, cripto, subconta, tempo_grafico, risco_por_operacao.value, logger)

                                        if abriu_trade:
                                            vela_abertura_trade = df.index[-1]
                                            ultima_execucao_trade_conductor = datetime.now()
                                            next_execution = (ultima_execucao_trade_conductor + timedelta(hours=frequencia_agente_horas)).strftime('%Y-%m-%d %H:%M:%S')
                                            logger.agent(LogCategory.AGENT_SCHEDULE, "Condutor MR programado", MODULE_NAME,
                                                agent_name="Trade Conductor MR", symbol=cripto, proxima_execucao=next_execution)
                                        else:
                                            logger.info(LogCategory.TRADE_SEARCH, "?? Aguardando pr�ximo setup MR", MODULE_NAME,
                                                subconta=subconta, symbol=cripto)

        # -------------------------------------------------------------------
        # TRATAMENTO DE ERROS � HERDADO INTEGRALMENTE DO BOT AGRESSIVO
        # -------------------------------------------------------------------
        except Exception as e:
            erro_str = str(e).lower()

            if 'ratelimit' in erro_str or 'rate limit' in erro_str or '429' in erro_str or 'too many requests' in erro_str or '10006' in erro_str or 'x-bapi-limit' in erro_str:
                logger.warning(LogCategory.CONNECTION_ERROR,
                    "? Rate Limit atingido � Pausando 15 segundos.",
                    MODULE_NAME, symbol=cripto, erro_message=str(e))
                time.sleep(15)
                continue

            elif 'network' in erro_str or 'connection' in erro_str or 'timeout' in erro_str or 'connectionerror' in erro_str:
                logger.error(LogCategory.CONNECTION_ERROR,
                    "?? Erro de rede. Aguardando 10s...",
                    MODULE_NAME, symbol=cripto, erro_message=str(e))
                time.sleep(10)
                continue

            elif 'exchange not available' in erro_str or 'maintenance' in erro_str or '503' in erro_str:
                logger.error(LogCategory.CONNECTION_ERROR,
                    "?? Exchange indispon�vel. Aguardando 60s...",
                    MODULE_NAME, symbol=cripto, erro_message=str(e))
                time.sleep(60)
                continue

            elif 'insufficient' in erro_str or 'balance' in erro_str:
                logger.critical(LogCategory.FATAL_ERROR,
                    "?? Saldo insuficiente. Bot aguardando 5 minutos.",
                    MODULE_NAME, symbol=cripto, erro_message=str(e))
                time.sleep(300)
                continue

            elif isinstance(e, ConnectionError):
                logger.error(LogCategory.CONNECTION_ERROR, "Erro de conex�o", MODULE_NAME,
                    symbol=cripto, erro_message=str(e), exception=e)

            elif isinstance(e, ValueError):
                logger.error(LogCategory.VALUE_ERROR, "Erro de valor", MODULE_NAME,
                    symbol=cripto, erro_message=str(e), exception=e)

            elif isinstance(e, KeyboardInterrupt):
                logger.info(LogCategory.SHUTDOWN, "Programa encerrado pelo usu�rio", MODULE_NAME, symbol=cripto)
                exit()

            else:
                logger.error(LogCategory.UNKNOWN_ERROR, "Erro desconhecido", MODULE_NAME,
                    symbol=cripto, erro_message=str(e), exception=e)

        time.sleep(30)  # 1H timeframe: 30s entre ciclos � suficiente e evita RateLimit

    logger.info(LogCategory.BOT_STOP, "Bot MR encerrado pela API", MODULE_NAME,
        symbol=cripto, bot_id=bot_id)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Bot de Revers�o � M�dia')
    parser.add_argument('--subconta', type=int, default=subconta)
    parser.add_argument('--cripto', type=str, default=cripto)
    parser.add_argument('--tempo_grafico', type=str, default=tempo_grafico)
    parser.add_argument('--lado_operacao', type=str, default=lado_operacao.value,
        choices=[lado.value for lado in LadoOperacao])
    parser.add_argument('--risco_por_operacao', type=float, default=risco_por_operacao.value,
        choices=[risco.value for risco in RiscoOperacao])
    parser.add_argument('--frequencia_agente_horas', type=float, default=frequencia_agente_horas)
    parser.add_argument('--executar_agente_no_start', type=bool, default=executar_agente_no_start)
    # Par�metros MR
    parser.add_argument('--rsi_periodo', type=int, default=rsi_periodo)
    parser.add_argument('--rsi_sobrevenda', type=float, default=rsi_sobrevenda)
    parser.add_argument('--rsi_sobrecompra', type=float, default=rsi_sobrecompra)
    parser.add_argument('--bb_periodo', type=int, default=bb_periodo)
    parser.add_argument('--bb_desvio_padrao', type=float, default=bb_desvio_padrao)
    parser.add_argument('--adx_periodo', type=int, default=adx_periodo)
    parser.add_argument('--adx_limite_maximo', type=float, default=adx_limite_maximo)

    args = parser.parse_args()
    start_live_trading_bot(
        subconta=args.subconta,
        cripto=args.cripto,
        tempo_grafico=args.tempo_grafico,
        lado_operacao=LadoOperacao(args.lado_operacao),
        risco_por_operacao=RiscoOperacao(args.risco_por_operacao),
        frequencia_agente_horas=args.frequencia_agente_horas,
        executar_agente_no_start=args.executar_agente_no_start,
    )
