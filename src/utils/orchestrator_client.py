import os
import json
import urllib.request
import urllib.error
from datetime import datetime

class FleetOrchestrator:
    def __init__(self, logger=None):
        self.url   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
        self.token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
        self.logger = logger
        self.last_known_state = None

    def get_fleet_state(self):
        """Busca o estado global de toda a frota no Redis"""
        if not self.url or not self.token:
            return None
            
        endpoint = f"{self.url}/get/fleet_state"
        req = urllib.request.Request(endpoint, method='GET')
        req.add_header('Authorization', f'Bearer {self.token}')
        
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read().decode('utf-8'))
                if result.get("result"):
                    state_json = json.loads(result["result"])
                    self.last_known_state = state_json
                    return state_json
        except Exception as e:
            pass # Fica silencioso se der erro de conexão para não flodar o console
        
        return self.last_known_state

    def get_bot_state(self, bot_name):
        """Retorna o estado específico de um bot, aplicando multiplicadores do CRO"""
        state = self.get_fleet_state()
        
        if not state:
            return None
            
        bot_config = state.get("bots", {}).get(bot_name)
        if not bot_config:
            return None
            
        cro_multiplier = state.get("cro_multiplier", 1.0)
        
        config_efetiva = dict(bot_config)
        config_efetiva["cro_multiplier"] = cro_multiplier
        config_efetiva["alpha_veredicto"] = state.get("alpha_veredicto", "GREEN")
        
        if "risco_percent" in config_efetiva:
            config_efetiva["risco_efetivo"] = config_efetiva["risco_percent"] * cro_multiplier
            
        if "risco_long_percent" in config_efetiva:
            config_efetiva["risco_long_efetivo"] = config_efetiva["risco_long_percent"] * cro_multiplier
            
        if "risco_short_percent" in config_efetiva:
            config_efetiva["risco_short_efetivo"] = config_efetiva["risco_short_percent"] * cro_multiplier

        return config_efetiva

    def log_execution_quality(self, cripto, nome_bot, lado, preco_esperado, preco_real):
        """
        Registra a diferença entre o preço no momento da decisão (última vela
        fechada usada para calcular stop/alvo/tamanho) e o preço real de
        entrada da posição (lido via tem_trade_aberto logo após a ordem a
        mercado ser confirmada). Best-effort — nunca lança exceção nem afeta
        o fluxo de abertura de trade, que já está concluído quando isso roda.

        Antes disso (auditoria de 2026-08), nenhum ponto do sistema media
        slippage de execução — ordens são sempre a mercado (orderType=Market),
        então o slippage é real e nunca foi quantificado.
        """
        if not self.url or not self.token:
            return
        try:
            if not preco_esperado or not preco_real:
                return
            slippage_pct = ((preco_real - preco_esperado) / preco_esperado) * 100
            if lado == "venda":
                slippage_pct = -slippage_pct

            registro = {
                'data': datetime.now().isoformat(),
                'bot': nome_bot,
                'cripto': cripto,
                'lado': lado,
                'preco_esperado': preco_esperado,
                'preco_real': preco_real,
                'slippage_pct': round(slippage_pct, 4),
            }
            payload = json.dumps(registro, ensure_ascii=False)
            endpoint = f"{self.url}/LPUSH/execution_quality_log"
            req = urllib.request.Request(endpoint, data=payload.encode('utf-8'), method='POST')
            req.add_header('Authorization', f'Bearer {self.token}')
            urllib.request.urlopen(req, timeout=5)
            # Mantém só os 500 registros mais recentes
            trim_req = urllib.request.Request(f"{self.url}/LTRIM/execution_quality_log/0/499")
            trim_req.add_header('Authorization', f'Bearer {self.token}')
            urllib.request.urlopen(trim_req, timeout=5)
        except Exception:
            pass

    def send_heartbeat(self, bot_name):
        """
        Grava um sinal de vida no Redis (best-effort — nunca bloqueia nem lança
        exceção, para nunca atrasar ou derrubar o loop principal do bot).
        Usado pelo watchdog do Alpha Strategist (webhook_server.py) para
        detectar bot travado/crashado sem ninguém perceber.
        """
        if not self.url or not self.token:
            return
        try:
            payload = json.dumps({"ts": datetime.now().isoformat()})
            endpoint = f"{self.url}/set/heartbeat:{bot_name}"
            req = urllib.request.Request(endpoint, data=payload.encode('utf-8'), method='POST')
            req.add_header('Authorization', f'Bearer {self.token}')
            req.add_header('Content-Type', 'application/json')
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass

    def log_trade_closed(self, bot_name, simbolo, motivo_fechamento, risco_efetivo_pct=None):
        """
        Registra o CONTEXTO de um fechamento de trade (best-effort — nunca
        bloqueia nem lança exceção). O número de PnL em si vem sempre da
        Bybit (fonte de verdade, via coletor_trades_fechados.py no repo do
        Alpha Strategist); isto aqui só guarda o "porquê" — motivo do
        fechamento e regime do Alpha no momento — para depois ser cruzado
        com o registro real da corretora por bot+símbolo+janela de tempo.
        """
        if not self.url or not self.token:
            return
        try:
            estado = self.last_known_state or {}
            registro = {
                'bot': bot_name,
                'simbolo': simbolo,
                'motivo_fechamento': motivo_fechamento,
                'risco_efetivo_pct': risco_efetivo_pct,
                'alpha_veredicto_momento': estado.get('alpha_veredicto', ''),
                'cro_multiplier_momento': estado.get('cro_multiplier', ''),
                'timestamp': datetime.now().isoformat(),
            }
            payload = json.dumps(registro, ensure_ascii=False)
            endpoint = f"{self.url}/LPUSH/trade_history"
            req = urllib.request.Request(endpoint, data=payload.encode('utf-8'), method='POST')
            req.add_header('Authorization', f'Bearer {self.token}')
            urllib.request.urlopen(req, timeout=5)
            trim_req = urllib.request.Request(f"{self.url}/LTRIM/trade_history/0/1999")
            trim_req.add_header('Authorization', f'Bearer {self.token}')
            urllib.request.urlopen(trim_req, timeout=5)
        except Exception:
            pass
