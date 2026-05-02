import subprocess

scripts = [
    # Execução do Bot de Reversão à Média (Projeto Mean Reversion)
    'src/live_trading/mean_reversion_agent_evaluator.py'
]

processes = [subprocess.Popen(['python', script]) for script in scripts]

try:
    while True:
        pass

except KeyboardInterrupt:
    print("Finalizando processos...")

    for process in processes:
        process.terminate()
        process.wait()

    print("Todos os processos foram encerrados.")