# Doma Points Public Bot

Публичная очищенная версия бота для Doma Network: свапы, bridge, проверка points, закрытие позиций, сбор токенов, фарм объёма, торговля domain-токенами, domain quest volume и выставление доменов на продажу.

## Важно

Этот репозиторий не содержит приватных ключей, кошельков, прокси и реальных API-ключей. Все локальные секреты должны храниться только в `.env`, `keys.txt`, `wallets.txt`, `api_keys.txt`, `proxies.txt`.

Использование на реальных кошельках связано с рисками:

- расход газа;
- slippage и price impact;
- ошибки RPC/API;
- задержки обновления points;
- риск невыгодного фарма;
- риск выставить домен по неправильной цене, если задать неверный диапазон.

Тестируйте на малых суммах.

## Возможности

Главное меню `main.py`:

```text
1) Bridge
2) Check points
3) Close all positions
4) Swap domain token
5) Swap ETH <-> USDC.E
6) Collect all tokens -> ETH / USDC.E
7) Farm 250+ volume ETH <-> USDC.E
8) Domain quest volume
9) List unlisted domains for sale
10) Exit
```

## Domain Quest Tokens

В режиме `8) Domain quest volume` доступны:

```text
rides.com
software.ai
alert.ai
swimsuits.ai
trenches.ai
depin.ai
terabytes.ai
mishka.ai
playonline.ai
exemption.ai
bipod.ai
itprojects.ai
lifeadvice.ai
onlineadvisor.ai
continents.ai
loancrypto.ai
coinlogic.ai
agenticconsultant.ai
gobitcoin.xyz
closingbells.com
get.cash
```

Режим делает объём по паре `USDC.E <-> domain token`, умеет стартовать от `ETH`, проверяет доступные балансы и по завершении может вернуть остаток в `USDC.E` или `ETH`.

## Выставление доменов на продажу

Режим `9) List unlisted domains for sale`:

- берёт домены из кошелька;
- отдельно проверяет уже выставленные;
- выставляет только невыставленные;
- цену берёт случайно между minimum и maximum;
- округляет цену до десятых;
- валюта листинга: `USDC.E`;
- срок листинга по умолчанию: `90` дней;
- пауза между доменами настраивается в меню;
- результаты пишет в `domain_listings.csv`.

Для листинга используется `@doma-protocol/orderbook-sdk`, поэтому нужны Node.js-зависимости.

## Установка

Требования:

- Windows;
- Python `3.11+`;
- Node.js `20+`;
- доступ к Doma RPC;
- свои кошельки и приватные ключи.

Шаги:

1. Склонируйте репозиторий.
2. Запустите `INSTALL.bat`.
3. Установите Node.js-зависимости:

```bash
npm install
```

4. Скопируйте `.env.example` в `.env`, если `INSTALL.bat` не сделал это автоматически.
5. Заполните локальные файлы:

```text
wallets.txt
keys.txt
api_keys.txt
proxies.txt
```

6. Запустите:

```bash
START.bat
```

или:

```bash
python main.py
```

## Формат файлов

`wallets.txt`:

```text
0x...
0x...
```

`keys.txt`:

```text
private_key_for_wallet_1
private_key_for_wallet_2
```

Строки должны совпадать:

```text
wallets.txt line 1 -> keys.txt line 1 -> api_keys.txt line 1 -> proxies.txt line 1
wallets.txt line 2 -> keys.txt line 2 -> api_keys.txt line 2 -> proxies.txt line 2
```

`api_keys.txt` можно оставить пустым, если используется публичный fallback или ключ задан в `.env`.

`proxies.txt` необязателен. Если прокси не нужны, оставьте файл пустым или с комментариями.

## Основные настройки `.env`

```text
RPC_URL=https://rpc.doma.xyz/
SUBGRAPH_URL=https://graph.doma.xyz/subgraphs/name/uniswap-v3-doma-mainnet
CHAIN_ID=97477

PAPER_MODE=false
DRY_RUN=false
ENABLE_EXECUTION=true
REQUIRE_LIVE_CONFIRMATION=false

WALLET_DELAY_MIN_SEC=4
WALLET_DELAY_MAX_SEC=10
```

Если включены реальные транзакции, приватные ключи должны быть указаны в `keys.txt` или `.env`.

## Основные файлы

```text
main.py                  main menu and mode execution
doma_api.py              Doma API, quotes, router and execution helpers
config.py                config loading from .env and local files
contracts.json           Doma contract and token addresses
relay_bridge.py          bridge logic
position_manager.py      close position logic
points_checker.py        points helper
strategy.py              strategy helper
badges_parser.py         badge holder parser and export tool
doma_list_domain.mjs     Doma orderbook listing helper
doma_node_esm_loader.mjs Node ESM compatibility loader for Doma SDK
```

## Badge Parser

`badges_parser.py` выгружает badge holder stats и умеет сохранять отчёты в удобных форматах.

Пример:

```bash
python badges_parser.py
```

## Выходные файлы

Бот может создавать локальные файлы:

```text
trades.csv
points.csv
domain_listings.csv
bot.log
state.json
```

Они не должны попадать в GitHub.

## Безопасность

Никогда не коммитьте:

- `.env`;
- `keys.txt`;
- `wallets.txt`;
- `api_keys.txt`;
- `proxies.txt`;
- `state.json`;
- CSV-логи;
- `bot.log`;
- `node_modules/`.

Перед публикацией проверяйте:

```bash
git status --short
git diff --cached
```

## Disclaimer

Проект предоставляется как есть. Вы сами отвечаете за приватные ключи, транзакции, комиссии, выставленные цены, торговые решения и последствия использования бота.
