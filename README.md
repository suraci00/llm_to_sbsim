## Prerequisiti
- Windows + WSL (Ubuntu)
- Python (dentro WSL)
- Ollama installato su WSL
- main dentro la cartella sb_sim:
1. git clone https://github.com/google/sbsim.git
2. cd sbsim (sposta il file qui dentro)

## Configurazione interprete WSL:
1. Creare interprete WSL da --> Settings --> Project --> Add Python Interpreter --> To WSL
2. Su terminale Python: 'wsl' (per entrare su terminale Ubuntu)

## Installazione OLLAMA e QWEN (LLM) su WSL
Su terminale Ubuntu:
1. 'sudo apt update'
2. 'sudo apt-get install zstd' (necessario per estrarre OLLAMA)
3. 'curl -fsSL https://ollama.com/install.sh | sh' (installazione OLLAMA)
4. 'ollama pull qwen2.5:7b' (installazione modello QWEN 2.5 a 7b parametri)
5. 'curl http://localhost:11434/api/tags' (per verificare se il modello è installato)

## Moduli richiesti per 'environment_test_utils.py':
0. Su terminale Ubuntu: 'source /home/simos/.virtualenvs/sbsim/bin/activate' (per entrare nel venv sbsim)
1. 'pip uninstall -y protobuf pip install  "protobuf==3.20.*" '' (downgrade protobuf per renderlo compatibile con SbSim)
2. 'pip install holidays' (libreria richiesta da Environments)

## Per avviare Ollama (prima del run)
Da terminale Python eseguire i seguenti comandi:
1. 'wsl' (per entrare su terminale Ubuntu)
2. 'ollama serve' (per avviare Ollama. NOTA: se si chiude il terminale anche Ollama verrà chiuso)
