import sys
import os
import json
import time
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

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
CB_STATE_FILE = Path(__file__).parent.parent.parent / "circuit_breaker_state.json"

subconta = 1
cripto = 'XRPUSDT'
tempo_grafico = '60'
frequencia_agente_horas = 4
executar_agente_no_start = False
lado_operacao = LadoOperacao.AMBOS
risco_por_operacao = RiscoOperacao.BAIXO

rsi_periodo = 14
rsi_sobrevenda = 35
rsi_sobrecompra = 65
bb_periodo = 20
bb_desvio_padrao = 2.0
adx_periodo = 14
adx_limite_maximo = 25
di_limite_dominancia = 30
volume_media_periodo = 20
volume_multiplicador_max = 2.0
MAX_STOPS_CONSECUTIVOS = 3
PAUSA_CIRCUIT_BREAKER_HORAS = 2


def salvar_estado_cb(stops_consecutivos, bloqueio_ate):
    try:
        estado = {"stops_consecutivos": stops_consecutivos, "bloqueio_ate": bloqueio_ate, "atualizado_em": datetime.now().isoformat()}
        CB_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CB_STATE_FILE, "w") as f:
            json.dump(estado, f, indent=2)
    except Exception:
        pass


def carregar_estado_cb():
    try:
        if CB_STATE_FILE.exists():
            with open(CB_STATE_FILE, "r") as f:
                estado = json.load(f)
            return int(estado.get("stops_consecutivos", 0)), float(estado.get("bloqueio_ate", 0))
    except Exception:
        pass
    return 0, 0.0


def calcular_rsi(df, periodo=14):
    delta = df['fechamento'].diff()
    ganho = delta.where(delta > 0, 0.0)
    perda = -delta.where(delta < 0, 0.0)
    media_ganho = ganho.ewm(alpha=1 / periodo, adjust=False).mean()
    media_perda = perda.ewm(alpha=1 / periodo, adjust=False).mean()
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
    atr_w = tr.ewm(alpha=1 / periodo, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / periodo, adjust=False).mean() / (atr_w + 1e-10)
    minus_di = 100 * minus_dm.ewm(alpha=1 / periodo, adjust=False).mean() / (atr_w + 1e-10)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    adx = dx.ewm(alpha=1 / periodo, adjust=False).mean()
    return adx, plus_di, minus_di


def volume_nao_e_anomalia(df, periodo=20, multiplicador_max=2.0, logger=None, symbol='', module=''):
    try:
        if 'volume' not in df.columns:
            return True
        media_volume = df['volume'].rolling(window=periodo).mean().iloc[-2]
        volume_sinal = df['volume'].iloc[-2]
        limite = media_volume * multiplicador_max
        eh_normal = volume_sinal <= limite
        if logger:
            status = "OK" if eh_normal else "ANOMALO"
            logger.info(
                LogCategory.TRADE_SEARCH,
                f"Volume {status}: {volume_sinal:.0f} / limite {limite:.0f}",
                module,
                symbol=symbol
            )
        return eh_normal
    except Exception:
        return True


def mercado_ok_para_entrada(df, lado, adx_s, plus_di_s, minus_di_s, adx_limite=25, di_limite=30, logger=None, symbol='', module=''):
    try:
        adx_v = adx_s.iloc[-1]
        pdi_v = plus_di_s.iloc[-1]
        mdi_v = minus_di_s.iloc[-1]
        if adx_v >= adx_limite:
            if logger:
                logger.info(LogCategory.TRADE_SEARCH, f"Tendencia ADX:{adx_v:.2f}>={adx_limite}. Bloqueando MR.", module, symbol=symbol)
            return False
        if lado.lower() == 'compra' and mdi_v > di_limite:
            if logger:
                logger.warning(LogCategory.TRADE_SEARCH, f"Pressao baixista -DI:{mdi_v:.2f}>{di_limite}. Bloqueando Long.", module, symbol=symbol)
            return False
        if lado.lower() == 'venda' and pdi_v > di_limite:
            if logger:
                logger.warning(LogCategory.TRADE_SEARCH, f"Pressao altista +DI:{pdi_v:.2f}>{di_limite}. Bloqueando Short.", module, symbol=symbol)
            return False
        if logger:
            logger.info(LogCategory.TRADE_SEARCH, f"Range OK ADX:{adx_v:.2f} +DI:{pdi_v:.2f} -DI:{mdi_v:.2f}", module, symbol=symbol)
        return True
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

    logger.info(LogCategory.BOT_START, "Bot Mean Reversion iniciado (v2 - auditado)", MODULE_NAME,
        subconta=subconta, symbol=cripto, tempo_grafico=tempo_grafico,
        rsi_sobrevenda=rsi_sobrevenda, rsi_sobrecompra=rsi_sobrecompra,
        adx_limite=adx_limite_maximo, di_limite=di_limite_dominancia)

    for tentativa in range(5):
        try:
            estado_de_trade, preco_entrada, preco_stop, preco_alvo, tamanho_posicao, trailing_stop = tem_trade_aberto(cripto, subconta)
            qtd_min_para_operar = quantidade_minima_para_operar(cripto, subconta)
            break
        except Exception as e:
            logger.error(LogCategory.TRADE_STATUS_ERROR, "Erro ao buscar trade aberto", MODULE_NAME,
                symbol=cripto, tentativa=tentativa+1, erro_message=str(e), exception=e)
            if tentativa < 4:
                time.sleep(2)
            elif tentativa == 4:
                logger.critical(LogCategory.FATAL_ERROR, "Nao foi possivel buscar trade. Encerrando.", MODULE_NAME, symbol=cripto)
                exit()

    # FIX #3: carrega estado do CB do disco ao iniciar
    stops_consecutivos, bloqueio_ate = carregar_estado_cb()
    if bloqueio_ate > time.time():
        logger.warning(LogCategory.TRADE_SEARCH, "Circuit Breaker ativo restaurado do disco.", MODULE_NAME, symbol=cripto)

    vela_abertura_trade = None
    vela_fechou_trade = None
    vela_executou_trade_entry_evaluator = None
    ultima_execucao_trade_conductor = None

    if estado_de_trade in [EstadoDeTrade.COMPRADO, EstadoDeTrade.VENDIDO]:
        if not executar_agente_no_start:
            ultima_execucao_trade_conductor = datetime.now()

    while True:
        if stop_flag and stop_flag.is_set():
            logger.info(LogCategory.BOT_STOP, "Bot recebeu sinal de parada", MODULE_NAME, symbol=cripto, bot_id=bot_id)
            break

        if time.time() < bloqueio_ate:
            restante = int(bloqueio_ate - time.time())
            h, m = divmod(restante // 60, 60)
            logger.warning(LogCategory.TRADE_SEARCH, f"CIRCUIT BREAKER ATIVO. Restam {h:02d}h{m:02d}m.", MODULE_NAME, symbol=cripto)
            time.sleep(30)
            continue

        try:
            df = busca_velas(cripto, tempo_grafico, [9, 21])

            if df.empty:
                logger.warning(LogCategory.EMPTY_DATA, "DataFrame vazio", MODULE_NAME, symbol=cripto)
            else:
                # ── GESTAO DE POSICAO ABERTA ────────────────────────────────────────
                if estado_de_trade == EstadoDeTrade.COMPRADO:
                    estado_de_trade, preco_entrada, preco_stop, preco_alvo, tamanho_posicao, trailing_stop = tem_trade_aberto(cripto, subconta)
                    ultima_execucao_trade_conductor = executar_trade_conductor_se_necessario(
                        ultima_execucao_trade_conductor, frequencia_agente_horas, df, cripto, subconta,
                        tempo_grafico, estado_de_trade, preco_entrada, preco_alvo, preco_stop,
                        tamanho_posicao, qtd_min_para_operar, trailing_stop, vela_abertura_trade, logger)

                    if df['maxima'].iloc[-1] >= preco_alvo and preco_alvo != 0:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        stops_consecutivos = 0
                        salvar_estado_cb(stops_consecutivos, bloqueio_ate)
                        logger.trading(LogCategory.TARGET_HIT, "Alvo BB_Media atingido", MODULE_NAME, symbol=cripto)
                    elif df['minima'].iloc[-1] <= preco_stop:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        stops_consecutivos += 1
                        salvar_estado_cb(stops_consecutivos, bloqueio_ate)
                        logger.trading(LogCategory.STOP_HIT, "Stop atingido", MODULE_NAME, symbol=cripto, stop=preco_stop)
                        if stops_consecutivos >= MAX_STOPS_CONSECUTIVOS:
                            bloqueio_ate = time.time() + (PAUSA_CIRCUIT_BREAKER_HORAS * 3600)
                            stops_consecutivos = 0
                            salvar_estado_cb(stops_consecutivos, bloqueio_ate)
                            logger.critical(LogCategory.FATAL_ERROR, "Circuit Breaker ativado e salvo em disco.", MODULE_NAME, symbol=cripto)
                    elif estado_de_trade == EstadoDeTrade.DE_FORA:
                        vela_fechou_trade = df.index[-1]

                elif estado_de_trade == EstadoDeTrade.VENDIDO:
                    estado_de_trade, preco_entrada, preco_stop, preco_alvo, tamanho_posicao, trailing_stop = tem_trade_aberto(cripto, subconta)
                    ultima_execucao_trade_conductor = executar_trade_conductor_se_necessario(
                        ultima_execucao_trade_conductor, frequencia_agente_horas, df, cripto, subconta,
                        tempo_grafico, estado_de_trade, preco_entrada, preco_alvo, preco_stop,
                        tamanho_posicao, qtd_min_para_operar, trailing_stop, vela_abertura_trade, logger)

                    if df['minima'].iloc[-1] <= preco_alvo:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        stops_consecutivos = 0
                        salvar_estado_cb(stops_consecutivos, bloqueio_ate)
                        logger.trading(LogCategory.TARGET_HIT, "Alvo BB_Media atingido", MODULE_NAME, symbol=cripto)
                    elif df['maxima'].iloc[-1] >= preco_stop and preco_stop != 0:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        stops_consecutivos += 1
                        salvar_estado_cb(stops_consecutivos, bloqueio_ate)
                        logger.trading(LogCategory.STOP_HIT, "Stop atingido", MODULE_NAME, symbol=cripto, stop=preco_stop)
                        if stops_consecutivos >= MAX_STOPS_CONSECUTIVOS:
                            bloqueio_ate = time.time() + (PAUSA_CIRCUIT_BREAKER_HORAS * 3600)
                            stops_consecutivos = 0
                            salvar_estado_cb(stops_consecutivos, bloqueio_ate)
                            logger.critical(LogCategory.FATAL_ERROR, "Circuit Breaker ativado e salvo em disco.", MODULE_NAME, symbol=cripto)
                    elif estado_de_trade == EstadoDeTrade.DE_FORA:
                        vela_fechou_trade = df.index[-1]

                # ── BUSCA DE NOVO SETUP ──────────────────────────────────────────────
                elif estado_de_trade == EstadoDeTrade.DE_FORA and df.index[-1] != vela_fechou_trade:

                    # FIX #1: calcula indicadores NO df ORIGINAL (antes do prepare_multi_tf)
                    rsi = calcular_rsi(df, rsi_periodo)
                    bb_superior, bb_media, bb_inferior = calcular_bandas_bollinger(df, bb_periodo, bb_desvio_padrao)
                    adx, plus_di, minus_di = calcular_adx(df, adx_periodo)

                    # FIX #4: Confirmacao real de reversao (vela atual colorida na direcao certa)
                    if compras_habilitadas:
                        cond_bb_compra = df['fechamento'].iloc[-2] <= bb_inferior.iloc[-2]
                        cond_rsi_compra = rsi.iloc[-2] <= rsi_sobrevenda
                        cond_retorno_compra = (
                            df['fechamento'].iloc[-1] > df['fechamento'].iloc[-2] and
                            df['fechamento'].iloc[-1] > df['abertura'].iloc[-1]
                        )
                        sinal_compra = cond_bb_compra and cond_rsi_compra and cond_retorno_compra

                        if sinal_compra:
                            if not mercado_ok_para_entrada(df, 'compra', adx, plus_di, minus_di,
                                                           adx_limite_maximo, di_limite_dominancia, logger, cripto, MODULE_NAME):
                                pass
                            elif not volume_nao_e_anomalia(df, volume_media_periodo, volume_multiplicador_max, logger, cripto, MODULE_NAME):
                                pass
                            else:
                                estado_de_trade, _, _, _, _, _ = tem_trade_aberto(cripto, subconta)
                                if estado_de_trade == EstadoDeTrade.DE_FORA and df.index[-1] != vela_executou_trade_entry_evaluator:
                                    vela_executou_trade_entry_evaluator = df.index[-1]

                                    # FIX #1: enriquece df so APOS confirmacao do gatilho
                                    df_1w, df_1d, df_consolidado = prepare_multi_timeframe_technical_data(df, cripto)

                                    # FIX #1+#2: recalcula indicadores no df consolidado para sincronia com o LLM
                                    rsi_sync = calcular_rsi(df_consolidado, rsi_periodo)
                                    bb_sup_sync, bb_med_sync, bb_inf_sync = calcular_bandas_bollinger(df_consolidado, bb_periodo, bb_desvio_padrao)
                                    adx_sync, _, _ = calcular_adx(df_consolidado, adx_periodo)

                                    # FIX #2: bb_med_sync e o alvo inegociavel passado ao prompt
                                    saldo = saldo_da_conta(subconta)
                                    df_4h = busca_velas(cripto, '240', [9, 21])
                                    df_4h = prepare_market_data(df_4h, use_emas=True, emas_periods=[200], use_peaks=True, peaks_distance=21)

                                    logger.agent(
                                        LogCategory.AGENT_EXECUTION,
                                        "Sinal MR COMPRA confirmado. Iniciando Entry Evaluator.",
                                        MODULE_NAME,
                                        agent_name="Entry Evaluator MR",
                                        symbol=cripto,
                                        rsi=round(rsi_sync.iloc[-1], 2),
                                        bb_inf=round(bb_inf_sync.iloc[-1], 5),
                                        bb_med_alvo=round(bb_med_sync.iloc[-1], 5),
                                        adx=round(adx_sync.iloc[-1], 2)
                                    )

                                    resposta = trade_entry_evaluator.run(
                                        prompt_trade_entry_evaluator(
                                            saldo, tempo_grafico,
                                            rsi_periodo, rsi_sobrevenda, rsi_sobrecompra,
                                            bb_periodo, bb_desvio_padrao,
                                            adx_periodo, adx_limite_maximo,
                                            rsi_sync.iloc[-1], bb_sup_sync.iloc[-1], bb_med_sync.iloc[-1], bb_inf_sync.iloc[-1],
                                            adx_sync.iloc[-1],
                                            cripto, qtd_min_para_operar, subconta, 'compra',
                                            df_consolidado, df_1w, df_1d, df_4h
                                        )
                                    )

                                    logger.agent(
                                        LogCategory.AGENT_RESPONSE,
                                        "Resposta Entry Evaluator",
                                        MODULE_NAME,
                                        agent_name="Entry Evaluator MR",
                                        symbol=cripto,
                                        response_content=resposta.content
                                    )

                                    abriu_trade = TradeEntryEvaluatorParser.processar_resposta(
                                        resposta, cripto, subconta, tempo_grafico, risco_por_operacao.value, logger
                                    )
                                    if abriu_trade:
                                        vela_abertura_trade = df_consolidado.index[-1]
                                        ultima_execucao_trade_conductor = datetime.now()

                    if vendas_habilitadas:
                        cond_bb_venda = df['fechamento'].iloc[-2] >= bb_superior.iloc[-2]
                        cond_rsi_venda = rsi.iloc[-2] >= rsi_sobrecompra
                        # FIX #4: vela atual deve ser vermelha (confirmacao de reversao)
                        cond_retorno_venda = (
                            df['fechamento'].iloc[-1] < df['fechamento'].iloc[-2] and
                            df['fechamento'].iloc[-1] < df['abertura'].iloc[-1]
                        )
                        sinal_venda = cond_bb_venda and cond_rsi_venda and cond_retorno_venda

                        if sinal_venda:
                            if not mercado_ok_para_entrada(df, 'venda', adx, plus_di, minus_di,
                                                           adx_limite_maximo, di_limite_dominancia, logger, cripto, MODULE_NAME):
                                pass
                            elif not volume_nao_e_anomalia(df, volume_media_periodo, volume_multiplicador_max, logger, cripto, MODULE_NAME):
                                pass
                            else:
                                estado_de_trade, _, _, _, _, _ = tem_trade_aberto(cripto, subconta)
                                if estado_de_trade == EstadoDeTrade.DE_FORA and df.index[-1] != vela_executou_trade_entry_evaluator:
                                    vela_executou_trade_entry_evaluator = df.index[-1]

                                    df_1w, df_1d, df_consolidado = prepare_multi_timeframe_technical_data(df, cripto)

                                    rsi_sync = calcular_rsi(df_consolidado, rsi_periodo)
                                    bb_sup_sync, bb_med_sync, bb_inf_sync = calcular_bandas_bollinger(df_consolidado, bb_periodo, bb_desvio_padrao)
                                    adx_sync, _, _ = calcular_adx(df_consolidado, adx_periodo)

                                    saldo = saldo_da_conta(subconta)
                                    df_4h = busca_velas(cripto, '240', [9, 21])
                                    df_4h = prepare_market_data(df_4h, use_emas=True, emas_periods=[200], use_peaks=True, peaks_distance=21)

                                    logger.agent(
                                        LogCategory.AGENT_EXECUTION,
                                        "Sinal MR VENDA confirmado. Iniciando Entry Evaluator.",
                                        MODULE_NAME,
                                        agent_name="Entry Evaluator MR",
                                        symbol=cripto,
                                        rsi=round(rsi_sync.iloc[-1], 2),
                                        bb_sup=round(bb_sup_sync.iloc[-1], 5),
                                        bb_med_alvo=round(bb_med_sync.iloc[-1], 5),
                                        adx=round(adx_sync.iloc[-1], 2)
                                    )

                                    resposta = trade_entry_evaluator.run(
                                        prompt_trade_entry_evaluator(
                                            saldo, tempo_grafico,
                                            rsi_periodo, rsi_sobrevenda, rsi_sobrecompra,
                                            bb_periodo, bb_desvio_padrao,
                                            adx_periodo, adx_limite_maximo,
                                            rsi_sync.iloc[-1], bb_sup_sync.iloc[-1], bb_med_sync.iloc[-1], bb_inf_sync.iloc[-1],
                                            adx_sync.iloc[-1],
                                            cripto, qtd_min_para_operar, subconta, 'venda',
                                            df_consolidado, df_1w, df_1d, df_4h
                                        )
                                    )

                                    logger.agent(
                                        LogCategory.AGENT_RESPONSE,
                                        "Resposta Entry Evaluator",
                                        MODULE_NAME,
                                        agent_name="Entry Evaluator MR",
                                        symbol=cripto,
                                        response_content=resposta.content
                                    )

                                    abriu_trade = TradeEntryEvaluatorParser.processar_resposta(
                                        resposta, cripto, subconta, tempo_grafico, risco_por_operacao.value, logger
                                    )
                                    if abriu_trade:
                                        vela_abertura_trade = df_consolidado.index[-1]
                                        ultima_execucao_trade_conductor = datetime.now()

        except Exception as e:
            erro_str = str(e).lower()
            if any(k in erro_str for k in ['ratelimit', 'rate limit', '429', 'too many requests', '10006', 'x-bapi-limit']):
                logger.warning(LogCategory.CONNECTION_ERROR, "Rate Limit. Pausando 15s.", MODULE_NAME, symbol=cripto)
                time.sleep(15)
                continue
            elif any(k in erro_str for k in ['network', 'connection', 'timeout', 'connectionerror']):
                logger.error(LogCategory.CONNECTION_ERROR, "Erro de rede. Aguardando 10s.", MODULE_NAME, symbol=cripto)
                time.sleep(10)
                continue
            elif any(k in erro_str for k in ['exchange not available', 'maintenance', '503']):
                logger.error(LogCategory.CONNECTION_ERROR, "Exchange indisponivel. Aguardando 60s.", MODULE_NAME, symbol=cripto)
                time.sleep(60)
                continue
            elif any(k in erro_str for k in ['insufficient', 'balance']):
                logger.critical(LogCategory.FATAL_ERROR, "Saldo insuficiente. Aguardando 5min.", MODULE_NAME, symbol=cripto)
                time.sleep(300)
                continue
            elif isinstance(e, KeyboardInterrupt):
                logger.info(LogCategory.SHUTDOWN, "Encerrado pelo usuario.", MODULE_NAME, symbol=cripto)
                exit()
            else:
                logger.error(LogCategory.UNKNOWN_ERROR, "Erro desconhecido", MODULE_NAME, symbol=cripto, erro_message=str(e), exception=e)

        time.sleep(30)

    logger.info(LogCategory.BOT_STOP, "Bot MR encerrado.", MODULE_NAME, symbol=cripto, bot_id=bot_id)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Bot de Reversao a Media')
    parser.add_argument('--subconta', type=int, default=subconta)
    parser.add_argument('--cripto', type=str, default=cripto)
    parser.add_argument('--tempo_grafico', type=str, default=tempo_grafico)
    parser.add_argument('--lado_operacao', type=str, default=lado_operacao.value, choices=[l.value for l in LadoOperacao])
    parser.add_argument('--risco_por_operacao', type=float, default=risco_por_operacao.value, choices=[r.value for r in RiscoOperacao])
    parser.add_argument('--frequencia_agente_horas', type=float, default=frequencia_agente_horas)
    parser.add_argument('--executar_agente_no_start', type=bool, default=executar_agente_no_start)
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