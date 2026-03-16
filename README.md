# Doma Points Public

Публичная (sanitized) версия Python-скрипта для:
- проверки `points` по кошелькам через Doma API,
- базового меню режимов (`Swap` / `Bridge` / `Check points`),
- запуска на Windows через `.bat`.

## Важно

Перед запуском добавьте **свои** данные в локальные файлы.

## Быстрый старт (Windows)

1. Установите Python 3.11+  
2. Запустите:
   - `INSTALL.bat`
3. Заполните файлы:
   - `.env` (можно скопировать из `.env.example`)
   - `api_keys.txt`
   - `wallets.txt`
   - `proxies.txt` (опционально)
   - `keys.txt` (только если нужен live swap/bridge)
4. Запустите:
   - `START.bat`
   - или `python main.py`

## Структура ключевых файлов

- `main.py` — основной запуск и меню
- `config.py` — загрузка конфигурации
- `doma_api.py` — API/GraphQL + EVM-клиент
- `points_checker.py` — отдельный чекер points
- `relay_bridge.py` — логика bridge-задач
- `strategy.py` — простая стратегия
- `test_risk_logic.py` — тест риск-логики

## Формат файлов

### `api_keys.txt`
Один API key на строку:
```txt
v1.your_api_key_1
v1.your_api_key_2

### `wallets.txt`
Один EVM-адрес на строку:

0xYourWallet1
0xYourWallet2

### proxies.txt (опционально)
Один прокси на строку:

user:pass@host:port
http://user:pass@host:port

Режим Check points (post-line mapping)
В режиме 3) Check points используется построчное соответствие:

строка N в wallets.txt
строка N в api_keys.txt
строка N в proxies.txt (если указан)
То есть wallet[1] проверяется через api_key[1] и proxy[1], и т.д.

Безопасность
Никогда не публикуйте реальные:

приватные ключи
API ключи
рабочие прокси
.env с секретами
Рекомендуется:

использовать отдельные API keys,
ограничивать API keys по IP (если нужно),
хранить секреты только локально.
Disclaimer
Код предоставлен в образовательных/исследовательских целях.
Используйте на свой риск и соблюдайте правила сервисов/сетей, которые используете.
