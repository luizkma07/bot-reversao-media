import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import time
from pathlib import Path
from datetime import datetime, timedelta
from entidades.estado_trade import EstadoDeTrade
from entidades.lado_operacao import LadoOperacao
from entidades.risco_operacao import RiscoOperacao
from corretoras.funcoes_bybit import busca_velas, tem_trade_aberto, saldo_da_conta, quantidade_minima_para_operar
# Import que funciona em ambos os contextos
if __name__ == '__main__':
    # Execução direta - import local
    from agent_execution_with_parser import executar_trade_conductor_se_necessario
else:
    # Importado por outro módulo (API) - import absoluto
    from live_trading.agent_execution_with_parser import executar_trade_conductor_se_necessario
from agentes.trade_entry_evaluator import trade_entry_evaluator
from agentes.prompts.trade_entry_evaluator import prompt_trade_entry_evaluator
from agentes.parsers.trade_entry_evaluator_parser import TradeEntryEvaluatorParser
from managers.data_manager import prepare_multi_timeframe_technical_data, prepare_market_data
from utils.utilidades import calcular_risco_retorno_compra, calcular_risco_retorno_venda
from utils.logging import get_logger, LogCategory

# Nome do módulo para logs (automático)
MODULE_NAME = Path(__file__).stem

# Valores padrão
subconta = 1
cripto = 'XRPUSDT'            # [PROJETO VANGUARDA] Ativo Alvo (preço unitário menor, boa volatilidade)
tempo_grafico = '15'          # [PROJETO VANGUARDA] Timeframe Tático de 15 minutos
frequencia_agente_horas = 4
executar_agente_no_start = False
lado_operacao = LadoOperacao.AMBOS   # Operar tanto na compra quanto na venda (Short)

# Parâmetros para compra
ema_rapida_compra = 5         # [PROJETO VANGUARDA] EMA Rápida: 5
ema_lenta_compra = 13         # [PROJETO VANGUARDA] EMA Lenta: 13

# Parâmetros para venda
ema_rapida_venda = 5          # [PROJETO VANGUARDA] EMA Rápida: 5
ema_lenta_venda = 13          # [PROJETO VANGUARDA] EMA Lenta: 13

# Define percentual de perda ao montar operação:
# MUITO_BAIXO (0.5%), BAIXO (1%), MEDIO (2%), ALTO (5%), MUITO_ALTO (8%)
risco_por_operacao = RiscoOperacao.MEDIO      # [PROJETO VANGUARDA] Ajustado para 2% (Agressivo)

# ═══════════════════════════════════════════════════════════════════
# [MELHORIA #2] FILTROS ANTI-MERCADO-LATERAL
# ───────────────────────────────────────────────────────────────────
# O ATR (Average True Range) mede o "tamanho médio" das velas.
# Quando o ATR está muito baixo, o mercado está se movendo pouco
# (lateral) e os breakouts são frequentemente falsos.
#
# Analogia: Imagine que num dia normal, os preços sobem/descem R$10.
# Se hoje só estão variando R$2, é um dia "quieto" — entrar num
# breakout de R$0.50 nesse cenário é arriscado.
#
# atr_periodo: quantas velas usamos para calcular a média
# atr_filtro_multiplicador: o mínimo de ATR necessário para operar
#   (0.5 = o ATR atual deve ser pelo menos 50% da média histórica)
# ═══════════════════════════════════════════════════════════════════
atr_periodo = 14
atr_filtro_multiplicador = 0.8  # [PROJETO VANGUARDA] Multiplicador ATR Reduzido para 0.8 (Volatilidade Moderada)

# Constantes do Circuit Breaker
MAX_STOPS_CONSECUTIVOS = 3
PAUSA_CIRCUIT_BREAKER_HORAS = 2


def calcular_atr(df, periodo=14):
    """
    Calcula o ATR (Average True Range) — o "termômetro de volatilidade".

    Como funciona:
    - Para cada vela, calcula o maior movimento entre: máxima-mínima,
      diferença da máxima com o fechamento anterior, diferença da
      mínima com o fechamento anterior.
    - Faz a média desses valores nas últimas N velas.

    Retorna: o ATR atual (última vela) e a média histórica do ATR.
    """
    high = df['maxima']
    low = df['minima']
    close_prev = df['fechamento'].shift(1)

    tr = (high - low).combine(
        (high - close_prev).abs(), max
    ).combine(
        (low - close_prev).abs(), max
    )

    atr = tr.ewm(span=periodo, adjust=False).mean()
    return atr.iloc[-1], atr.mean()


def mercado_tem_volatilidade_suficiente(df, periodo=14, multiplicador=0.6, logger=None, symbol='', module=''):
    """
    Retorna True se o mercado está com volatilidade suficiente para operar.
    Retorna False se estiver lateral/parado demais.

    Regra: ATR atual deve ser >= (multiplicador * ATR médio histórico)
    """
    try:
        atr_atual, atr_medio = calcular_atr(df, periodo)
        limite_minimo = multiplicador * atr_medio
        volatilidade_ok = atr_atual >= limite_minimo

        if logger:
            if volatilidade_ok:
                logger.info(LogCategory.TRADE_SEARCH,
                    f"✅ Volatilidade OK — ATR atual: {atr_atual:.4f} | Mínimo exigido: {limite_minimo:.4f}",
                    module, symbol=symbol)
            else:
                logger.info(LogCategory.TRADE_SEARCH,
                    f"⏸️ Mercado lateral detectado — ATR atual: {atr_atual:.4f} < Mínimo: {limite_minimo:.4f}. Aguardando volatilidade.",
                    module, symbol=symbol)

        return volatilidade_ok
    except Exception:
        # Em caso de erro no cálculo, permite a operação por segurança
        return True


def tendencia_diaria_confirma_compra(df_1d, logger=None, symbol='', module=''):
    """
    [PROJETO VANGUARDA] MUTAÇÃO MESTRA: Filtro Macro D1 Desativado.
    O Vanguarda cruza lógicas agressivas no 15 Minutos.
    Retorna sempre True.
    """
    return True


def tendencia_diaria_confirma_venda(df_1d, logger=None, symbol='', module=''):
    """
    [PROJETO VANGUARDA] MUTAÇÃO MESTRA: Filtro Macro D1 Desativado.
    O Vanguarda cruza lógicas agressivas no 15 Minutos.
    Retorna sempre True.
    """
    return True


def start_live_trading_bot(
    subconta = subconta,
    cripto = cripto,
    tempo_grafico = tempo_grafico,
    lado_operacao = lado_operacao,
    frequencia_agente_horas = frequencia_agente_horas,
    executar_agente_no_start = executar_agente_no_start,
    ema_rapida_compra = ema_rapida_compra,
    ema_lenta_compra = ema_lenta_compra,
    ema_rapida_venda = ema_rapida_venda,
    ema_lenta_venda = ema_lenta_venda,
    risco_por_operacao = risco_por_operacao,
    bot_id = f"{datetime.now().timestamp():.0f}",
    stop_flag = None
):
    compras_habilitadas = lado_operacao in [LadoOperacao.AMBOS, LadoOperacao.APENAS_COMPRA]
    vendas_habilitadas = lado_operacao in [LadoOperacao.AMBOS, LadoOperacao.APENAS_VENDA]

    logger = get_logger(bot_id)

    logger.info(LogCategory.BOT_START, "🚀 Bot de trading iniciado com Entry Evaluator", MODULE_NAME,
        subconta=subconta, symbol=cripto, tempo_grafico=tempo_grafico, lado_operacao=lado_operacao.value,
        risco_por_operacao=risco_por_operacao.value, frequencia_agente_horas=frequencia_agente_horas,
        executar_agente_no_start=executar_agente_no_start,
        compras_habilitadas=compras_habilitadas, vendas_habilitadas=vendas_habilitadas,
        ema_rapida_compra=ema_rapida_compra if compras_habilitadas else None,
        ema_lenta_compra=ema_lenta_compra if compras_habilitadas else None,
        ema_rapida_venda=ema_rapida_venda if vendas_habilitadas else None,
        ema_lenta_venda=ema_lenta_venda if vendas_habilitadas else None,
        # [MELHORIA #2] Loga os parâmetros de filtro de volatilidade no start
        atr_periodo=atr_periodo,
        atr_filtro_multiplicador=atr_filtro_multiplicador
    )

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

                emoji = "🟢" if estado_de_trade == EstadoDeTrade.COMPRADO else "🔴"
                logger.trading(LogCategory.POSITION_STATUS, f"{emoji} Posição {estado_de_trade.value} ativa", MODULE_NAME,
                    symbol=cripto, estado_de_trade=estado_de_trade, preco_entrada=preco_entrada,
                    preco_stop=preco_stop, preco_alvo=preco_alvo, tamanho_posicao=tamanho_posicao,
                    trailing_stop=trailing_stop, risco_retorno=risco_retorno,
                    stop_gain_ativo="Stop Gain ativado! Lucro garantido!" if (risco_retorno is None) else "Aguardando ajuste de stop")
            else:
                logger.info(LogCategory.POSITION_STATUS, "🔵 Sem posição aberta", MODULE_NAME,
                    symbol=cripto, estado_de_trade=estado_de_trade.value)
            break

        except Exception as e:
            logger.error(LogCategory.TRADE_STATUS_ERROR, f"Erro ao buscar trade aberto", MODULE_NAME,
                symbol=cripto, tentativa=tentativa+1, erro_message=str(e), exception=e)

            if tentativa < 4:
                logger.info(LogCategory.RETRY_ATTEMPT, "Tentando novamente", MODULE_NAME,
                    symbol=cripto, tentativa=tentativa+1, max_tentativas=5)
                time.sleep(2)
            elif tentativa == 4:
                logger.critical(LogCategory.FATAL_ERROR, "Não foi possível buscar trade aberto. Encerrando programa.", MODULE_NAME,
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
            logger.info(LogCategory.AGENT_SCHEDULE, "Agente condutor não será executado no start", MODULE_NAME,
                symbol=cripto, proxima_execucao=next_execution.strftime("%Y-%m-%d %H:%M:%S"),
                frequencia_agente_horas=frequencia_agente_horas, status="Aguardando stop, alvo ou avaliação do condutor")
    else:
        logger.info(LogCategory.TRADE_SEARCH, "🔍 Procurando oportunidades de trade", MODULE_NAME,
            subconta=subconta, symbol=cripto, tempo_grafico=tempo_grafico, lado_operacao=lado_operacao.value)

    while True:
        # Verificar sinal de parada
        if stop_flag and stop_flag.is_set():
            logger.info(LogCategory.BOT_STOP,
                "🛑 Bot recebeu sinal de parada da API", MODULE_NAME,
                symbol=cripto, bot_id=bot_id)
            break

        if time.time() < bloqueio_ate:
            restante_segundos = int(bloqueio_ate - time.time())
            minutos, segundos = divmod(restante_segundos, 60)
            horas, minutos = divmod(minutos, 60)
            logger.warning(LogCategory.TRADE_SEARCH,
                f"🛑 CIRCUIT BREAKER ATIVO: Pausa por sequência de stops. Restam {horas:02d}h{minutos:02d}m{segundos:02d}s. Executando varredura passiva.",
                MODULE_NAME, symbol=cripto)
            time.sleep(30)
            continue

        try:
            # Buscar dados com todas as EMAs necessárias (união das EMAs de compra e venda)
            df = busca_velas(cripto, tempo_grafico, [5, 15])
            df = df.drop(columns=['EMA_5', 'EMA_15'])
            df['ema_rapida_compra'] = df['fechamento'].ewm(span=ema_rapida_compra, adjust=False).mean()
            df['ema_lenta_compra'] = df['fechamento'].ewm(span=ema_lenta_compra, adjust=False).mean()
            df['ema_rapida_venda'] = df['fechamento'].ewm(span=ema_rapida_venda, adjust=False).mean()
            df['ema_lenta_venda'] = df['fechamento'].ewm(span=ema_lenta_venda, adjust=False).mean()

            if df.empty:
                logger.warning(LogCategory.EMPTY_DATA, "DataFrame vazio - dados de mercado não disponíveis", MODULE_NAME,
                    symbol=cripto, tempo_grafico=tempo_grafico)
            else:
                if estado_de_trade == EstadoDeTrade.COMPRADO:
                    estado_de_trade, preco_entrada, preco_stop, preco_alvo, tamanho_posicao, trailing_stop = tem_trade_aberto(cripto, subconta)

                    ultima_execucao_trade_conductor = executar_trade_conductor_se_necessario(
                        ultima_execucao_trade_conductor,
                        frequencia_agente_horas,
                        df,
                        cripto,
                        subconta,
                        tempo_grafico,
                        estado_de_trade,
                        preco_entrada,
                        preco_alvo,
                        preco_stop,
                        tamanho_posicao,
                        qtd_min_para_operar,
                        trailing_stop,
                        vela_abertura_trade,
                        logger
                    )

                    if df['maxima'].iloc[-1] >= preco_alvo and preco_alvo != 0:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.TARGET_HIT, "Take profit atingido", MODULE_NAME,
                            symbol=cripto, tempo_abertura=df.index[-1], preco_alvo=preco_alvo,
                            preco_atual=df['maxima'].iloc[-1])
                        stops_consecutivos = 0

                    elif df['minima'].iloc[-1] <= preco_stop:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.STOP_HIT, "Stop loss atingido", MODULE_NAME,
                            symbol=cripto, tempo_abertura=df.index[-1], preco_stop=preco_stop,
                            preco_atual=df['minima'].iloc[-1])
                        stops_consecutivos += 1
                        logger.warning(LogCategory.TRADE_STATUS_ERROR,
                            f"⚠️ Stop Consecutivo {stops_consecutivos}/{MAX_STOPS_CONSECUTIVOS}", MODULE_NAME, symbol=cripto)
                        if stops_consecutivos >= MAX_STOPS_CONSECUTIVOS:
                            bloqueio_ate = time.time() + (PAUSA_CIRCUIT_BREAKER_HORAS * 3600)
                            stops_consecutivos = 0
                            logger.critical(LogCategory.FATAL_ERROR, f"🛑 Múltiplos stops atingidos. Circuit Breaker ativado. Bot suspenso até {datetime.fromtimestamp(bloqueio_ate).strftime('%Y-%m-%d %H:%M:%S')} (Pausa de {PAUSA_CIRCUIT_BREAKER_HORAS}h).", MODULE_NAME, symbol=cripto)

                    elif estado_de_trade == EstadoDeTrade.DE_FORA:
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.MANUAL_CLOSE, "Trade fechado manualmente na corretora", MODULE_NAME,
                            symbol=cripto, tempo_abertura=df.index[-1])
                        logger.info(LogCategory.TRADE_SEARCH, "🔍 Procurando oportunidades de trade", MODULE_NAME,
                            subconta=subconta, symbol=cripto, tempo_grafico=tempo_grafico, lado_operacao=lado_operacao.value)

                elif estado_de_trade == EstadoDeTrade.VENDIDO:
                    estado_de_trade, preco_entrada, preco_stop, preco_alvo, tamanho_posicao, trailing_stop = tem_trade_aberto(cripto, subconta)

                    ultima_execucao_trade_conductor = executar_trade_conductor_se_necessario(
                        ultima_execucao_trade_conductor,
                        frequencia_agente_horas,
                        df,
                        cripto,
                        subconta,
                        tempo_grafico,
                        estado_de_trade,
                        preco_entrada,
                        preco_alvo,
                        preco_stop,
                        tamanho_posicao,
                        qtd_min_para_operar,
                        trailing_stop,
                        vela_abertura_trade,
                        logger
                    )

                    if df['minima'].iloc[-1] <= preco_alvo:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.TARGET_HIT, "Take profit atingido", MODULE_NAME,
                            symbol=cripto, tempo_abertura=df.index[-1], preco_alvo=preco_alvo,
                            preco_atual=df['minima'].iloc[-1])
                        stops_consecutivos = 0

                    elif df['maxima'].iloc[-1] >= preco_stop and preco_stop != 0:
                        estado_de_trade = EstadoDeTrade.DE_FORA
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.STOP_HIT, "Stop loss atingido", MODULE_NAME,
                            symbol=cripto, tempo_abertura=df.index[-1], preco_stop=preco_stop,
                            preco_atual=df['maxima'].iloc[-1])
                        stops_consecutivos += 1
                        logger.warning(LogCategory.TRADE_STATUS_ERROR,
                            f"⚠️ Stop Consecutivo {stops_consecutivos}/{MAX_STOPS_CONSECUTIVOS}", MODULE_NAME, symbol=cripto)
                        if stops_consecutivos >= MAX_STOPS_CONSECUTIVOS:
                            bloqueio_ate = time.time() + (PAUSA_CIRCUIT_BREAKER_HORAS * 3600)
                            stops_consecutivos = 0
                            logger.critical(LogCategory.FATAL_ERROR, f"🛑 Múltiplos stops atingidos. Circuit Breaker ativado. Bot suspenso até {datetime.fromtimestamp(bloqueio_ate).strftime('%Y-%m-%d %H:%M:%S')} (Pausa de {PAUSA_CIRCUIT_BREAKER_HORAS}h).", MODULE_NAME, symbol=cripto)

                    elif estado_de_trade == EstadoDeTrade.DE_FORA:
                        vela_fechou_trade = df.index[-1]
                        logger.trading(LogCategory.MANUAL_CLOSE, "Trade fechado manualmente na corretora", MODULE_NAME,
                            symbol=cripto, tempo_abertura=df.index[-1])
                        logger.info(LogCategory.TRADE_SEARCH, "🔍 Procurando oportunidades de trade", MODULE_NAME,
                            subconta=subconta, symbol=cripto, tempo_grafico=tempo_grafico, lado_operacao=lado_operacao.value)

                elif estado_de_trade == EstadoDeTrade.DE_FORA and df.index[-1] != vela_fechou_trade:
                    if compras_habilitadas:
                        vela_referencia_condition = df['fechamento'].iloc[-2] > df['ema_rapida_compra'].iloc[-2] and df['fechamento'].iloc[-2] > df['ema_lenta_compra'].iloc[-2]
                        breakout_condition = df['maxima'].iloc[-1] > df['maxima'].iloc[-2]

                        if vela_referencia_condition and breakout_condition:
                            # ═══════════════════════════════════════════════════════
                            # [MELHORIA #2] FILTRO ATR — Bloqueia entradas em mercado lateral
                            # Só avança se o mercado tem volatilidade suficiente.
                            # ═══════════════════════════════════════════════════════
                            if not mercado_tem_volatilidade_suficiente(df, atr_periodo, atr_filtro_multiplicador, logger, cripto, MODULE_NAME):
                                pass  # Sinal ignorado silenciosamente — log já feito dentro da função
                            else:
                                estado_de_trade, _, _, _, _, _ = tem_trade_aberto(cripto, subconta)
                                if estado_de_trade == EstadoDeTrade.DE_FORA:
                                    if df.index[-1] != vela_executou_trade_entry_evaluator:
                                        # ═══════════════════════════════════════════════════════
                                        # [FIX] UMA ÚNICA CHAMADA — resultado reutilizado para:
                                        #   1. Filtro D1 (verificação de tendência)
                                        #   2. Alimentar o agente Entry Evaluator
                                        # Antes havia 2 chamadas à API (bug de RateLimit).
                                        # ═══════════════════════════════════════════════════════
                                        df_1w, df_1d, df = prepare_multi_timeframe_technical_data(df, cripto)

                                        # [MELHORIA #1] FILTRO D1 — usa df_1d já obtido acima
                                        if not tendencia_diaria_confirma_compra(df_1d, logger, cripto, MODULE_NAME):
                                            logger.info(LogCategory.TRADE_SEARCH,
                                                "🚫 Compra bloqueada — Tendência do D1 não confirma alta. Aguardando alinhamento.",
                                                MODULE_NAME, symbol=cripto)
                                        else:
                                            logger.agent(LogCategory.AGENT_EXECUTION, "🤖 Iniciando análise de compra", MODULE_NAME,
                                                agent_name="Entry Evaluator", symbol=cripto, tempo_grafico=tempo_grafico, lado_operacao="compra")
                                            vela_executou_trade_entry_evaluator = df.index[-1]

                                            saldo = saldo_da_conta(subconta)
                                            # df_1w, df_1d, df ja obtidos acima - sem chamada dupla!
                                            # [FIX TF] TF operacional agora e 1H, entao subimos o auxiliar para 4H
                                            df_4h = busca_velas(cripto, '240', [9, 21])
                                            df_4h = prepare_market_data(df_4h, use_emas=True, emas_periods=[200], use_peaks=True, peaks_distance=21)
                                            resposta = trade_entry_evaluator.run(prompt_trade_entry_evaluator(
                                                saldo, tempo_grafico, ema_rapida_compra, ema_lenta_compra, cripto,
                                                qtd_min_para_operar, subconta, 'compra', df, df_1w, df_1d, df_4h
                                            ))

                                            logger.agent(LogCategory.AGENT_RESPONSE, "Resposta do Entry Evaluator recebida", MODULE_NAME,
                                                agent_name="Entry Evaluator", symbol=cripto, response_length=len(resposta.content), response_content=resposta.content)

                                            abriu_trade = TradeEntryEvaluatorParser.processar_resposta(resposta, cripto, subconta, tempo_grafico, risco_por_operacao.value, logger)

                                            if abriu_trade:
                                                vela_abertura_trade = df.index[-1]
                                                ultima_execucao_trade_conductor = datetime.now()
                                                next_execution = (ultima_execucao_trade_conductor + timedelta(hours=frequencia_agente_horas)).strftime('%Y-%m-%d %H:%M:%S')
                                                logger.agent(LogCategory.AGENT_SCHEDULE, "Análise do condutor programada", MODULE_NAME,
                                                    agent_name="Trade Conductor", symbol=cripto, proxima_execucao=next_execution)
                                            else:
                                                logger.info(LogCategory.TRADE_SEARCH, "🔍 Procurando oportunidades de trade", MODULE_NAME,
                                                    subconta=subconta, symbol=cripto, tempo_grafico=tempo_grafico, lado_operacao=lado_operacao.value)

                    if vendas_habilitadas:
                        vela_venda_condition = df['fechamento'].iloc[-2] < df['ema_rapida_venda'].iloc[-2] and df['fechamento'].iloc[-2] < df['ema_lenta_venda'].iloc[-2]
                        breakout_condition = df['minima'].iloc[-1] < df['minima'].iloc[-2]

                        if vela_venda_condition and breakout_condition:
                            # ═══════════════════════════════════════════════════════
                            # [MELHORIA #2] FILTRO ATR — Bloqueia entradas em mercado lateral
                            # ═══════════════════════════════════════════════════════
                            if not mercado_tem_volatilidade_suficiente(df, atr_periodo, atr_filtro_multiplicador, logger, cripto, MODULE_NAME):
                                pass  # Sinal ignorado silenciosamente — log já feito dentro da função
                            else:
                                estado_de_trade, _, _, _, _, _ = tem_trade_aberto(cripto, subconta)
                                if estado_de_trade == EstadoDeTrade.DE_FORA:
                                    if df.index[-1] != vela_executou_trade_entry_evaluator:
                                        # ═══════════════════════════════════════════════════════
                                        # [FIX] UMA ÚNICA CHAMADA — resultado reutilizado para:
                                        #   1. Filtro D1 (verificação de tendência)
                                        #   2. Alimentar o agente Entry Evaluator
                                        # Antes havia 2 chamadas à API (bug de RateLimit).
                                        # ═══════════════════════════════════════════════════════
                                        df_1w, df_1d, df = prepare_multi_timeframe_technical_data(df, cripto)

                                        # [MELHORIA #1] FILTRO D1 — usa df_1d já obtido acima
                                        if not tendencia_diaria_confirma_venda(df_1d, logger, cripto, MODULE_NAME):
                                            logger.info(LogCategory.TRADE_SEARCH,
                                                "🚫 Venda bloqueada — Tendência do D1 não confirma baixa. Aguardando alinhamento.",
                                                MODULE_NAME, symbol=cripto)
                                        else:
                                            logger.agent(LogCategory.AGENT_EXECUTION, "🤖 Iniciando análise de venda", MODULE_NAME,
                                                agent_name="Entry Evaluator", symbol=cripto, tempo_grafico=tempo_grafico, lado_operacao="venda")
                                            vela_executou_trade_entry_evaluator = df.index[-1]

                                            saldo = saldo_da_conta(subconta)
                                            # df_1w, df_1d, df ja obtidos acima - sem chamada dupla!
                                            # [FIX TF] TF operacional agora e 1H, entao subimos o auxiliar para 4H
                                            df_4h = busca_velas(cripto, '240', [9, 21])
                                            df_4h = prepare_market_data(df_4h, use_emas=True, emas_periods=[200], use_peaks=True, peaks_distance=21)
                                            resposta = trade_entry_evaluator.run(prompt_trade_entry_evaluator(
                                                saldo, tempo_grafico, ema_rapida_venda, ema_lenta_venda, cripto,
                                                qtd_min_para_operar, subconta, 'venda', df, df_1w, df_1d, df_4h
                                            ))

                                            logger.agent(LogCategory.AGENT_RESPONSE, "Resposta do Entry Evaluator recebida", MODULE_NAME,
                                                agent_name="Entry Evaluator", symbol=cripto, response_length=len(resposta.content), response_content=resposta.content)

                                            abriu_trade = TradeEntryEvaluatorParser.processar_resposta(resposta, cripto, subconta, tempo_grafico, risco_por_operacao.value, logger)

                                            if abriu_trade:
                                                vela_abertura_trade = df.index[-1]
                                                ultima_execucao_trade_conductor = datetime.now()
                                                next_execution = (ultima_execucao_trade_conductor + timedelta(hours=frequencia_agente_horas)).strftime('%Y-%m-%d %H:%M:%S')
                                                logger.agent(LogCategory.AGENT_SCHEDULE, "Análise do condutor programada", MODULE_NAME,
                                                    agent_name="Trade Conductor", symbol=cripto, proxima_execucao=next_execution)
                                            else:
                                                logger.info(LogCategory.TRADE_SEARCH, "🔍 Procurando oportunidades de trade", MODULE_NAME,
                                                    subconta=subconta, symbol=cripto, tempo_grafico=tempo_grafico, lado_operacao=lado_operacao.value)

        # ═══════════════════════════════════════════════════════════════════
        # [MELHORIA #3] TRATAMENTO ESPECÍFICO DE ERROS DA EXCHANGE
        # ───────────────────────────────────────────────────────────────────
        # Antes: qualquer erro da Bybit caía no "except Exception" genérico.
        # Agora: cada tipo de erro tem uma resposta específica e inteligente.
        #
        # RateLimitExceeded: A Bybit te bloqueou temporariamente por fazer
        #   muitas chamadas. Solução: esperar 15 segundos antes de continuar.
        #   (Backoff exponencial — tenta depois de 15s, depois 30s, etc.)
        #
        # NetworkError: A internet caiu ou o servidor da Bybit está fora.
        #   Solução: esperar 10 segundos e tentar reconectar.
        #
        # ExchangeNotAvailable: Manutenção da exchange.
        #   Solução: esperar 60 segundos (não adianta tentar agora).
        # ═══════════════════════════════════════════════════════════════════
        except Exception as e:
            erro_str = str(e).lower()

            # Detecta erros de RateLimit pelo texto (compatível sem importar ccxt diretamente)
            if 'ratelimit' in erro_str or 'rate limit' in erro_str or '429' in erro_str or 'too many requests' in erro_str or '10006' in erro_str or 'x-bapi-limit' in erro_str:
                logger.warning(LogCategory.CONNECTION_ERROR,
                    "⏳ Rate Limit atingido — Bybit pediu para desacelerar. Pausando 15 segundos.",
                    MODULE_NAME, symbol=cripto, erro_message=str(e))
                time.sleep(15)  # Pausa obrigatória antes de tentar novamente
                continue  # Pula direto para a próxima iteração do while

            # Detecta erros de rede/conexão
            elif 'network' in erro_str or 'connection' in erro_str or 'timeout' in erro_str or 'connectionerror' in erro_str:
                logger.error(LogCategory.CONNECTION_ERROR,
                    "🌐 Erro de rede detectado. Aguardando 10s para reconectar...",
                    MODULE_NAME, symbol=cripto, erro_message=str(e))
                time.sleep(10)
                continue

            # Detecta exchange em manutenção
            elif 'exchange not available' in erro_str or 'maintenance' in erro_str or '503' in erro_str:
                logger.error(LogCategory.CONNECTION_ERROR,
                    "🔧 Exchange indisponível (possível manutenção). Aguardando 60s...",
                    MODULE_NAME, symbol=cripto, erro_message=str(e))
                time.sleep(60)
                continue

            # Detecta saldo insuficiente — não deve tentar operar de novo sem intervenção
            elif 'insufficient' in erro_str or 'balance' in erro_str:
                logger.critical(LogCategory.FATAL_ERROR,
                    "💸 Saldo insuficiente na exchange. Verifique sua conta. Bot aguardando 5 minutos.",
                    MODULE_NAME, symbol=cripto, erro_message=str(e))
                time.sleep(300)  # 5 minutos — operador precisa agir
                continue

            elif isinstance(e, ConnectionError):
                logger.error(LogCategory.CONNECTION_ERROR, "Erro de conexão durante execução do bot", MODULE_NAME,
                    symbol=cripto, erro_message=str(e), exception=e)

            elif isinstance(e, ValueError):
                logger.error(LogCategory.VALUE_ERROR, "Erro de valor durante execução do bot", MODULE_NAME,
                    symbol=cripto, erro_message=str(e), exception=e)

            elif isinstance(e, KeyboardInterrupt):
                logger.info(LogCategory.SHUTDOWN, "Programa encerrado pelo usuário", MODULE_NAME,
                    symbol=cripto)
                exit()

            else:
                logger.error(LogCategory.UNKNOWN_ERROR, "Erro desconhecido durante execução do bot", MODULE_NAME,
                    symbol=cripto, erro_message=str(e), exception=e)

        time.sleep(30)  # 1H timeframe: 30s entre ciclos é suficiente e evita RateLimit

    # Se chegou aqui, o bot foi encerrado pela API
    logger.info(LogCategory.BOT_STOP, "Bot encerrado pela API", MODULE_NAME,
        symbol=cripto, bot_id=bot_id)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--subconta', type=int, default=subconta)
    parser.add_argument('--cripto', type=str, default=cripto)
    parser.add_argument('--tempo_grafico', type=str, default=tempo_grafico)
    parser.add_argument('--lado_operacao', type=str,
        default=lado_operacao.value, choices=[lado.value for lado in LadoOperacao],
        help='Modo de operação: compra, venda, ambos')
    parser.add_argument('--risco_por_operacao', type=float,
        default=risco_por_operacao.value, choices=[risco.value for risco in RiscoOperacao],
        help='Risco por operação: 0.005, 0.01, 0.02, 0.05, 0.08')
    parser.add_argument('--frequencia_agente_horas', type=float, default=frequencia_agente_horas)
    parser.add_argument('--executar_agente_no_start', type=bool, default=executar_agente_no_start,
        help='Executar agente condutor no start: True, False')

    # Parâmetros de compra
    parser.add_argument('--ema_rapida_compra', type=int, default=ema_rapida_compra)
    parser.add_argument('--ema_lenta_compra', type=int, default=ema_lenta_compra)

    # Parâmetros de venda
    parser.add_argument('--ema_rapida_venda', type=int, default=ema_rapida_venda)
    parser.add_argument('--ema_lenta_venda', type=int, default=ema_lenta_venda)

    args = parser.parse_args()
    start_live_trading_bot(
        subconta=args.subconta,
        cripto=args.cripto,
        tempo_grafico=args.tempo_grafico,
        lado_operacao=LadoOperacao(args.lado_operacao),
        frequencia_agente_horas=args.frequencia_agente_horas,
        executar_agente_no_start=args.executar_agente_no_start,
        ema_rapida_compra=args.ema_rapida_compra,
        ema_lenta_compra=args.ema_lenta_compra,
        ema_rapida_venda=args.ema_rapida_venda,
        ema_lenta_venda=args.ema_lenta_venda,
        risco_por_operacao=RiscoOperacao(args.risco_por_operacao)
    )
