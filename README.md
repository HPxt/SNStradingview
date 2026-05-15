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
- Envia alertas separados para RSI abaixo de 40 e RSI abaixo de 30.
- Evita e-mail duplicado para o mesmo par/timeframe/nivel dentro da mesma hora.
- Expoe `/` como health check, `/check` para rodar uma verificacao manual e `/rsi` para diagnosticar os valores atuais.

## Variaveis de ambiente

Para envio no Render Free, prefira Resend via HTTPS:

| Key | Exemplo |
| --- | --- |
| `RESEND_API_KEY` | `re_xxxxxxxxx` |
| `RESEND_FROM` | `RSI Monitor <onboarding@resend.dev>` |
| `EMAIL_TO` | `hppeixoto14@gmail.com` |

O Gmail SMTP pode funcionar localmente, mas o Render Free bloqueia trafego SMTP. Variaveis para uso local ou instancia paga:

| Key | Exemplo |
| --- | --- |
| `EMAIL_FROM` | `seuemail@gmail.com` |
| `EMAIL_TO` | `destino@gmail.com` |
| `GMAIL_PASS` | senha de app do Gmail |

Opcionais:

| Key | Padrao | Descricao |
| --- | --- | --- |
| `RSI_PERIOD` | `14` | Periodo do RSI |
| `RSI_LIMIT` | `40` | Compatibilidade: limite do alerta principal |
| `RSI_WARNING_LIMIT` | `40` | Dispara alerta quando RSI fica abaixo deste valor |
| `RSI_EXTREME_LIMIT` | `30` | Dispara alerta extremo separado quando RSI fica abaixo deste valor |
| `CHECK_INTERVAL_MIN` | `15` | Intervalo entre verificacoes |
| `LEVERAGE` | `10` | Alavancagem usada apenas para estimar TP/SL no alerta |
| `BACKTEST_MIN_TRADES` | `12` | Amostra minima para calibrar confianca pelo backtest |
| `BACKTEST_MAX_SIGNALS` | `80` | Maximo de sinais historicos avaliados por par/timeframe |
| `BACKTEST_VALIDATION_RATIO` | `0.35` | Parte final dos sinais usada como validacao fora da amostra |
| `BACKTEST_SIGNAL_COOLDOWN` | `0` | Cooldown entre sinais; `0` usa o horizonte do timeframe |
| `TRAINING_INTERVAL_MIN` | `180` | Frequencia do treino/calibracao em background |
| `TRAINING_CANDLE_LIMIT` | `3000` | Candles usados no treino historico por par/timeframe |
| `PLAN_MIN_WIN_RATE` | `60` | Assertividade minima para mostrar entrada/TP/SL no e-mail |
| `PLAN_MIN_PROFIT_FACTOR` | `1.25` | Profit factor minimo para mostrar plano no e-mail |
| `PLAN_MIN_AVG_ROI` | `0` | ROI medio minimo simulado para mostrar plano no e-mail |
| `PLAN_MIN_SCORE` | `60` | Score minimo da call para enviar e-mail |
| `SEND_ONLY_QUALIFIED_SIGNALS` | `true` | Quando `true`, so envia e-mail se a call completa passar nos filtros |
| `BINANCE_BASE_URLS` | `https://data-api.binance.vision,https://api1.binance.com,https://api.binance.com` | URLs da Binance para tentar em ordem |
| `DISABLE_SCHEDULER` | vazio | Use `true` para desativar o agendador |

Os planos de entrada/TP/SL enviados por e-mail sao educativos e baseados em indicadores tecnicos. Por padrao, o app so envia e-mail quando a call completa passa os filtros minimos de assertividade, ROI, payoff, score e confianca. Candidatos recusados podem ser vistos em `/rsi`, mas nao viram e-mail. O relatorio `/model-report` mostra a quantidade de dados por timeframe, o que passou e o que foi recusado. Eles nao executam ordens e nao substituem gestao de risco.

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
