import os
import logging
import asyncio
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Bot
from telegram.ext import Application
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


# === ЦБ РФ: курсы валют ===

def get_cbr_rates() -> dict:
    url = "https://www.cbr.ru/scripts/XML_daily.asp"
    r = requests.get(url, timeout=10)
    r.encoding = "windows-1251"
    root = ET.fromstring(r.text)
    rates = {}
    for valute in root.findall("Valute"):
        char_code = valute.find("CharCode").text
        value = float(valute.find("Value").text.replace(",", "."))
        nominal = int(valute.find("Nominal").text)
        if char_code in ("USD", "EUR", "GBP"):
            rates[char_code] = value / nominal
    return rates


# === ЦБ РФ: металлы ===

def get_cbr_metals_on_date(date: datetime) -> dict:
    date_str = date.strftime("%d/%m/%Y")
    url = f"https://www.cbr.ru/scripts/xml_metall.asp?date_req1={date_str}&date_req2={date_str}"
    r = requests.get(url, timeout=10)
    r.encoding = "windows-1251"
    root = ET.fromstring(r.text)
    metals = {}
    code_map = {"1": "gold", "2": "silver"}
    for record in root.findall("Record"):
        code = record.get("Code")
        if code in code_map:
            buy = record.find("Buy")
            if buy is not None and buy.text:
                metals[code_map[code]] = float(buy.text.replace(",", "."))
    return metals


def get_cbr_metals_history(today: datetime) -> dict:
    result = {"now": get_cbr_metals_on_date(today)}
    for days in (7, 14, 30, 365):
        result[f"d{days}"] = get_cbr_metals_on_date(today - timedelta(days=days))
    return result


# === CoinGecko: крипта ===

def get_crypto_on_date(date: datetime) -> dict:
    date_str = date.strftime("%d-%m-%Y")
    coins = {"bitcoin": "btc", "ethereum": "eth", "the-open-network": "ton"}
    result = {}
    for coin_id, key in coins.items():
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/history"
        price = None
        for attempt in range(3):
            try:
                r = requests.get(url, params={"date": date_str, "localization": "false"}, timeout=15)
                data = r.json()
                price = data["market_data"]["current_price"]["usd"]
                break
            except (KeyError, TypeError):
                if attempt < 2:
                    import time; time.sleep(2)
        result[key] = price
    return result


def get_crypto_history(today: datetime) -> dict:
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": "bitcoin,ethereum,the-open-network", "vs_currencies": "usd"}
    r = requests.get(url, params=params, timeout=10)
    data = r.json()
    result = {
        "now": {
            "btc": data["bitcoin"]["usd"],
            "eth": data["ethereum"]["usd"],
            "ton": data["the-open-network"]["usd"],
        }
    }
    for days in (7, 14, 30, 365):
        result[f"d{days}"] = get_crypto_on_date(today - timedelta(days=days))
    return result


# === Форматирование ===

def fmt_change(current, past) -> str:
    if past is None or past == 0:
        return "—"
    pct = (current - past) / past * 100
    icon = "🟢" if pct >= 0 else "🔴"
    sign = "+" if pct >= 0 else ""
    return f"{icon}{sign}{pct:.1f}%"


def changes_line(current, hist, key) -> str:
    c7   = fmt_change(current, hist["d7"].get(key))
    c14  = fmt_change(current, hist["d14"].get(key))
    c30  = fmt_change(current, hist["d30"].get(key))
    c365 = fmt_change(current, hist["d365"].get(key))
    return f"  7д {c7}   14д {c14}   30д {c30}   1г {c365}"


# === Сборка и отправка сводки ===

async def send_summary(bot: Bot):
    try:
        now = datetime.now(MOSCOW_TZ)
        now_str = now.strftime("%d.%m.%Y, %H:%M МСК")

        rates = get_cbr_rates()
        metals_hist = get_cbr_metals_history(now)
        crypto_hist = get_crypto_history(now)

        m = metals_hist["now"]
        c = crypto_hist["now"]

        text = (
            f"📊 <b>Сводка — {now_str}</b>\n\n"

            f"<b>──── Валюты ────────────────</b>\n"
            f"💵 USD      <code>{rates.get('USD', 0):>8.2f} ₽</code>\n"
            f"💶 EUR      <code>{rates.get('EUR', 0):>8.2f} ₽</code>\n"
            f"💷 GBP      <code>{rates.get('GBP', 0):>8.2f} ₽</code>\n\n"

            f"<b>──── Металлы ────────────────</b>\n"
            f"🥇 <b>Золото</b>   <code>{m.get('gold', 0):>10.2f} ₽/г</code>\n"
            f"{changes_line(m.get('gold', 0), metals_hist, 'gold')}\n\n"
            f"🥈 <b>Серебро</b>  <code>{m.get('silver', 0):>10.4f} ₽/г</code>\n"
            f"{changes_line(m.get('silver', 0), metals_hist, 'silver')}\n\n"

            f"<b>──── Крипта ──────────────────</b>\n"
            f"🏅 <b>Bitcoin</b>   <code>{c.get('btc', 0):>10,.0f} $</code>\n"
            f"{changes_line(c.get('btc', 0), crypto_hist, 'btc')}\n\n"
            f"💠 <b>Ethereum</b>  <code>{c.get('eth', 0):>10,.0f} $</code>\n"
            f"{changes_line(c.get('eth', 0), crypto_hist, 'eth')}\n\n"
            f"💎 <b>TON</b>       <code>{c.get('ton', 0):>10.2f} $</code>\n"
            f"{changes_line(c.get('ton', 0), crypto_hist, 'ton')}"
        )

        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
        logger.info("Summary sent successfully")

    except Exception as e:
        logger.error(f"Error sending summary: {e}", exc_info=True)
        try:
            await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Ошибка при получении данных: {e}")
        except Exception:
            pass


async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(
        send_summary,
        "cron",
        hour=14,
        minute=0,
        args=[app.bot],
    )
    scheduler.start()

    logger.info("svodkavin_bot started, waiting for 14:00 MSK daily...")

    await app.initialize()
    await app.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
