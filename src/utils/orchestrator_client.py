import os
import json
import urllib.request
import urllib.error

class FleetOrchestrator:
    def __init__(self, logger=None):
        self.url = os.environ.get("UPSTASH_REDIS_REST_URL", "https://credible-airedale-125727.upstash.io")
        self.token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "gQAAAAAAAesfAAIgcDFhNzlkZmQ5OTQ5YWQ0YmMyYjYxMzgzMjAwOTI2Y2IwMw")
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
