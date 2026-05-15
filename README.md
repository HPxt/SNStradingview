# RSI Monitor - Binance + alerta por e-mail

Monitora RSI abaixo de 40 nos pares `JTOUSDT`, `ENAUSDT`, `IMXUSDT`, `PENDLEUSDT` e `BTCUSDT`, nos timeframes `15m`, `1h`, `4h` e `1d`. Quando algum ativo entra no criterio, o app envia um e-mail.

## Arquivos

```text
rsi_alertas/
|-- main.py           # codigo principal
|-- requirements.txt  # dependencias Python
|-- Procfile          # comando de start para Render/Heroku-like
|-- render.yaml       # configuracao do Render
`-- README.md
```

## Como funciona

- Usa a API publica da Binance Spot, entao nao precisa de API key para buscar candles.
- Calcula RSI de 14 periodos usando suavizacao estilo Wilder.
- Verifica os timeframes a cada 15 minutos.
- Evita e-mail duplicado para o mesmo par/timeframe dentro da mesma hora.
- Expoe `/` como health check, `/check` para rodar uma verificacao manual e `/rsi` para diagnosticar os valores atuais.

## Variaveis de ambiente

Obrigatorias para envio de e-mail:

| Key | Exemplo |
| --- | --- |
| `EMAIL_FROM` | `seuemail@gmail.com` |
| `EMAIL_TO` | `destino@gmail.com` |
| `GMAIL_PASS` | senha de app do Gmail |

Opcionais:

| Key | Padrao | Descricao |
| --- | --- | --- |
| `RSI_PERIOD` | `14` | Periodo do RSI |
| `RSI_LIMIT` | `40` | Dispara alerta quando RSI fica abaixo deste valor |
| `CHECK_INTERVAL_MIN` | `15` | Intervalo entre verificacoes |
| `BINANCE_BASE_URL` | `https://api.binance.com` | URL base da Binance |
| `DISABLE_SCHEDULER` | vazio | Use `true` para desativar o agendador |

## Configurar Gmail

1. Acesse sua conta Google e entre em **Seguranca**.
2. Ative a verificacao em duas etapas.
3. Crie uma **senha de app**.
4. Use essa senha como `GMAIL_PASS`.

## Rodar localmente

```bash
pip install -r requirements.txt
python main.py
```

Depois acesse:

- `http://localhost:5000/`
- `http://localhost:5000/check`
- `http://localhost:5000/rsi`

## Deploy no Render

1. Suba estes arquivos para um repositorio no GitHub.
2. No Render, crie um **Web Service**.
3. Use:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn main:app --workers 1 --threads 2 --timeout 120`
   - Instance Type: Free
4. Configure `EMAIL_FROM`, `EMAIL_TO` e `GMAIL_PASS` nas environment variables.

Importante: no plano gratuito, o Render pode dormir depois de inatividade. Para monitorar 24/7, use um ping externo como UptimeRobot chamando a rota `/` a cada 5 minutos.
