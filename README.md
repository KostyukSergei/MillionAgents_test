# Magnit Mobile API Parser — Категория "Кофе"

## 🔐 Как получить токен и device-id

Необходимо перехватить трафик мобильного приложения (например через Charles / mitmproxy / Proxyman).

В запросах к:

```
https://middle-api.magnit.ru
```

нужно найти заголовки:

```
authorization: bearer <JWT_TOKEN>
x-device-id: <UUID>
```

Эти значения используются при запуске скрипта.

---

## 🚀 Запуск скрипта

Пример для Санкт-Петербурга:

```bash
python magnit_parser.py \
  --city "Санкт-Петербург" \
  --out coffee_spb.csv \
  --token <bearer> \
  --device-id <x-device-id>
```

Пример для Москвы:

```bash
python magnit_parser.py \
  --city "Москва" \
  --out coffee_moscow.csv \
  --token <bearer> \
  --device-id <x-device-id>
```
