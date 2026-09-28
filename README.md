# 🧥 Фит-мастер — телеграм-бот

Погодный стилист: **Яндекс Погода + Гидрометцентр России + МирПогоды + РП5** →
ИИ Groq собирает бомбовский образ (цвета, материалы, слои, вайб) с учётом
твоего гардероба, распознанного по фото.

## Команды
Telegram разрешает командам только латиницу, поэтому русский — обычными словами:

| Написать | Что делает | Статус |
|---|---|---|
| `/city Москва` или «город Москва» | сохранить город | ✅ |
| `/fit` или «фит» / «погода» / «что надеть» | фит под погоду + закреп | ✅ стабильно |
| 📸 фото вещи | распознавание → в гардероб | ✅ |
| `/wardrobe` или «гардероб», `/delw N`, `/clearw` | гардероб | ✅ |
| `/style streetwear` или «стиль streetwear» | разбор стиля в ЛС | 🧪 **бета** (без погоды) |
| подписка на канал (`SUB_CHANNEL`) | гейт доступа | ✅ |

## 🇷🇺 Источники погоды
| Источник | Тип | Ключ |
|---|---|---|
| Яндекс Погода | API | нужен (Яндекс Облако, бесплатный тест) |
| Гидрометцентр России (meteoinfo.ru) | HTML-парсинг | не нужен |
| МирПогоды (world-weather.ru) | HTML-парсинг | не нужен |
| РП5 (rp5.ru) | HTML-парсинг | не нужен |
| Open-Meteo | API | только аварийный запас (`FALLBACK_OPEN_METEO=1`) |

## Где брать ключи
- **BOT_TOKEN** → [@BotFather](https://t.me/BotFather): `/newbot`
- **GROQ_API_KEY** → [console.groq.com](https://console.groq.com) (бесплатно)
- **YANDEX_WEATHER_KEY** → [Яндекс Облако](https://yandex.cloud/ru) → Weather API
- **PEXELS_API_KEY** → [pexels.com/api](https://www.pexels.com/api/) (опц.)

## Деплой на Railway (без волума)
1. Залей репозиторий в GitHub.
2. Railway → **New Project → Deploy from GitHub repo**.
3. **Variables** → минимум `BOT_TOKEN` и `GROQ_API_KEY`.
4. В логах должна быть строка: `Фит-мастер v2.3 запущен 🧥🇷🇺`.
5. Для подписки — добавь бота **админом в канал**.

## Локальный запуск
bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=... GROQ_API_KEY=...
python bot.py
