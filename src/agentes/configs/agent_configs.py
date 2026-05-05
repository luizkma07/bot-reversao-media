from agno.models.anthropic import Claude
# from agno.models.groq import Groq

# Verificação de importação do Gemini
try:
    from agno.models.google import Gemini
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from ..instructions.trade_conductor_v1 import TRADE_CONDUCTOR_V1
from ..instructions.trade_conductor_v2 import TRADE_CONDUCTOR_V2
from ..instructions.trade_conductor_v3 import TRADE_CONDUCTOR_V3
from ..instructions.trade_entry_evaluator_v1 import TRADE_ENTRY_EVALUATOR_V1
from ..instructions.trade_entry_evaluator_v2 import TRADE_ENTRY_EVALUATOR_V2
# [MEAN REVERSION] Instruções específicas da estratégia MR
from ..instructions.trade_entry_evaluator_mr_v1 import TRADE_ENTRY_EVALUATOR_MR_V1
from ..instructions.trade_conductor_mr_v1 import TRADE_CONDUCTOR_MR_V1

class ModelConfigs:
    CLAUDE_SONNET = Claude(id="claude-sonnet-4-20250514")
    CLAUDE_SONNET_4_5 = Claude(id="claude-sonnet-4-5-20250929")
    CLAUDE_HAIKU = Claude(id="claude-3-5-haiku-20241022")

    # Instancia Gemini apenas se a lib estiver disponível
    if GEMINI_AVAILABLE:
        GEMINI_FLASH = Gemini(id="gemini-2.5-flash", response_mime_type="application/json")
        GEMINI_PRO = Gemini(id="gemini-2.5-pro", response_mime_type="application/json")
    # GROQ_LLAMA = Groq(id="llama-3.3-70b-versatile")

    MODEL_MAP = {
        "sonnet": CLAUDE_SONNET,
        "sonnet-4-5": CLAUDE_SONNET_4_5,
        "haiku": CLAUDE_HAIKU,
    }

    # Adiciona Gemini ao mapa apenas se disponível
    if GEMINI_AVAILABLE:
        MODEL_MAP["gemini-flash"] = GEMINI_FLASH
        MODEL_MAP["gemini-pro"] = GEMINI_PRO
    # "groq": GROQ_LLAMA

class InstructionSets:
    TRADE_CONDUCTOR_V1 = TRADE_CONDUCTOR_V1
    TRADE_CONDUCTOR_V2 = TRADE_CONDUCTOR_V2
    TRADE_CONDUCTOR_V3 = TRADE_CONDUCTOR_V3
    TRADE_ENTRY_EVALUATOR_V1 = TRADE_ENTRY_EVALUATOR_V1
    TRADE_ENTRY_EVALUATOR_V2 = TRADE_ENTRY_EVALUATOR_V2
    # [MEAN REVERSION]
    TRADE_ENTRY_EVALUATOR_MR_V1 = TRADE_ENTRY_EVALUATOR_MR_V1
    TRADE_CONDUCTOR_MR_V1 = TRADE_CONDUCTOR_MR_V1

class AgentConfigs:
    @staticmethod
    def get_trade_conductor_config(version="mr-v1", model="gemini-pro"):
        instruction_map = {
            "v1": InstructionSets.TRADE_CONDUCTOR_V1,
            "v2": InstructionSets.TRADE_CONDUCTOR_V2,
            "v3": InstructionSets.TRADE_CONDUCTOR_V3,
            "mr-v1": InstructionSets.TRADE_CONDUCTOR_MR_V1,
        }

        return {
            "model": ModelConfigs.MODEL_MAP[model],
            "instructions": instruction_map[version]
        }

    @staticmethod
    def get_trade_entry_evaluator_config(version="mr-v1", model="gemini-pro"):
        instruction_map = {
            "v1": InstructionSets.TRADE_ENTRY_EVALUATOR_V1,
            "v2": InstructionSets.TRADE_ENTRY_EVALUATOR_V2,
            "mr-v1": InstructionSets.TRADE_ENTRY_EVALUATOR_MR_V1,
        }

        return {
            "model": ModelConfigs.MODEL_MAP[model],
            "instructions": instruction_map[version]
        }