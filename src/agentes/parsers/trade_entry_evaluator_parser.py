from typing import Dict, Optional
from .base_parser import BaseParser
from decimal import Decimal, ROUND_DOWN

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from corretoras.funcoes_bybit import busca_velas, saldo_da_conta, quantidade_minima_para_operar, abre_compra, abre_venda, tem_trade_aberto
from utils.utilidades import calcular_risco_retorno_compra, calcular_risco_retorno_venda
from utils.logging import LogCategory
from utils.orchestrator_client import FleetOrchestrator

class TradeEntryEvaluatorParser(BaseParser):
    """Parser específico para o Trade Entry Evaluator"""
    
    def __init__(self):
        super().__init__("TradeEntryEvaluator")
        
        # Ações válidas específicas
        self.valid_actions = {
            'ignorar', 'comprar', 'vender'
        }
    
    def validate(self, data: Dict) -> bool:
        """Validação específica do Trade Entry Evaluator"""
        # Campos obrigatórios
        required_fields = {'acoes', 'confianca', 'justificativa'}
        if not required_fields.issubset(data.keys()):
            return False
        
        # Validação da confiança
        confianca = data.get('confianca')
        if not isinstance(confianca, (int, float)) or not (0.0 <= confianca <= 1.0):
            return False
        
        # Validação das ações
        acoes = data.get('acoes', [])
        if not isinstance(acoes, list) or not acoes:
            return False
        
        for acao in acoes:
            if not isinstance(acao, dict):
                return False
            
            acao_tipo = acao.get('acao')
            if acao_tipo not in self.valid_actions:
                return False
            
            # Validações específicas por tipo de ação
            if not self._validate_action(acao):
                return False
        
        return True
    
    def _validate_action(self, acao: Dict) -> bool:
        """Valida ação específica"""
        acao_tipo = acao.get('acao')
        
        # Ação ignorar - não precisa de campos adicionais
        if acao_tipo == 'ignorar':
            return True
        
        # Ações de compra e venda precisam de campos obrigatórios
        if acao_tipo in ['comprar', 'vender']:
            required_fields = ['preco_stop', 'preco_alvo']
            
            # Verifica se todos os campos obrigatórios estão presentes
            if not all(field in acao for field in required_fields):
                return False
            
            # Valida tipos dos campos
            preco_stop = acao.get('preco_stop')
            preco_alvo = acao.get('preco_alvo')
            
            if not isinstance(preco_stop, (int, float)) or preco_stop <= 0:
                return False
            
            if not isinstance(preco_alvo, (int, float)) or preco_alvo <= 0:
                return False
            
            # Validação lógica: para compra, alvo > stop; para venda, alvo < stop
            if acao_tipo == 'comprar' and preco_alvo <= preco_stop:
                return False
            
            if acao_tipo == 'vender' and preco_alvo >= preco_stop:
                return False
        
        return True
    
    def parse_response(self, response_text: str) -> Optional[Dict]:
        """Parse completo da resposta do Trade Entry Evaluator"""
        data = self.extract_json(response_text)
        
        if not data:
            self.log_error("Nenhum JSON encontrado na resposta")
            return None
        
        if not self.validate(data):
            self.log_error("Dados inválidos", data)
            return None
        
        # Normalização específica
        return self._normalize_data(data)
    
    def _normalize_data(self, data: Dict) -> Dict:
        """Normaliza dados específicos do Trade Entry Evaluator"""
        # Garante que confiança seja float
        data['confianca'] = float(data['confianca'])
        
        # Normaliza valores nas ações
        for acao in data['acoes']:
            # Normaliza preços
            for price_field in ['preco_stop', 'preco_alvo']:
                if price_field in acao:
                    acao[price_field] = float(acao[price_field])
        
        return data



    @staticmethod
    def processar_resposta(resposta, cripto, subconta, tempo_grafico, risco_por_operacao, logger, preco_atual_travado=None):
        """
        preco_atual_travado: preço capturado UMA VEZ, no momento em que o
        sinal foi montado para a LLM (antes de chamá-la). Se fornecido, o
        parser usa esse valor em vez de buscar um preço novo na Bybit.

        [ANALISE DE ARQUITETURA] Sem isso, o parser buscava um preço NOVO
        aqui, depois da LLM já ter respondido - ou seja, a LLM calculava o
        RRR com um preço, e o Python recalculava minutos depois com outro.
        O preço anda nesse intervalo, o que degrada o RRR de forma
        sistemática logo abaixo do mínimo aceitável. Mantém compatibilidade
        (None = busca como antes) para não quebrar nenhum outro chamador.
        """
        confianca_aceitavel = 0.65
        risco_retorno_aceitavel = 1.5
        
        parser = TradeEntryEvaluatorParser()
        resposta_json = parser.parse_response(resposta.content)

        if resposta_json is None:
            logger.error(LogCategory.PARSING_ERROR, "Resposta inválida do trade entry evaluator", "trade_entry_evaluator_parser",
                symbol=cripto, timeframe=tempo_grafico)
        else:
            # print(f'Resposta do trade entry evaluator: {resposta.content}', flush=True)
            # print('-' * 10)
            confianca = resposta_json.get('confianca', 0.0)
            acoes = resposta_json.get('acoes', [])
            justificativa = resposta_json.get('justificativa', 'Sem justificativa')
            
            # verifica se a confiança é suficiente para executar o trade e se existe 'ação':'ignorar' na lista de ações
            if confianca < confianca_aceitavel:
                FleetOrchestrator(logger=logger).log_evaluator_decision(cripto, f"Confiança baixa ({confianca}). {justificativa}", "REJEITADO", confianca, "Mean Reversion")
                if acoes and any(acao.get('acao') == 'ignorar' for acao in acoes):
                    logger.agent(LogCategory.AGENT_DECISION, "Trade ignorado conforme decisão do agente", "trade_entry_evaluator_parser",
                        agent_name="Entry Evaluator", symbol=cripto, action="ignorar", decision="NO_ACTION")
                    return False
                else:
                    logger.agent(LogCategory.AGENT_DECISION, f"Confiança ({confianca}) baixa para executar trade", "trade_entry_evaluator_parser",
                        agent_name="Entry Evaluator", symbol=cripto, confidence=confianca, 
                        threshold=confianca_aceitavel, decision="NO_ACTION")
                    return False

            if acoes:
                for acao in acoes:
                    if acao.get('acao') == 'ignorar':
                        FleetOrchestrator(logger=logger).log_evaluator_decision(cripto, f"Agente decidiu ignorar. {justificativa}", "REJEITADO", confianca, "Mean Reversion")
                        logger.agent(LogCategory.AGENT_DECISION, "Trade ignorado conforme decisão do agente", "trade_entry_evaluator_parser",
                            agent_name="Entry Evaluator", symbol=cripto, action="ignorar", decision="NO_ACTION")
                        return False

                    if acao.get('acao') == 'comprar':
                        preco_stop = acao.get('preco_stop')
                        preco_alvo = acao.get('preco_alvo')

                        qtd_min_para_operar = quantidade_minima_para_operar(cripto, subconta)
            
                        if preco_atual_travado is not None:
                            preco_atual = preco_atual_travado
                        else:
                            df = busca_velas(cripto, tempo_grafico, [9,21])
                            preco_atual = df['fechamento'].iloc[-1]

                        if preco_atual < preco_stop:
                            logger.warning(LogCategory.INVALID_PRICES, "Preço atual é menor que o preço de stop - operação cancelada", "trade_entry_evaluator_parser",
                                symbol=cripto, current_price=preco_atual, stop_price=preco_stop)
                            return False
                        elif preco_atual > preco_alvo:
                            logger.warning(LogCategory.INVALID_PRICES, "Preço atual é maior que o preço de alvo - operação cancelada", "trade_entry_evaluator_parser",
                                symbol=cripto, current_price=preco_atual, target_price=preco_alvo)
                            return False

                        distancia_stop_percent = (preco_atual - preco_stop) / preco_atual

                        saldo = saldo_da_conta(subconta) * 0.98
                        tamanho_posicao = saldo * risco_por_operacao / distancia_stop_percent

                        # Trava de Segurança: Alavancagem Máxima 3x
                        if tamanho_posicao > (saldo * 3):
                            logger.warning(LogCategory.INVALID_ORDER_QTY, "Risco rejeitado: Alavancagem exigida maior que 3x - operação cancelada", "trade_entry_evaluator_parser",
                                symbol=cripto, required_leverage=float(tamanho_posicao/saldo), max_leverage=3)
                            FleetOrchestrator(logger=logger).log_evaluator_decision(cripto, f"Risco Rejeitado: Alavancagem exigida > 3x. {justificativa}", "REJEITADO", confianca, "Mean Reversion")
                            return False

                        qtd_cripto_para_operar = tamanho_posicao / preco_atual

                        quantidade_cripto_para_operar = int(qtd_cripto_para_operar / qtd_min_para_operar) * qtd_min_para_operar
                        quantidade_cripto_para_operar = Decimal(quantidade_cripto_para_operar)
                        quantidade_cripto_para_operar = quantidade_cripto_para_operar.quantize(Decimal(f'{qtd_min_para_operar}'), rounding=ROUND_DOWN)

                        # Validar quantidade antes de operar
                        if quantidade_cripto_para_operar <= 0:
                            logger.warning(LogCategory.INVALID_ORDER_QTY, "Quantidade calculada é zero ou negativa - operação cancelada", "trade_entry_evaluator_parser",
                                symbol=cripto, calculated_quantity=float(quantidade_cripto_para_operar),
                                min_quantity=qtd_min_para_operar, current_price=preco_atual,
                                stop_price=preco_stop, target_price=preco_alvo)
                            return False

                        risco_retorno = calcular_risco_retorno_compra(preco_atual, preco_stop, preco_alvo)
                        if risco_retorno < risco_retorno_aceitavel:
                            logger.warning(LogCategory.LOW_RISK_REWARD, "Risco/retorno abaixo do aceitável - operação ignorada", "trade_entry_evaluator_parser",
                                symbol=cripto, risk_reward=risco_retorno, threshold=risco_retorno_aceitavel)
                            return False

                        resposta = abre_compra(cripto, str(quantidade_cripto_para_operar), str(preco_stop), str(preco_alvo), subconta)
                        if resposta.get('retCode') != 0:
                            logger.warning(LogCategory.TRADE_OPEN_ERROR, "Erro ao abrir posição - operação ignorada", "trade_entry_evaluator_parser",
                                symbol=cripto, error_message=resposta.get('retMsg'), error_code=resposta.get('retCode'))
                            return False

                        logger.trading(LogCategory.POSITION_OPEN, "Posição LONG aberta pelo Entry Evaluator", "trade_entry_evaluator_parser",
                            symbol=cripto, entry_price=preco_atual, stop_price=preco_stop, target_price=preco_alvo,
                            position_size=float(quantidade_cripto_para_operar), risk_reward=risco_retorno, operation="compra")

                        FleetOrchestrator(logger=logger).log_evaluator_decision(cripto, f"Trade LONG Aberto! {justificativa}", "EXECUTADO", confianca, "Mean Reversion")

                        try:
                            _, preco_real_entrada, _, _, _, _ = tem_trade_aberto(cripto, subconta)
                            FleetOrchestrator(logger=logger).log_execution_quality(cripto, "mean_reversion", "compra", preco_atual, preco_real_entrada)
                        except Exception:
                            pass  # tracking de qualidade nunca deve afetar o resultado do trade

                        return True

                    if acao.get('acao') == 'vender':
                        preco_stop = acao.get('preco_stop')
                        preco_alvo = acao.get('preco_alvo')

                        qtd_min_para_operar = quantidade_minima_para_operar(cripto, subconta)

                        if preco_atual_travado is not None:
                            preco_atual = preco_atual_travado
                        else:
                            df = busca_velas(cripto, tempo_grafico, [9,21])
                            preco_atual = df['fechamento'].iloc[-1]

                        if preco_atual > preco_stop:
                            logger.warning(LogCategory.INVALID_PRICES, "Preço atual é maior que o preço de stop - operação cancelada", "trade_entry_evaluator_parser",
                                symbol=cripto, current_price=preco_atual, stop_price=preco_stop)
                            return False
                        elif preco_atual < preco_alvo:
                            logger.warning(LogCategory.INVALID_PRICES, "Preço atual é menor que o preço de alvo - operação cancelada", "trade_entry_evaluator_parser",
                                symbol=cripto, current_price=preco_atual, target_price=preco_alvo)
                            return False

                        distancia_stop_percent = (preco_stop - preco_atual) / preco_atual

                        saldo = saldo_da_conta(subconta) * 0.98
                        tamanho_posicao = saldo * risco_por_operacao / distancia_stop_percent

                        # Trava de Segurança: Alavancagem Máxima 3x
                        if tamanho_posicao > (saldo * 3):
                            logger.warning(LogCategory.INVALID_ORDER_QTY, "Risco rejeitado: Alavancagem exigida maior que 3x - operação cancelada", "trade_entry_evaluator_parser",
                                symbol=cripto, required_leverage=float(tamanho_posicao/saldo), max_leverage=3)
                            FleetOrchestrator(logger=logger).log_evaluator_decision(cripto, f"Risco Rejeitado: Alavancagem exigida > 3x. {justificativa}", "REJEITADO", confianca, "Mean Reversion")
                            return False

                        qtd_cripto_para_operar = tamanho_posicao / preco_atual

                        quantidade_cripto_para_operar = int(qtd_cripto_para_operar / qtd_min_para_operar) * qtd_min_para_operar
                        quantidade_cripto_para_operar = Decimal(quantidade_cripto_para_operar)
                        quantidade_cripto_para_operar = quantidade_cripto_para_operar.quantize(Decimal(f'{qtd_min_para_operar}'), rounding=ROUND_DOWN)

                        # Validar quantidade antes de operar
                        if quantidade_cripto_para_operar <= 0:
                            logger.warning(LogCategory.INVALID_ORDER_QTY, "Quantidade calculada é zero ou negativa - operação cancelada", "trade_entry_evaluator_parser",
                                symbol=cripto, calculated_quantity=float(quantidade_cripto_para_operar),
                                min_quantity=qtd_min_para_operar, current_price=preco_atual,
                                stop_price=preco_stop, target_price=preco_alvo)
                            return False

                        risco_retorno = calcular_risco_retorno_venda(preco_atual, preco_stop, preco_alvo)
                        if risco_retorno < risco_retorno_aceitavel:
                            logger.warning(LogCategory.LOW_RISK_REWARD, "Risco/retorno abaixo do aceitável - operação ignorada", "trade_entry_evaluator_parser",
                                symbol=cripto, risk_reward=risco_retorno, threshold=risco_retorno_aceitavel)
                            return False

                        resposta = abre_venda(cripto, str(quantidade_cripto_para_operar), str(preco_stop), str(preco_alvo), subconta)
                        if resposta.get('retCode') != 0:
                            logger.warning(LogCategory.TRADE_OPEN_ERROR, "Erro ao abrir posição - operação ignorada", "trade_entry_evaluator_parser",
                                symbol=cripto, error_message=resposta.get('retMsg'), error_code=resposta.get('retCode'))
                            return False

                        logger.trading(LogCategory.POSITION_OPEN, "Posição SHORT aberta pelo Entry Evaluator", "trade_entry_evaluator_parser",
                            symbol=cripto, entry_price=preco_atual, stop_price=preco_stop, target_price=preco_alvo,
                            position_size=float(quantidade_cripto_para_operar), risk_reward=risco_retorno, operation="venda")

                        FleetOrchestrator(logger=logger).log_evaluator_decision(cripto, f"Trade SHORT Aberto! {justificativa}", "EXECUTADO", confianca, "Mean Reversion")

                        try:
                            _, preco_real_entrada, _, _, _, _ = tem_trade_aberto(cripto, subconta)
                            FleetOrchestrator(logger=logger).log_execution_quality(cripto, "mean_reversion", "venda", preco_atual, preco_real_entrada)
                        except Exception:
                            pass  # tracking de qualidade nunca deve afetar o resultado do trade

                        return True
