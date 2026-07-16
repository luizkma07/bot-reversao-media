import os
from dotenv import load_dotenv

load_dotenv()

from agno.agent import Agent
from agno.models.google import Gemini

try:
    print(f"GOOGLE_API_KEY: {os.environ.get('GOOGLE_API_KEY')[:10]}...")
    
    agent = Agent(
        model=Gemini(id="gemini-2.5-pro"),
        description="Teste",
    )
    
    response = agent.run("Responda com 'Google OK'")
    print(f"Agno Google API OK! Resposta: {response.content}")
except Exception as e:
    print(f"Agno Google API Error: {e}")
