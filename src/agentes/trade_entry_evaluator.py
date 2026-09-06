from agno.agent import Agent

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from agentes.toolkits.bybit import BybitTools
from agentes.configs.agent_configs import AgentConfigs

AGENT_VERSION = "mr-v1" # ACHADO REAL (2026-09): estava em "v2", que é o prompt
# GENÉRICO de rompimento (mesmo texto do Sniper — "evitar falsos rompimentos",
# "ADX > 25 confirma inércia direcional validando o rompimento"). Pra um bot de
# Reversão à Média isso é INVERTIDO: aqui ADX alto é motivo de VETO (tendência
# forte = ambiente ruim pra reversão), não confirmação de entrada. O prompt
# certo (trade_entry_evaluator_mr_v1.py) já existia no repo, escrito e correto,
# só nunca foi ligado — "v2" era passado explícito no lugar do default "mr-v1"
# da própria função get_trade_entry_evaluator_config.
MODEL_TYPE = "gemini-pro-3-1" # sonnet-4-5, gemini-flash, gemini-pro, gemini-pro-3-1

config = AgentConfigs.get_trade_entry_evaluator_config(AGENT_VERSION, MODEL_TYPE)

trade_entry_evaluator = Agent(
    name="Avaliador de entrada de trade",
    model=config["model"],
    description="Agente especializado em avaliar se um trade deve ser realizado com base no contexto técnico do mercado.",
    instructions=config["instructions"],
    # tools=[
    #     BybitTools(
    #         # buscar_contexto=True,
    #         abrir_compra=False,
    #         abrir_venda=False
    #     )
    # ],
    debug_mode=False,
)