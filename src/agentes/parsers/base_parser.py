import json
import re
from abc import ABC, abstractmethod
from typing import Dict, Optional, Any

class BaseParser(ABC):
    """Parser base para todos os agentes"""
    
    # Regex compilada para performance
    JSON_PATTERN = re.compile(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', re.DOTALL)
    
    def __init__(self, agent_name: str):
        self.agent_name = agent_name
    
    def extract_json(self, text: str) -> Optional[Dict]:
        """Extração básica de JSON com limpeza defensiva de Markdown"""
        text_limpo = text.strip()
        
        # Limpador de Markdown (Regex simplificado via replace)
        if text_limpo.startswith('```json'):
            text_limpo = text_limpo[7:]
        elif text_limpo.startswith('```'):
            text_limpo = text_limpo[3:]
            
        if text_limpo.endswith('```'):
            text_limpo = text_limpo[:-3]
            
        text_limpo = text_limpo.strip()

        # Tenta o parse direto primeiro (cenário ideal do response_mime_type)
        try:
            return json.loads(text_limpo)
        except json.JSONDecodeError:
            pass
            
        # Fallback para regex
        matches = self.JSON_PATTERN.findall(text_limpo)
        
        if not matches:
            return None
            
        for match in reversed(matches):
            try:
                return json.loads(match)
            except json.JSONDecodeError:
                continue
        
        return None
    
    @abstractmethod
    def validate(self, data: Dict) -> bool:
        """Validação específica do agente"""
        pass
    
    @abstractmethod
    def parse_response(self, response_text: str) -> Optional[Dict]:
        """Parse completo da resposta"""
        pass
    
    def log_error(self, error: str, data: Any = None):
        """Log de erros padronizado"""
        print(f"[{self.agent_name}] Parser Error: {error}")
        if data:
            print(f"Data: {data}")