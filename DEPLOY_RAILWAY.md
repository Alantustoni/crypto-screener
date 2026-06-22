# Deploy 24/7 no Railway.app

O Railway é a opção mais simples: você envia o código para o GitHub e o Railway mantém o servidor rodando sozinho, sem precisar do seu PC ligado.

**Custo:** ~R$ 25/mês (plano Hobby, $5 USD)
**Tempo de setup:** ~15 minutos

---

## Passo 1 — Crie uma conta no GitHub (gratuito)

Se ainda não tem:
1. Acesse https://github.com e clique em **Sign up**
2. Use qualquer e-mail e crie um usuário

---

## Passo 2 — Instale o GitHub Desktop (mais fácil que a linha de comando)

1. Acesse https://desktop.github.com e baixe o instalador
2. Instale e faça login com sua conta GitHub

---

## Passo 3 — Envie o projeto para o GitHub

1. Abra o **GitHub Desktop**
2. Clique em **File → Add Local Repository**
3. Aponte para a pasta `C:\Users\lanco\OneDrive\Área de Trabalho\Alandev\crypto-screener`
4. Se aparecer "This directory does not appear to be a Git repository", clique em **create a repository**
5. Clique em **Publish repository** no topo
6. Desmarque "Keep this code private" (ou mantenha marcado se quiser privado) → **Publish Repository**

---

## Passo 4 — Crie uma conta no Railway.app

1. Acesse https://railway.app
2. Clique em **Start a New Project** → **Login with GitHub**
3. Autorize o Railway a acessar seu GitHub

---

## Passo 5 — Crie o projeto no Railway

1. No painel do Railway, clique em **+ New Project**
2. Escolha **Deploy from GitHub repo**
3. Selecione o repositório `crypto-screener`
4. O Railway detecta automaticamente que é Python e inicia o deploy

---

## Passo 6 — Adicione um Volume (para salvar o histórico)

O Railway apaga arquivos temporários ao fazer redeploy. Para manter o `signal_history.json`:

1. Na página do seu serviço no Railway, clique em **+ Add Volume**
2. Em **Mount Path**, coloque: `/app/data`
3. Clique em **Add**

Depois disso, atualize o caminho do arquivo de histórico:
- Abra `signal_history.py` e mude a linha do `HISTORY_PATH`:

```python
# Linha atual:
HISTORY_PATH = Path(__file__).parent / "signal_history.json"

# Mude para:
import os
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
HISTORY_PATH = DATA_DIR / "signal_history.json"
```

- Abra o Railway → seu serviço → **Variables** → **+ New Variable**
  - Nome: `DATA_DIR`
  - Valor: `/app/data`

---

## Passo 7 — Verifique o deploy

1. No Railway, clique em **View Logs** para acompanhar o startup
2. Quando aparecer `Uvicorn running on http://0.0.0.0:XXXXX`, o servidor está no ar
3. Clique em **Settings → Domains → Generate Domain** para obter um link público
4. Acesse o link no celular ou no PC — funciona sem precisar do seu computador!

---

## Como fazer updates depois

Sempre que quiser mudar o código:
1. Edite os arquivos normalmente
2. Abra o **GitHub Desktop** → escreva uma mensagem de commit → clique **Commit to main**
3. Clique em **Push origin**
4. O Railway detecta automaticamente e faz o redeploy em ~1 minuto

---

## Telegram continua funcionando

Os alertas do Telegram vão continuar sendo enviados pelo servidor na nuvem, não pelo seu PC.
Configure o bot normalmente na aba Telegram do dashboard.

---

## Resumo dos arquivos criados para o deploy

| Arquivo | Função |
|---|---|
| `Procfile` | Diz ao Railway como iniciar o servidor |
| `requirements.txt` | Lista as dependências Python |
| `railway.toml` | Configurações de build e restart automático |

