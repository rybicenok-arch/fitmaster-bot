# -*- coding: utf-8 -*-
"""
Фит-мастер v2.3 — телеграм-бот.

Погодные источники (русские):
  • Яндекс Погода (API; если задан YANDEX_WEATHER_KEY) — основной
  • Гидрометцентр России (meteoinfo.ru)  — HTML, без ключа
  • МирПогоды (world-weather.ru)         — HTML, без ключа
  • РП5 (rp5.ru)                         — HTML, без ключа
  • (опция) Open-Meteo — аварийный запас, env FALLBACK_OPEN_METEO=1

Возможности:
  • /fit — Groq собирает образ под погоду + закреп в чате
  • фото вещи → vision-распознавание → личный гардероб
  • /style <название> — разбор стиля в ЛС (🧪 бета, без погоды)
  • обязательная подписка на канал (SUB_CHANNEL)
  • состояние в RAM — Railway без волума

Важно: команды Telegram — только латиница (a-z, 0-9, _),
поэтому русские алиасы работают как обычные сообщения:
«фит», «погода», «что надеть», «город Москва», «стиль techwear», «гардероб».
"""

import asyncio
import base64
import html
import io
import json
import logging
import os
import random
import re
from urllib.parse import quote, quote_plus, unquote, urljoin

import httpx
from bs4 import BeautifulSoup
from groq import AsyncGroq
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ------------------------------- ENV -------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
YANDEX_KEY = os.getenv("YANDEX_WEATHER_KEY")          # опц.: Яндекс Погода
PEXELS_KEY = os.getenv("PEXELS_API_KEY")              # опц.: фото-референсы
FALLBACK_OPEN_METEO = os.getenv("FALLBACK_OPEN_METEO", "0") in ("1", "true", "yes")

CHANNEL = os.getenv("SUB_CHANNEL", "").strip()        # "@channel" или "-100123..."
CHANNEL_URL = os.getenv("SUB_CHANNEL_URL") or (
    f"https://t.me/{CHANNEL.lstrip('@')}" if CHANNEL.startswith("@") else None
)

MAX_WARDROBE = 40
MAX_PHOTO_BYTES = 5 * 1024 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("fitbot")

if not BOT_TOKEN or not GROQ_API_KEY:
    raise SystemExit("Не заданы переменные окружения: BOT_TOKEN и GROQ_API_KEY")

groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# --------------------------- ПОДПИСКА (гейт) ---------------------------

def sub_keyboard() -> InlineKeyboardMarkup:
    row = []
    if CHANNEL_URL:
        row.append(InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_URL))
    row.append(InlineKeyboardButton("✅ Я подписался", callback_data="check_sub"))
    return InlineKeyboardMarkup([row])


async def check_sub(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if not CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        # бот не админ в канале / канал недоступен — не блокируем юзеров молча
        log.warning("Не смог проверить подписку (бот админ в канале?): %s", e)
        return True


async def require_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await check_sub(context, update.effective_user.id):
        return True
    await update.message.reply_text(
        "🔒 Чтобы пользоваться ботом, нужно быть подписанным на наш канал.\n\n"
        + ("" if CHANNEL_URL else f"Канал: <code>{html.escape(CHANNEL)}</code>\n")
        + "Подпишись и жми «✅ Я подписался».",
        reply_markup=sub_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return False


async def cb_check_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if await check_sub(context, q.from_user.id):
        try:
            await q.edit_message_text("✅ Подписка найдена! Пользуйся ботом 🧥")
        except Exception:
            pass
    else:
        await q.answer("Пока не вижу подписки 🤔", show_alert=True)

# ----------------------------- ГЕОКОДИНГ -----------------------------

async def geocode(city: str) -> dict | None:
    """Переводим название города в координаты (справочник, не погода)."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "ru"},
        )
        r.raise_for_status()
        results = r.json().get("results") or []
    if not results:
        return None
    g = results[0]
    display = str(g["name"]) + (f", {g['admin1']}" if g.get("admin1") else "")
    # name — ЧИСТОЕ имя города (для поиска на русских сайтах),
    # display — красивое (для сообщений)
    return {"name": str(g["name"]), "display": display,
            "lat": float(g["latitude"]), "lon": float(g["longitude"])}

# --------------------- РУССКИЕ ИСТОЧНИКИ ПОГОДЫ ---------------------

WEATHER_WORDS = sorted([
    "переменная облачность", "небольшой дождь", "сильный дождь", "дождь со снегом",
    "ледяной дождь", "небольшой снег", "сильный снег", "снежные ливни",
    "гроза с градом", "гроза с дождём", "без осадков", "малооблачно",
    "пасмурно", "облачно", "снегопад", "морось", "ливень", "дождь",
    "туман", "дымка", "ясно", "снег", "гроза", "град",
], key=len, reverse=True)

PRECIP_WORDS = ("дожд", "ливн", "снег", "морось", "гроза", "град")

WMO = {
    0: "ясно", 1: "почти ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь", 51: "лёгкая морось", 53: "морось", 55: "морось",
    61: "лёгкий дождь", 63: "дождь", 65: "ливень", 66: "ледяной дождь",
    67: "ледяной дождь", 71: "лёгкий снег", 73: "снег", 75: "сильный снег",
    77: "снежная крупка", 80: "ливни", 81: "ливни", 82: "сильные ливни",
    85: "снежные ливни", 86: "сильные снежные ливни", 95: "гроза",
    96: "гроза с градом", 99: "сильная гроза с градом",
}

YA_COND = {
    "clear": "ясно", "partly-cloudy": "переменная облачность", "cloudy": "облачно",
    "overcast": "пасмурно", "light-rain": "небольшой дождь", "rain": "дождь",
    "heavy-rain": "сильный дождь", "showers": "ливень", "wet-snow": "дождь со снегом",
    "light-snow": "небольшой снег", "snow": "снег", "snow-showers": "снегопад",
    "hail": "град", "thunderstorm": "гроза", "thunderstorm-with-rain": "гроза с дождём",
    "thunderstorm-with-hail": "гроза с градом", "mist": "дымка", "fog": "туман",
}


def _num(v, lo: float, hi: float) -> float | None:
    """Конвертит в float; ловит юникод-минус и запятую; режет по диапазону."""
    if v is None:
        return None
    s = str(v).replace(",", ".").replace("−", "-").replace("–", "-").strip()
    try:
        x = float(s)
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def finalize_wx(src, temp, feels=None, hum=None, precip=None, wind=None,
                desc=None, pressure=None) -> dict:
    """Единая валидация: мусорный источник -> RuntimeError -> пропуск."""
    temp = _num(temp, -75, 60)
    if temp is None:
        raise RuntimeError(f"{src}: температура не распознана — источник пропускается")
    desc = (desc or "").strip() or "—"
    if precip is None:
        precip = 1.0 if any(w in desc for w in PRECIP_WORDS) else 0.0
    return {
        "src": src,
        "temp": temp,
        "feels": _num(feels, -75, 60),
        "hum": _num(hum, 0, 100),
        "precip": _num(precip, 0, 300) or 0.0,
        "wind": _num(wind, 0, 80) or 0.0,
        "desc": desc,
        "pressure": _num(pressure, 500, 900),
    }


def parse_current_weather(text: str) -> dict:
    """Generic-парсер текущей погоды из текста страницы.
    Понимает юникод-минус и кириллическую «°С»."""
    t = re.sub(r"\s+", " ", text or "")

    def first(pattern):
        m = re.search(pattern, t, re.IGNORECASE)
        return m.group(1) if m else None

    # сначала ищем температуру рядом с маркером «сейчас», иначе первый °
    temp = first(r"(?:сейчас|текущ\w*|наблюдает\w*|факт\w*)[^0-9+\u2212-]{0,80}([+\u2212-]?\d+(?:[.,]\d+)?)\s*°")
    if temp is None:
        temp = first(r"([+\u2212-]?\d+(?:[.,]\d+)?)\s*°\s*[CСcс]?")
    feels = first(r"(?:ощущается|по ощущени\w*)[^0-9+\u2212-]{0,40}([+\u2212-]?\d+(?:[.,]\d+)?)")
    hum = first(r"влажность[^0-9%]{0,30}(\d{1,3})\s*%") or first(r"(\d{1,3})\s*%")
    wind = first(r"ветер[^0-9]{0,50}(\d+(?:[.,]\d+)?)\s*м/с") or first(r"(\d+(?:[.,]\d+)?)\s*м/с")
    pressure = first(r"давление[^0-9]{0,50}(\d{3,4})\s*(?:мм|гПа)")
    desc = next((w for w in WEATHER_WORDS if w in t.lower()), "—")
    return {"temp": temp, "feels": feels, "hum": hum, "wind": wind,
            "pressure": pressure, "desc": desc}


async def wx_yandex(lat: float, lon: float) -> dict:
    """Яндекс Погода — API (ключ из Яндекс Облака)."""
    if not YANDEX_KEY:
        raise RuntimeError("YANDEX_WEATHER_KEY не задан")
    headers = {"X-Yandex-Weather-Key": YANDEX_KEY, "X-Yandex-API-Key": YANDEX_KEY}
    endpoints = [
        ("https://api.weather.yandex.ru/v2/informers",
         {"lat": lat, "lon": lon, "lang": "ru_RU"}),
        ("https://api.weather.yandex.ru/v2/forecast",
         {"lat": lat, "lon": lon, "lang": "ru_RU", "limit": 1, "extra": "false"}),
    ]
    async with httpx.AsyncClient(timeout=12, headers=headers) as c:
        fact, last_code = None, None
        for url, params in endpoints:
            r = await c.get(url, params=params)
            last_code = r.status_code
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                fact = (r.json() or {}).get("fact") or {}
                if fact:
                    break
                fact = None
        if not fact:
            raise RuntimeError(f"Яндекс Погода не ответила (HTTP {last_code})")
    cond = fact.get("condition")
    return finalize_wx(
        "Яндекс Погода 🇷🇺",
        fact.get("temp"), fact.get("feels_like"), fact.get("humidity"),
        fact.get("prec_mm"), fact.get("wind_speed"),
        YA_COND.get(cond, cond or "—"),
    )


async def wx_meteoinfo(city_name: str) -> dict:
    """Гидрометцентр России (meteoinfo.ru) — поиск города, затем страница погоды."""
    src = "Гидрометцентр РФ 🇷🇺"
    base = "https://meteoinfo.ru"
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True,
        headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"},
    ) as c:
        r = await c.get(f"{base}/pogoda/search", params={"q": city_name})
        if r.status_code != 200:
            raise RuntimeError(f"{src}: поиск недоступен (HTTP {r.status_code})")
        soup = BeautifulSoup(r.text, "lxml")
        needle = city_name.lower().replace("ё", "е")
        city_url = None
        for a in soup.find_all("a", href=True):
            txt = a.get_text(" ", strip=True).lower().replace("ё", "е")
            if "/pogoda/" in a["href"] and needle in txt:
                city_url = urljoin(base, a["href"])
                break
        if not city_url:
            raise RuntimeError(f"{src}: город «{city_name}» не найден поиском")
        r = await c.get(city_url)
        r.raise_for_status()
        page_text = BeautifulSoup(r.text, "lxml").get_text(" ")
    p = parse_current_weather(page_text)
    return finalize_wx(src, p["temp"], p["feels"], p["hum"], None,
                       p["wind"], p["desc"], p["pressure"])


async def wx_world_weather(city_name: str) -> dict:
    """МирПогоды (world-weather.ru) — прямые варианты URL со слагом города."""
    src = "МирПогоды 🇷🇺"
    base = "https://world-weather.ru"
    city = city_name.strip()
    slugs = [
        quote(city.title().replace(" ", "-"), safe="-"),
        quote(city.lower().replace(" ", "-"), safe="-"),
    ]
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True,
        headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"},
    ) as c:
        page = None
        for slug in slugs:
            for pattern in (f"/погода-в-{slug}/", f"/pogoda-v-{slug}/"):
                try:
                    r = await c.get(base + pattern)
                    if r.status_code == 200:
                        page = r.text
                        break
                except httpx.HTTPError:
                    continue
            if page:
                break
        if page is None:
            raise RuntimeError(f"{src}: страница города «{city_name}» не найдена")
    p = parse_current_weather(BeautifulSoup(page, "lxml").get_text(" "))
    return finalize_wx(src, p["temp"], p["feels"], p["hum"], None,
                       p["wind"], p["desc"], p["pressure"])


async def wx_rp5(city_name: str) -> dict:
    """РП5 (rp5.ru) — поиск города на сайте, затем страница погоды."""
    src = "РП5 🇷🇺"
    base = "https://rp5.ru"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": UA}) as c:
        r = await c.get(f"{base}/search", params={"name": city_name})
        if r.status_code != 200:
            raise RuntimeError(f"{src}: поиск недоступен (HTTP {r.status_code})")
        soup = BeautifulSoup(r.text, "lxml")
        city_url = None
        for a in soup.find_all("a", href=True):
            href_dec = unquote(a["href"]).lower()
            label = a.get_text(" ", strip=True).lower()
            if "погода_в_" in href_dec or "погода в " in label:
                city_url = urljoin(base, a["href"])
                break
        if not city_url:
            # запасной вариант: прямой URL «Погода_в_<Город>»
            city_url = f"{base}/{quote('Погода_в_' + city_name.strip().title(), safe='')}"
        r = await c.get(city_url)
        r.raise_for_status()
        page_text = BeautifulSoup(r.text, "lxml").get_text(" ")
    p = parse_current_weather(page_text)
    return finalize_wx(src, p["temp"], p["feels"], p["hum"], None,
                       p["wind"], p["desc"], p["pressure"])


async def wx_open_meteo(lat: float, lon: float) -> dict:
    """Аварийный запас (не русский) — включается env FALLBACK_OPEN_METEO=1."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                           "precipitation,weather_code,wind_speed_10m",
                "wind_speed_unit": "ms",
            },
        )
        r.raise_for_status()
        cur = r.json()["current"]
    return finalize_wx(
        "Open-Meteo (аварийный запас)",
        cur["temperature_2m"], cur["apparent_temperature"], cur["relative_humidity_2m"],
        cur["precipitation"], cur["wind_speed_10m"],
        WMO.get(cur["weather_code"], "—"),
    )


async def get_weather(lat: float, lon: float, city_name: str) -> list[dict]:
    """Все русские источники параллельно; упавшие/битые пропускаются."""
    tasks: dict[str, asyncio.Task] = {}
    if YANDEX_KEY:
        tasks["Яндекс Погода"] = wx_yandex(lat, lon)
    tasks["Гидрометцентр РФ"] = wx_meteoinfo(city_name)
    tasks["МирПогоды"] = wx_world_weather(city_name)
    tasks["РП5"] = wx_rp5(city_name)

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    wx_list = []
    for (name, _), res in zip(tasks.items(), results):
        if isinstance(res, Exception):
            log.warning("Источник «%s» пропущен: %s", name, res)
        else:
            wx_list.append(res)

    if not wx_list and FALLBACK_OPEN_METEO:
        log.warning("Русские источники не ответили — включён аварийный Open-Meteo")
        try:
            wx_list = [await wx_open_meteo(lat, lon)]
        except Exception as e:
            log.warning("Аварийный источник тоже упал: %s", e)
    return wx_list

# ------------------------------- GROQ -------------------------------

SYSTEM_PROMPT = """Ты — «Фит-мастер»: топовый стилист + уличный модник + практика.
На вход получаешь погоду из нескольких источников для одного города.
Источники: Яндекс Погода (если есть), Гидрометцентр России (официальный),
МирПогоды, РП5. Если данные расходятся — ориентируйся на большинство и здравый смысл.

Собери ОДИН бомбовский фит (образ на сегодня), который одновременно:
1) реально подходит под погоду: температура и «ощущается как», осадки, ветер, влажность;
2) выглядит вкусно: продуманная палитра (цвета сочетаются, но не скучно),
   микс фактур и материалов, многослойность, интересный силуэт;
3) практичен: в нём удобно жить весь день.

ГАРДЕРОБ: у пользователя есть список его реальных вещей (распознаны по фото).
Приоритет — вписать подходящие по погоде и стилю вещи из его гардероба;
у таких вещей ставь "from_wardrobe": true. Если для идеального образа чего-то
не хватает — добавь с "from_wardrobe": false и в "why" подскажи, что докупить.

Правила:
- Цвета конкретно: не «тёмный», а «графитово-серый», «оливковый», «кремово-бежевый».
- Материалы под погоду: меринос, плотный хлопок, нейлон, вельвет, кожа, замша, деним, футер…
- В items — 4-7 вещей: верх, низ, слои по погоде. Плюс обувь и 1-3 аксессуара.
- Тон: живой, с вайбом, но по делу. Всё на русском.

Отвечай СТРОГО одним JSON-объектом без markdown и текста вокруг:
{
  "summary": "1-2 предложения о погоде сегодня",
  "items": [{"name":"предмет","color":"цвет","material":"материал","why":"почему он тут, коротко","from_wardrobe":true}],
  "shoes": {"name":"модель","color":"цвет","material":"материал","why":"почему"},
  "accessories": [{"name":"аксессуар","color":"цвет","why":"зачем"}],
  "vibe": "1-2 предложения про вайб образа",
  "tip": "практический совет (зонт, водостойкая обувь, термобельё…)"
}
"""

STYLE_PROMPT = """Ты — «Фит-мастер», топовый стилист. Тебе дают название стиля.
Сделай разбор стиля так, чтобы человек мог собрать образ с нуля.

Отвечай СТРОГО одним JSON-объектом без markdown и текста вокруг:
{
  "style": "название стиля",
  "essence": "суть стиля в 1-2 предложениях",
  "palette": ["3-5 конкретных цветов"],
  "items": [{"name":"базовая вещь","color":"цвет","material":"материал","note":"коротко зачем"}],
  "how_to_wear": "как носить и миксовать, 2-3 предложения",
  "do": "главное правило, что делать",
  "dont": "главная ошибка, чего избегать"
}
На русском, живо и по делу. В items — 5-7 вещей."""

VISION_PROMPT = """Ты — ассистент гардероба. Пользователь прислал фото одной вещи
одежды/обуви/аксессуара. Определи, что это, и опиши её.
Отвечай СТРОГО одним JSON без markdown и текста вокруг:
{"name":"что это (напр. бомбер, худи, кроссовки)","type":"категория: верх / низ / верхняя одежда / обувь / аксессуар","color":"конкретный цвет","material":"материал","style":"стиль, к которому тяготеет","season":"тёплый / холодный / всесезонный","note":"короткая заметка"}
На русском."""

STYLES = [
    "streetwear", "old money", "techwear", "gorpcore", "минимализм",
    "y2k", "smart casual", "dark academia", "russian winter core",
]


def parse_llm_json(raw: str) -> dict:
    """Терпимый парсинг: срезает -блоки, умеет доставать {...} из текста."""
    raw = (raw or "").strip()
    if raw.startswith(""):
        raw = raw[3:]
        if raw[:4].lower() == "json":
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


async def groq_json(system: str, user: str, *, temperature: float = 0.8,
                    max_tokens: int = 1600, image_b64: str | None = None,
                    model: str | None = None) -> dict:
    """Вызов Groq с 2 попытками (на битый JSON / сетевые сбои)."""
    model = model or (GROQ_VISION_MODEL if image_b64 else GROQ_MODEL)
    messages: list = []
    if system:
        messages.append({"role": "system", "content": system})
    if image_b64:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": user},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]})
    else:
        messages.append({"role": "user", "content": user})

    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            kwargs = {} if image_b64 else {"response_format": {"type": "json_object"}}
            resp = await groq_client.chat.completions.create(
                model=model, messages=messages,
                temperature=temperature, max_tokens=max_tokens, **kwargs,
            )
            return parse_llm_json(resp.choices[0].message.content or "{}")
        except Exception as e:
            last_exc = e
            log.warning("Groq: попытка %d не удалась: %s", attempt, e)
    raise last_exc


def wx_to_text(w: dict) -> str:
    parts = [f"- {w['src']}: {w['temp']:+.0f}°C"]
    if w.get("feels") is not None:
        parts.append(f"ощущается {w['feels']:+.0f}°C")
    if w.get("hum") is not None:
        parts.append(f"влажность {w['hum']:.0f}%")
    if w.get("precip"):
        parts.append(f"осадки {w['precip']:.1f} мм")
    if w.get("wind"):
        parts.append(f"ветер {w['wind']:.0f} м/с")
    if w.get("pressure"):
        parts.append(f"давление {w['pressure']:.0f} мм рт.ст.")
    parts.append(f"состояние: {w['desc']}")
    return ", ".join(parts)


def wardrobe_to_text(items: list[dict]) -> str:
    return "\n".join(
        f"{i}. {it.get('name', '—')} — {it.get('color', '?')}, {it.get('material', '?')}, "
        f"тип: {it.get('type', '?')}, сезон: {it.get('season', '?')}, стиль: {it.get('style', '?')}"
        for i, it in enumerate(items, 1)
    )

# --------------------------- ФОРМАТИРОВАНИЕ ---------------------------

def fmt_w(w: dict) -> str:
    parts = [f"• <b>{html.escape(str(w['src']))}</b>: {w['temp']:+.0f}°"]
    if w.get("feels") is not None:
        parts.append(f"ощущается как <b>{w['feels']:+.0f}°</b>")
    parts.append(html.escape(str(w["desc"])))
    if w.get("wind"):
        parts.append(f"ветер {w['wind']:.0f} м/с")
    if w.get("precip"):
        parts.append(f"осадки {w['precip']:.1f} мм")
    return ", ".join(parts)


def build_fit_message(city: str, wx_list: list[dict], fit: dict) -> str:
    out = [f"🔥 <b>Фит на сегодня — {html.escape(city)}</b>", "",
           "<b>🇷🇺 Погода (русские источники)</b>"]
    out += [fmt_w(w) for w in wx_list]
    out.append("")
    if fit.get("summary"):
        out += [f"💬 <i>{html.escape(str(fit['summary']))}</i>", ""]
    out.append("<b>👕 Образ:</b>")
    for i, it in enumerate(fit.get("items") or [], 1):
        tag = " 🏷 <i>из твоего гардероба</i>" if it.get("from_wardrobe") else ""
        line = (f"{i}. {html.escape(str(it.get('name', '—')))} — "
                f"<b>{html.escape(str(it.get('color', '')))}</b> "
                f"({html.escape(str(it.get('material', '')))}){tag}")
        if it.get("why"):
            line += f"\n    <i>{html.escape(str(it['why']))}</i>"
        out.append(line)
    shoes = fit.get("shoes") or {}
    if shoes.get("name"):
        out.append(f"👟 Обувь: {html.escape(str(shoes['name']))} — "
                   f"<b>{html.escape(str(shoes.get('color', '')))}</b> "
                   f"({html.escape(str(shoes.get('material', '')))})")
    if fit.get("accessories"):
        out.append("<b>🎩 Аксессуары:</b>")
        for a in fit["accessories"]:
            line = f"• {html.escape(str(a.get('name', '—')))} — <b>{html.escape(str(a.get('color', '')))}</b>"
            if a.get("why"):
                line += f" — <i>{html.escape(str(a['why']))}</i>"
            out.append(line)
    if fit.get("vibe"):
        out += ["", f"✨ <b>Вайб:</b> {html.escape(str(fit['vibe']))}"]
    if fit.get("tip"):
        out.append(f"💡 <b>Совет:</b> {html.escape(str(fit['tip']))}")
    return "\n".join(out)


def build_style_text(style: str, data: dict) -> str:
    out = [
        f"🧪 <b>Стиль: {html.escape(str(data.get('style', style)))}</b> "
        "<i>(бета — без привязки к погоде)</i>",
        "",
        f"<i>{html.escape(str(data.get('essence', '')))}</i>",
    ]
    if data.get("palette"):
        out += ["", "<b>🎨 Палитра:</b> " + ", ".join(html.escape(str(c)) for c in data["palette"])]
    if data.get("items"):
        out += ["", "<b>👕 База стиля:</b>"]
        for it in data["items"]:
            out.append(f"• {html.escape(str(it.get('name', '—')))} — "
                       f"<b>{html.escape(str(it.get('color', '')))}</b> "
                       f"({html.escape(str(it.get('material', '')))})")
    for key, icon, title in (("how_to_wear", "🧩", "Как носить"),
                             ("do", "✅", "Делай"), ("dont", "🚫", "Не делай")):
        if data.get(key):
            out += ["", f"{icon} <b>{title}:</b> {html.escape(str(data[key]))}"]
    q = quote_plus(f"{style} outfit")
    out += ["", "<b>🔗 Инспо из инета:</b>",
            f"• Pinterest: https://pinterest.com/search/pins/?q={q}",
            f"• Google Images: https://www.google.com/search?tbm=isch&q={q}",
            f"• YouTube: https://www.youtube.com/results?search_query={q}",
            "", "🧪 <i>Фича в бете: сборка стиля без погоды. Чтобы одеться хорошо "
            "прямо сейчас — /fit (погода + стиль вместе) 💪</i>"]
    return "\n".join(out)


async def get_inspo_photos(query: str, n: int = 3) -> list[str]:
    if not PEXELS_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(
                "https://api.pexels.com/v1/search",
                params={"query": query, "per_page": n},
                headers={"Authorization": PEXELS_KEY},
            )
            r.raise_for_status()
            return [p["src"]["large"] for p in r.json().get("photos", [])]
    except Exception as e:
        log.warning("Pexels недоступен: %s", e)
        return []


async def deliver_to_pm(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        text: str, photos: list[str] | None = None):
    """Кидаем разбор стиля в личку (сначала текст, потом фото)."""
    uid = update.effective_user.id
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        for ph in photos or []:
            try:
                await context.bot.send_photo(uid, ph)
            except Exception:
                pass
        return
    try:
        await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text(
            "Не могу написать тебе в ЛС 😢 Открой личку со мной, нажми /start, "
            "потом попробуй снова."
        )
        return
    for ph in photos or []:
        try:
            await context.bot.send_photo(uid, ph)
        except Exception:
            pass
    await update.message.reply_text("📬 Кинул стиль тебе в личку!")

# ------------------------------ ХЕНДЛЕРЫ ------------------------------

HELP_TEXT = (
    "🧥 <b>Фит-мастер</b> — русские источники погоды + ИИ Groq = бомбовские фиты.\n\n"
    "<b>🇷🇺 Погода:</b> Яндекс + Гидрометцентр России + МирПогоды + РП5\n\n"
    "<b>Команды (латиницей) или просто слова:</b>\n"
    "/city Москва или «город Москва» — сохранить город\n"
    "/fit или «фит» / «погода» / «что надеть» — фит под погоду + закреп ✅\n"
    "/style streetwear или «стиль streetwear» — разбор стиля в ЛС 🧪 бета\n"
    "/wardrobe или «гардероб» — мой гардероб\n"
    "/delw 3 — удалить вещь №3\n"
    "/clearw — очистить гардероб\n\n"
    "📸 <b>Пришли фото вещи</b> — распознаю и добавлю в гардероб 🏷\n\n"
    "🧪 Сборка стиля без погоды — бета. Стабильно работает /fit 👌"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sub(update, context):
        return
    await update.message.reply_text(
        "👋 Привет! Я Фит-мастер — собираю бомбовские фиты по погоде 🧥\n\n" + HELP_TEXT,
        parse_mode=ParseMode.HTML,
    )


async def save_city(update: Update, context: ContextTypes.DEFAULT_TYPE, city_name: str):
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        geo = await geocode(city_name)
    except Exception:
        log.exception("geocode failed")
        geo = None
    context.user_data["awaiting_city"] = False
    if not geo:
        await update.message.reply_text(f"Не нашёл город «{city_name}» 🤔 Проверь написание.")
        return
    context.user_data["geo"] = geo
    await update.message.reply_text(
        f"✅ Город сохранён: <b>{html.escape(geo['display'])}</b>\n"
        "Теперь жми /fit 🔥",
        parse_mode=ParseMode.HTML,
    )


async def cmd_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sub(update, context):
        return
    if context.args:
        await save_city(update, context, " ".join(context.args))
    else:
        context.user_data["awaiting_city"] = True
        await update.message.reply_text("Напиши название города (Москва, Сочи, Astana…):")


async def cmd_fit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sub(update, context):
        return
    geo = context.user_data.get("geo")
    if not geo:
        await update.message.reply_text("Сначала скажи, где ты: /city <город>")
        return
    status = await update.message.reply_text("🇷🇺 Меряю погоду (Яндекс + Гидрометцентр + МирПогоды + РП5)…")
    try:
        wx_list = await get_weather(geo["lat"], geo["lon"], geo["name"])
        if not wx_list:
            await status.edit_text(
                "🪦 Сейчас не смог получить погоду ни с одного русского источника.\n"
                "💡 Что помогает: добавить YANDEX_WEATHER_KEY (Яндекс Погода) "
                "или включить FALLBACK_OPEN_METEO=1.\nПопробуй чуть позже."
            )
            return
        await status.edit_text(f"🧠 Погода собрана ({len(wx_list)} ист.). Groq придумывает бомбовский фит…")
        wardrobe = context.user_data.get("wardrobe") or []
        fit = await groq_json(SYSTEM_PROMPT, _fit_user_content(geo["display"], wx_list, wardrobe))
        if not (fit.get("items") or fit.get("shoes")):
            raise RuntimeError("Groq вернул пустой фит")
        text = build_fit_message(geo["display"], wx_list, fit)
        final = await update.message.reply_text(text, parse_mode=ParseMode.HTML)

        # закреп: снимаем прошлый, ставим новый
        chat_id = update.effective_chat.id
        prev = context.user_data.get("pinned_msg_id")
        if prev:
            try:
                await context.bot.unpin_chat_message(chat_id, message_id=prev)
            except Exception:
                pass
        try:
            await final.pin(disable_notification=True)
            context.user_data["pinned_msg_id"] = final.message_id
        except Exception:
            await update.message.reply_text("Не смог закрепить фит 😕 В группе мне нужны права админа.")
        await status.delete()
    except Exception:
        log.exception("fit failed")
        await status.edit_text("Что-то сломалось при сборке фита 😵 Попробуй ещё раз.")


def _fit_user_content(city: str, wx_list: list[dict], wardrobe: list[dict]) -> str:
    content = f"Город: {city}\n\nПогода (русские источники):\n"
    content += "\n".join(wx_to_text(w) for w in wx_list) + "\n"
    if wardrobe:
        content += f"\nГардероб пользователя:\n{wardrobe_to_text(wardrobe)}\n"
    content += "\nСобери фит."
    return content


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sub(update, context):
        return
    status = await update.message.reply_text("👀 Смотрю вещь через ИИ…")
    try:
        photo = update.message.photo[-1]
        if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
            await status.edit_text("Фото слишком большое 📸 Пришли поменьше.")
            return
        file = await context.bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        b64 = base64.b64encode(buf.getvalue()).decode()

        item = await groq_json(VISION_PROMPT, "Опиши вещь на фото.", image_b64=b64)
        wardrobe = context.user_data.setdefault("wardrobe", [])
        wardrobe.append(item)
        while len(wardrobe) > MAX_WARDROBE:
            wardrobe.pop(0)

        await status.edit_text(
            "✅ Добавил в гардероб:\n"
            f"🧥 <b>{html.escape(str(item.get('name', '—')))}</b> — "
            f"{html.escape(str(item.get('color', '')))}, "
            f"{html.escape(str(item.get('material', '')))}\n"
            f"<i>{html.escape(str(item.get('note', '')))}</i>\n\n"
            f"Вещей: <b>{len(wardrobe)}</b>. Жми /fit — встрою в образ 🏷",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        log.exception("vision failed")
        await status.edit_text("Не смог разобрать фото 😵 Попробуй другую фотку (покрупнее, поярче).")


async def cmd_wardrobe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sub(update, context):
        return
    wardrobe = context.user_data.get("wardrobe") or []
    if not wardrobe:
        await update.message.reply_text("Гардероб пуст 👀 Пришли фото вещи — распознаю и добавлю.")
        return
    lines = ["👕 <b>Твой гардероб:</b>", ""]
    for i, it in enumerate(wardrobe, 1):
        lines.append(
            f"{i}. <b>{html.escape(str(it.get('name', '—')))}</b> — "
            f"{html.escape(str(it.get('color', '')))}, "
            f"{html.escape(str(it.get('material', '')))} "
            f"<i>({html.escape(str(it.get('season', '')))})</i>"
        )
    lines += ["", "🗑 /delw &lt;номер&gt; — удалить · /clearw — очистить всё"]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_delw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sub(update, context):
        return
    wardrobe = context.user_data.get("wardrobe") or []
    if (not context.args or not context.args[0].isdigit()
            or not (1 <= int(context.args[0]) <= len(wardrobe))):
        await update.message.reply_text("Использование: /delw <номер вещи из /wardrobe>")
        return
    removed = wardrobe.pop(int(context.args[0]) - 1)
    await update.message.reply_text(f"🗑 Убрал: {html.escape(str(removed.get('name', '—')))}")


async def cmd_clearw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sub(update, context):
        return
    context.user_data["wardrobe"] = []
    await update.message.reply_text("🧹 Гардероб очищен.")


async def cmd_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_sub(update, context):
        return
    style = " ".join(context.args).strip() or random.choice(STYLES)
    status = await update.message.reply_text(f"🧪 Собираю разбор стиля «{html.escape(style)}»… (бета)")
    try:
        data = await groq_json(STYLE_PROMPT, f"Стиль: {style}",
                               temperature=0.8, max_tokens=1200)
        text = build_style_text(style, data)
        photos = await get_inspo_photos(f"{style} fashion outfit")
        await deliver_to_pm(update, context, text, photos)
        await status.delete()
    except Exception:
        log.exception("style failed")
        await status.edit_text("Стиль не собрался 😵 (ну, бета же 🧪). Попробуй ещё раз.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обычные сообщения: ждём город, ловим русские слова-алиасы."""
    if not await require_sub(update, context):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    # ждём название города после /city без аргумента
    if context.user_data.get("awaiting_city"):
        await save_city(update, context, text)
        return

    # русские слова-алиасы (команды в Telegram только латиницей)
    low = text.lower()
    if low in ("фит", "погода", "что надеть", "что одеть", "одеться"):
        await cmd_fit(update, context)
        return
    if low == "стиль":
        await cmd_style(update, context)
        return
    if low.startswith("стиль "):
        context.args = text.split()[1:]
        await cmd_style(update, context)
        return
    if low == "гардероб":
        await cmd_wardrobe(update, context)
        return
    if low.startswith(("город ", "city ")):
        await save_city(update, context, " ".join(text.split()[1:]))
        return


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Необработанная ошибка: %s", context.error, exc_info=context.error)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    # ВАЖНО: команды Telegram — только латиница a-z, 0-9, _
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler(["city", "setcity"], cmd_city))
    app.add_handler(CommandHandler(["fit", "outfit", "weather"], cmd_fit))
    app.add_handler(CommandHandler(["style"], cmd_style))
    app.add_handler(CommandHandler(["wardrobe"], cmd_wardrobe))
    app.add_handler(CommandHandler(["delw"], cmd_delw))
    app.add_handler(CommandHandler(["clearw"], cmd_clearw))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(cb_check_sub, pattern="^check_sub$"))
    app.add_error_handler(on_error)
    log.info("Фит-мастер v2.3 запущен 🧥🇷🇺 | Яндекс: %s | канал: %s | аварийный запас: %s",
             "вкл" if YANDEX_KEY else "выкл", CHANNEL or "выкл",
             "вкл" if FALLBACK_OPEN_METEO else "выкл")
    app.run_polling()


if __name__ == "__main__":
    main()
