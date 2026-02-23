# Setup Pulito SbSim su Windows + WSL (Ubuntu 24.04) + Ollama

Questa guida riassume la procedura per:

- usare **Python 3.11** (SbSim non supporta Python 3.12)
- creare un **virtual environment** dentro WSL
- installare tutte le librerie richieste da SbSim
- installare Ollama e il relativo modello Qwen 

---

## 0. Prerequisiti
- Wsl installato
- Comando 'wsl' su terminale Python per entrare nel terminale Ubuntu

## 1. Clonazione SbSim
Da Terminale (Windows o Ubuntu):

```bash
git clone https://github.com/google/sbsim.git
cd sbsim
```
- NOTA: il main deve essere collocato all'interno della cartella 'sbsim' per sfruttare le dipendenze di libreria
---

---

## 2. Aprire Ubuntu (WSL)

Da Terminale:

```bash
wsl
```

# 3. Installare Python 3.11 (necessario)

Alcuni moduli di SbSim non supportano Python 3.12, quindi serve Python 3.11.

## 3.1 Installare pyenv

Pacchetti e librerie necessarie per compilare Python:

```bash
sudo apt update
sudo apt install -y make build-essential curl git \
  libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
  libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev \
  libffi-dev liblzma-dev
```

Installazione pyenv (per usare Python 3.11 su Ubuntu 24.04)

```bash
curl https://pyenv.run | bash
```

Attivazione sul terminale (in `~/.bashrc`):

```bash
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
echo 'export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
echo 'eval "$(pyenv init -)"' >> ~/.bashrc
echo 'eval "$(pyenv virtualenv-init -)"' >> ~/.bashrc
exec $SHELL
```

## 3.2 Installazione Python 3.11

```bash
pyenv install 3.11.8
pyenv global 3.11.8
hash -r
python -V
```
- Python -V deve restituire '3.11.x'
---

# 4. Creare Virtual Environment per SbSim (in /home)

Esempio con il mio path:

```bash
mkdir -p /home/simos/venvs
python -m venv /home/simos/venvs/sbsim311
source /home/simos/venvs/sbsim311/bin/activate
python -m pip install --upgrade pip setuptools wheel
```

---

# 5. Installare dipendenze SbSim

Dentro `sbsim/`:

```bash
pip install -e .
pip install tf-agents
pip install tensorflow absl-py gin-config bidict holidays pandas matplotlib nbconvert jupyter
```

---

# 6. Installazione Ollama + Qwen su WSL

## 6.1 Installare Ollama

```bash
sudo apt update
sudo apt-get install zstd
curl -fsSL https://ollama.com/install.sh | sh
```

## 6.2 Scaricare Qwen

```bash
ollama pull qwen2.5:7b
```
Nota: potrebbe essere necessario re-installare il modulo dopo la chiusura e riapertura del main (verifica tramire Run)

Verifica installazione:

```bash
curl http://localhost:11434/api/tags
```

---

# 7. Avviare Ollama (prima del run)

Prima di fare il run:

```bash
wsl
ollama serve
```

NOTA: Ollama resta attivo solo finché il terminale è aperto.

---

# 8. Configurare Interpreter WSL in PyCharm

Settings → Project → Python Interpreter → Add Interpreter → WSL → Existing

Selezionare l'ambiente in cui è stato installato python 3.11 (nel mio caso):

```
/home/simos/venvs/sbsim311/bin/python
```

---


