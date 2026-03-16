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
