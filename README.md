# Doma Points Public Bot

Публичная очищенная версия бота для Doma Network. Бот работает с кошельками построчно и поддерживает bridge, points check, свапы, сбор токенов, фарм объема, domain quest volume, листинг/делистинг доменов, покупку дешевых domain tokens с клеймом subdomain, bridge доменов в Base и выставление offers в USDC.E.

## Важно

Репозиторий не должен содержать реальные приватные ключи, кошельки, прокси и API-ключи. Локальные секреты храните только в файлах:

```text
.env
wallets.txt
keys.txt
api_keys.txt
proxies.txt
```

Использование на реальных кошельках связано с рисками:

- расход газа;
- slippage и price impact;
- ошибки RPC/API/Relay/Doma;
- задержки обновления points;
- риск невыгодного фарма объема;
- риск выставить листинг или offer не с той суммой, если неверно заданы параметры.

Тестируйте новые режимы на малых суммах и с одного кошелька.

## Установка

Требования:

- Windows;
- Python 3.11+;
- Node.js 20+;
- доступ к Doma RPC;
- кошельки и приватные ключи.

Шаги:

1. Запустите `INSTALL.bat`.
2. Установите Node.js зависимости:

```bash
npm install
```

3. Заполните локальные файлы:

```text
wallets.txt
keys.txt
api_keys.txt
proxies.txt
```

4. Запустите:

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

`api_keys.txt` можно оставить пустым, если используется ключ из `.env` или публичный fallback.

`proxies.txt` необязателен для части режимов, но marketplace/cheap-buy режимы обычно используют прокси построчно.

## Актуальное меню

```text
1) Bridge
2) Check points + quests
3) Close all positions
4) Swap domain token
5) Swap ETH <-> USDC.E
6) Collect all tokens -> ETH / USDC.E
7) Farm 250+ volume ETH <-> USDC.E
8) Domain quest volume
9) List unlisted domains for sale
10) Cancel active domain listings
11) Buy $0.01 cheap tokens + claim subdomain
12) Season quest: bridge a domain from Doma to Base
13) Place domain offers
14) Accept received domain offers
15) Create full-range liquidity
16) Exit
```

## 1) Bridge

Relay bridge mode.

Доступные маршруты:

```text
1) Base -> Doma | ETH -> ETH
2) Base -> Doma | ETH -> USDC.E
3) Mantle + Blast -> Doma | all native ETH -> ETH
4) Back
```

Особенности:

- для Base -> Doma можно выбрать amount как число или процент;
- для Mantle + Blast бот отправляет native balance в Doma;
- для Mantle оставляется резерв `0.2` native MNT на газ;
- для native percent bridge оставляется небольшой gas reserve;
- используются Relay API и RPC fallback.

## 2) Check points + quests

Проверяет points по кошелькам и пишет результат в CSV.

Дополнительно проверяет выполнение Doma quests по периодам:

- `DAILY`
- `WEEKLY`
- `SEASON`

Для каждого кошелька сразу после points в лог пишется блок заданий в столбик:

```text
Quests [0x...] [line=1]
  DAILY: 1/1
    [DONE] Make a $5+ swap on any domain token (+300 pts)
  WEEKLY: 1/3
    [DONE] List any domain on the marketplace. (+400 pts)
    [MISS] Trade $100 total volume (+700 pts)
  SEASON: 3/5
    [DONE] Stake 3 subdomains (+750 pts)
    [MISS] Add at least $50 in liquidity to a domain token. (+1500 pts)
```

Детализация по каждому заданию пишется в `quests.csv`.

## 3) Close all positions

Закрывает активные позиции ликвидности, если режим поддерживается текущими настройками и контрактами.

## 4) Swap domain token

Ручной свап выбранного domain token. Поддерживает маршруты через Doma UI quote/router.

## 5) Swap ETH <-> USDC.E

Парный свап ETH и USDC.E.

Особенности:

- можно выбрать source ETH или source USDC.E;
- amount задается числом или процентом;
- если `graph.doma.xyz` недоступен, бот использует fallback через Doma quote API и адреса из `contracts.json`;
- ETH source исполняется через native ETH route, без ручного wrap в интерфейсе.

## 6) Collect all tokens -> ETH / USDC.E

Собирает остатки токенов в выбранный финальный актив:

- `USDC.E`;
- `ETH`.

Может свапать WETH/domain tokens через доступные маршруты и выполнять финальный settle.

## 7) Farm 250+ volume ETH <-> USDC.E

Фармит объем по паре ETH <-> USDC.E.

Параметры:

- target volume;
- partial return percent range;
- финальный settle в ETH.

## 8) Domain quest volume

Фармит volume по domain token quest.

Особенности:

- выбирает/использует domain token;
- умеет стартовать от ETH или USDC.E;
- делает объем по `USDC.E <-> domain token`;
- может вернуть остаток в ETH или USDC.E.

## 9) List unlisted domains for sale

Выставляет невыставленные домены на продажу за USDC.E.

Параметры:

- minimum price;
- maximum price;
- listing duration;
- count mode: все домены или случайное количество min/max;
- delay между доменами;
- start wallet.

Особенности:

- проверяет owned/listed domains;
- выставляет только unlisted domains;
- цена выбирается случайно в заданном диапазоне;
- результаты пишутся в `domain_listings.csv`;
- используется `@doma-protocol/orderbook-sdk` через Node helper.

## 10) Cancel active domain listings

Снимает активные листинги доменов.

Особенности:

- используется off-chain cancellation flow;
- получает active listings по кошельку;
- проходит по доменам с задержками;
- пишет итог по кошелькам.

## 11) Buy $0.01 cheap tokens + claim subdomain

Покупает дешевые domain tokens и клеймит subdomains.

Параметры:

```text
Maximum token price USD
Minimum USDC.E amount per buy
Maximum USDC.E amount per buy
Minimum tokens to buy per wallet
Maximum tokens to buy per wallet
Minimum subdomains to claim per token
Maximum subdomains to claim per token
Minimum delay between buys sec
Maximum delay between buys sec
Start from wallet number
```

Текущая логика:

- сначала проверяет уже имеющиеся domain tokens на кошельке;
- если существующего токена хватает на subdomain staking price, сразу создает subdomain из него;
- может сделать несколько subdomains из одного токена, если баланса хватает;
- покупает новые токены только если существующих балансов не хватило;
- если USDC.E нет, бот пробует свапнуть ETH -> USDC.E;
- оставляет газовый резерв около `$0.05`;
- в конце выводит только номер кошелька, адрес и `insufficient balance` для кошельков, где реально не хватило баланса.

## 12) Season quest: bridge a domain from Doma to Base

Бриджит domain NFT из Doma в Base.

Параметры:

- listed domains;
- unlisted domains;
- any domains;
- сколько доменов бриджить на кошелек;
- задержки;
- start wallet.

## 13) Place domain offers

Ставит offers на домены в USDC.E.

Параметры:

```text
Minimum offer amount USDC.E
Maximum offer amount USDC.E
Offer duration days
Minimum offers per wallet
Maximum offers per wallet
Minimum delay between offers sec
Maximum delay between offers sec
Start from wallet number
```

Текущая логика:

- ETH не прибавляется к offer amount;
- offer amount выбирается случайно в диапазоне min/max USDC.E;
- для каждого offer может быть своя сумма;
- если USDC.E не хватает, бот сначала свапает ETH -> USDC.E;
- затем сразу ставит offers;
- результаты пишутся в `domain_offers.csv`;
- используется `@doma-protocol/orderbook-sdk` через Node helper.

## 14) Accept received domain offers

Принимает входящие top offer по доменам кошелька через Doma orderbook SDK.

Параметры:

```text
Minimum delay between accepts sec
Maximum delay between accepts sec
Start from wallet number
```

Текущая логика:

- берет только домены текущего кошелька со статусом `OFFERS_RECEIVED`;
- принимает только `highestOffer.externalId`, то есть top offer по домену;
- сортирует входящие top offers по цене и принимает самый дорогой на кошельке;
- один accepted offer продает/передает домен покупателю;
- результаты пишутся в `domain_accepted_offers.csv`;
- пропущенные кошельки печатаются в конце, чтобы их можно было доделать вручную.

## 15) Create full-range liquidity

Creates Uniswap V3 full-range liquidity positions through the configured NonfungiblePositionManager.

Parameters:

```text
Minimum total liquidity USD
Maximum total liquidity USD
Minimum delay between wallets sec
Maximum delay between wallets sec
Start from wallet number
```

Current logic:

- fetches top 10 pools by TVL from the Doma API, with subgraph fallback;
- picks one of those pools randomly per wallet;
- chooses total liquidity randomly inside min/max USD;
- treats min/max as the target UI liquidity range;
- adds an internal 1.18x mint buffer because full-range mint/UI valuation can consume/show less than the pre-mint USD estimate;
- splits buffered mint budget 50/50 between token0 and token1;
- reserves the USDC.E side of USDC.E/token pools so it is not spent while buying the other token;
- refuses to mint if either prepared token balance is below 97% of the buffered target;
- tops up missing tokens via USDC.E or ETH using Doma UI route;
- wraps ETH to WETH when WETH is required;
- approves token0/token1 to the position manager;
- mints full-range position using fee-tier tick spacing;
- results are written to `domain_liquidity_positions.csv`.

## Doma Quests

Практическое соответствие режимов:

```text
Daily: Make a $5+ swap on any domain token
-> пункт 8, target volume = 5

Weekly: List any domain on marketplace
-> пункт 9

Weekly: Trade $100 total volume
-> пункт 7, target volume = 100

Weekly: Trade $250 total volume
-> пункт 7, target volume = 250

Season: Bridge a domain from Doma to Base
-> пункт 12

Season: Stake subdomains
-> пункт 11
```

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
relay_bridge.py          Relay bridge logic
position_manager.py      close position logic
points_checker.py        points helper
strategy.py              strategy helper
badges_parser.py         badge holder parser and export tool
doma_list_domain.mjs     Doma orderbook listing helper
doma_cancel_listing.mjs  Doma orderbook cancel helper
doma_place_offer.mjs     Doma orderbook offer helper
doma_accept_offer.mjs    Doma orderbook accept-offer helper
doma_node_esm_loader.mjs Node ESM compatibility loader for Doma SDK
```

## Выходные файлы

Бот может создавать локальные файлы:

```text
trades.csv
points.csv
quests.csv
domain_listings.csv
domain_offers.csv
domain_accepted_offers.csv
domain_liquidity_positions.csv
bot.log
state.json
```

Эти файлы не должны попадать в публичный репозиторий.

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

Проект предоставляется как есть. Вы сами отвечаете за приватные ключи, транзакции, комиссии, выставленные цены, offers, торговые решения и последствия использования бота.
