"""
Концертный бот: раз в неделю ищет концерты выбранных исполнителей
в зонах Барселона / Санкт-Петербург / Москва и присылает только новое.

Источники:
  venue  — афиша площадки/фестиваля: ищем имена исполнителей;
  artist — страница туров исполнителя: ищем города из зон;
  list   — общий список туров (лейбл и т.п.): нужны и имя, и город;
  KudaGo — API (СПб, Москва), встроенный;
  Ticketmaster — API (Испания), если задан TM_API_KEY.
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- настройки ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
TM_API_KEY = os.environ.get("TM_API_KEY", "")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Madrid"))
CHECK_WEEKDAY = int(os.environ.get("CHECK_WEEKDAY", "1"))  # 1=пн … 7=вс
CHECK_HOUR = int(os.environ.get("CHECK_HOUR", "10"))

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("concerts")

ZONES = {
    "Барселона": ["Barcelona", "Barcelone", "Girona", "Gerona", "Tarragona", "Terrassa",
                  "Sabadell", "Granollers", "Mataró", "Mataro", "Badalona", "Reus",
                  "Sitges", "Figueres", "Lleida", "Hospitalet"],
    "Санкт-Петербург": ["Санкт-Петербург", "Петербург", "СПб", "St. Petersburg",
                        "St Petersburg", "Saint Petersburg", "Saint-Petersburg",
                        "Sankt-Peterburg"],
    "Москва": ["Москв", "Moscow", "Moskau", "Moscou", "Moscú"],
}
KUDAGO_LOCATIONS = {"spb": "Санкт-Петербург", "msk": "Москва"}
TM_CENTER = ("41.3874,2.1686", 150)  # Барселона, радиус км

SEED_ARTISTS = {
    "Shai Maestro": ["Shai Maestro", "Шай Маэстро"],
    "Brad Mehldau": ["Brad Mehldau", "Mehldau", "Брэд Мелдау", "Мелдау"],
    "illo.trio": ["illo.trio", "illo trio", "illotrio"],
    "Ilugdin Trio": ["Ilugdin", "Илугдин", "Илюгдин"],
    "Marc Mezquida": ["Marc Mezquida", "Mezquida"],
}
# (kind, url, artist, zone) — стартовый список; проверяется командой /test
SEED_SOURCES = [
    ("artist", "https://ilugdin.ru/", "Ilugdin Trio", None),
    ("artist", "https://illotrio.com/", "illo.trio", None),
    ("artist", "https://www.shaimaestro.com/", "Shai Maestro", None),
    ("artist", "https://www.bradmehldau.com/", "Brad Mehldau", None),
    ("list", "https://www.nonesuch.com/on-tour", None, None),
    ("venue", "https://kozlovclub.ru/", None, "Москва"),
    ("venue", "https://www.mmdm.ru/", None, "Москва"),
    ("venue", "https://jfc-club.spb.ru/", None, "Санкт-Петербург"),
    ("venue", "https://www.jamboreejazz.com/", None, "Барселона"),
    ("venue", "https://harlemjazzclub.es/", None, "Барселона"),
    ("venue", "https://www.palaumusica.cat/", None, "Барселона"),
    ("venue", "https://www.barcelonajazzfestival.com/", None, "Барселона"),
    ("venue", "https://www.auditori.cat/", None, "Барселона"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------- распознавание ----------
_M = (r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|ene|abr|ago|dic|gen|"
      r"maig|juny|set|des|январ|феврал|март|апрел|ма[яй]|июн|июл|август|"
      r"сентябр|октябр|ноябр|декабр)")
DATE_RE = re.compile(
    rf"\b\d{{1,2}}[./-]\d{{1,2}}\b|\b\d{{1,2}}\s*(?:de\s+|d'|d’)?{_M}|"
    rf"\b{_M}\w*\.?\s+\d{{1,2}}\b|\b20\d\d-\d\d-\d\d\b",
    re.IGNORECASE,
)


def kw_pattern(words):
    parts = []
    for w in words:
        w = w.strip()
        if not w:
            continue
        esc = re.escape(w)
        # кириллица склоняется — хвост не ограничиваем
        if re.search(r"[а-яё]", w, re.IGNORECASE):
            parts.append(rf"(?<!\w){esc}")
        else:
            parts.append(rf"(?<!\w){esc}(?!\w)")
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


ZONE_RES = {z: kw_pattern(ws) for z, ws in ZONES.items()}


def find_zone(text):
    for z, rx in ZONE_RES.items():
        if rx.search(text):
            return z
    return None


def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def fp(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


@dataclass
class Hit:
    artist: str
    zone: str
    text: str
    url: str
    key: str


def scan(text, kind, url, src_artist, src_zone, artists):
    """artists: {name: compiled_regex}"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    hits = []
    for i, line in enumerate(lines):
        window = " ".join(lines[max(0, i - 2): i + 3])
        if not DATE_RE.search(window):
            continue
        dates = " ".join(m.group(0) for m in DATE_RE.finditer(window))
        if kind == "venue":
            for name, rx in artists.items():
                if rx.search(line):
                    hits.append(Hit(name, src_zone or "?", window[:400], url,
                                    fp(url, name, norm(line), norm(dates))))
        elif kind == "artist":
            z = find_zone(line)
            if z and src_artist:
                hits.append(Hit(src_artist, z, window[:400], url,
                                fp(url, src_artist, norm(line), norm(dates))))
        elif kind == "list":
            z = find_zone(line)
            if z:
                for name, rx in artists.items():
                    if rx.search(window):
                        hits.append(Hit(name, z, window[:400], url,
                                        fp(url, name, norm(line), norm(dates))))
    return hits


# ---------- база ----------
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = sqlite3.connect(DATA_DIR / "concerts.db", check_same_thread=False)
DB.executescript("""
CREATE TABLE IF NOT EXISTS artists(name TEXT PRIMARY KEY, aliases TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, url TEXT NOT NULL UNIQUE, artist TEXT, zone TEXT);
CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY, ts TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
""")


def meta_get(k):
    r = DB.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def meta_set(k, v):
    DB.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    DB.commit()


def seed():
    if meta_get("seeded"):
        return
    for n, a in SEED_ARTISTS.items():
        DB.execute("INSERT OR IGNORE INTO artists VALUES(?,?)", (n, json.dumps(a, ensure_ascii=False)))
    for k, u, a, z in SEED_SOURCES:
        DB.execute("INSERT OR IGNORE INTO sources(kind,url,artist,zone) VALUES(?,?,?,?)", (k, u, a, z))
    meta_set("seeded", 1)


def get_artists():
    return {n: json.loads(a) for n, a in DB.execute("SELECT name, aliases FROM artists ORDER BY name")}


def get_sources():
    return DB.execute("SELECT id, kind, url, artist, zone FROM sources ORDER BY id").fetchall()


def owner():
    if OWNER_ID:
        return OWNER_ID
    v = meta_get("owner")
    return int(v) if v else None


# ---------- сбор данных ----------
async def fetch_text(client, url):
    r = await client.get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return soup.get_text("\n")


async def kudago(client, artists):
    hits = []
    now = datetime.now(timezone.utc).timestamp()
    for name, aliases in artists.items():
        rx = kw_pattern(aliases)
        for alias in aliases:
            for loc, zone in KUDAGO_LOCATIONS.items():
                r = await client.get("https://kudago.com/public-api/v1.4/search/",
                                     params={"q": alias, "ctype": "event", "location": loc,
                                             "lang": "ru", "page_size": 20})
                r.raise_for_status()
                for it in r.json().get("results", []):
                    blob = f"{it.get('title', '')} {it.get('description', '')}"
                    if not rx.search(blob):
                        continue
                    dr = it.get("daterange") or {}
                    end = dr.get("end") or dr.get("start")
                    if end and end < now:
                        continue
                    when = ""
                    if dr.get("start"):
                        when = datetime.fromtimestamp(dr["start"], TZ).strftime("%d.%m.%Y %H:%M")
                    url = it.get("item_url") or it.get("site_url") or "https://kudago.com/"
                    title = BeautifulSoup(it.get("title", ""), "html.parser").get_text()
                    hits.append(Hit(name, zone, f"{when} — {title}".strip(" —"), url,
                                    fp("kudago", str(it.get("id")), name)))
    return hits


async def ticketmaster(client, artists):
    if not TM_API_KEY:
        return []
    hits = []
    for name, aliases in artists.items():
        r = await client.get("https://app.ticketmaster.com/discovery/v2/events.json",
                             params={"apikey": TM_API_KEY, "keyword": aliases[0],
                                     "latlong": TM_CENTER[0], "radius": TM_CENTER[1],
                                     "unit": "km", "size": 50})
        r.raise_for_status()
        for ev in r.json().get("_embedded", {}).get("events", []):
            venue = (ev.get("_embedded", {}).get("venues") or [{}])[0]
            txt = (f"{ev.get('dates', {}).get('start', {}).get('localDate', '')} — "
                   f"{ev.get('name', '')}, {venue.get('name', '')}, "
                   f"{venue.get('city', {}).get('name', '')}")
            hits.append(Hit(name, "Барселона", txt, ev.get("url", ""), fp("tm", ev.get("id", ""))))
    return hits


async def collect():
    artists = get_artists()
    rxs = {n: kw_pattern(a) for n, a in artists.items()}
    hits, errors = [], []
    sem = asyncio.Semaphore(5)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": UA}) as client:
        async def one(src):
            sid, kind, url, a, z = src
            async with sem:
                try:
                    text = await fetch_text(client, url)
                    hits.extend(scan(text, kind, url, a, z, rxs))
                except Exception as e:
                    errors.append(f"#{sid} {url}: {type(e).__name__}")

        await asyncio.gather(*(one(s) for s in get_sources()))
        for label, fn in (("KudaGo", kudago), ("Ticketmaster", ticketmaster)):
            try:
                hits.extend(await fn(client, artists))
            except Exception as e:
                errors.append(f"{label}: {type(e).__name__}")
    return hits, errors


def take_new(hits):
    new, keys = [], set()
    for h in hits:
        if h.key in keys:
            continue
        keys.add(h.key)
        if not DB.execute("SELECT 1 FROM seen WHERE key=?", (h.key,)).fetchone():
            new.append(h)
            DB.execute("INSERT INTO seen VALUES(?,?)", (h.key, datetime.now(TZ).isoformat()))
    DB.commit()
    return new


def render(new):
    by = {}
    for h in new:
        by.setdefault(h.artist, []).append(h)
    out = []
    for artist, hs in by.items():
        out.append(f"🎹 <b>{html.escape(artist)}</b>")
        for h in hs:
            out.append(f"📍 {html.escape(h.zone)} — {html.escape(h.text)}\n"
                       f"<a href=\"{html.escape(h.url)}\">источник</a>")
        out.append("")
    return "\n".join(out)


async def send_long(bot, chat_id, text):
    while text:
        cut = text[:3900]
        if len(text) > 3900 and "\n" in cut:
            cut = cut[:cut.rfind("\n")]
        await bot.send_message(chat_id, cut, parse_mode=ParseMode.HTML,
                               disable_web_page_preview=True)
        text = text[len(cut):].lstrip("\n")


async def run_check(bot, chat_id, manual=False):
    hits, errors = await collect()
    new = take_new(hits)
    if new:
        await send_long(bot, chat_id, "Новые анонсы:\n\n" + render(new))
    else:
        await bot.send_message(chat_id, "Новых анонсов нет." if manual
                               else "Недельная проверка: новых анонсов нет.")
    if errors:
        await bot.send_message(chat_id, "Не удалось проверить:\n" + "\n".join(errors[:30]),
                               disable_web_page_preview=True)


# ---------- команды ----------
HELP = """Команды:
/list — исполнители
/add Имя; вариант2; вариант3 — добавить (варианты написания через «;»)
/remove Имя — удалить
/sources — источники
/addsource venue URL Город — афиша площадки (город: Барселона / Санкт-Петербург / Москва)
/addsource artist URL Имя — страница туров исполнителя
/addsource list URL — общий список туров
/delsource N — удалить источник
/test — проверить, какие источники читаются
/check — проверить сейчас

Автопроверка — раз в неделю. Присылаю только то, чего не было раньше."""


def guard(fn):
    async def wrap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if owner() is None:
            meta_set("owner", uid)
        if uid != owner():
            return
        return await fn(update, ctx)
    return wrap


@guard
async def cmd_start(update, ctx):
    await update.message.reply_text("Привет! Слежу за концертами.\n\n" + HELP)


@guard
async def cmd_list(update, ctx):
    a = get_artists()
    txt = "\n".join(f"• {n} ({', '.join(v)})" for n, v in a.items()) or "Список пуст."
    await update.message.reply_text(txt)


@guard
async def cmd_add(update, ctx):
    parts = [p.strip() for p in " ".join(ctx.args).split(";") if p.strip()]
    if not parts:
        return await update.message.reply_text("Формат: /add Brad Mehldau; Брэд Мелдау")
    DB.execute("INSERT OR REPLACE INTO artists VALUES(?,?)",
               (parts[0], json.dumps(parts, ensure_ascii=False)))
    DB.commit()
    await update.message.reply_text(f"Добавил: {parts[0]}. Уже объявленное пришлю при следующей проверке (или /check).")


@guard
async def cmd_remove(update, ctx):
    name = " ".join(ctx.args).strip()
    n = DB.execute("DELETE FROM artists WHERE lower(name)=lower(?)", (name,)).rowcount
    DB.commit()
    await update.message.reply_text("Удалил." if n else "Не нашёл такого имени, см. /list")


@guard
async def cmd_sources(update, ctx):
    rows = get_sources()
    txt = "\n".join(f"#{i} [{k}] {u}" + (f" — {a}" if a else "") + (f" — {z}" if z else "")
                    for i, k, u, a, z in rows) or "Источников нет."
    txt += "\n\nВстроенные: KudaGo (СПб, Москва)" + (", Ticketmaster (Испания)" if TM_API_KEY else "")
    await update.message.reply_text(txt, disable_web_page_preview=True)


@guard
async def cmd_addsource(update, ctx):
    if len(ctx.args) < 2 or ctx.args[0] not in ("venue", "artist", "list"):
        return await update.message.reply_text("Формат: /addsource venue|artist|list URL [Город или Имя]")
    kind, url, rest = ctx.args[0], ctx.args[1], " ".join(ctx.args[2:]).strip()
    artist = zone = None
    if kind == "venue":
        zone = next((z for z in ZONES if z.lower() == rest.lower()), None)
        if not zone:
            return await update.message.reply_text("Укажите город: " + " / ".join(ZONES))
    elif kind == "artist":
        artist = next((n for n in get_artists() if n.lower() == rest.lower()), None)
        if not artist:
            return await update.message.reply_text("Укажите имя из /list")
    try:
        DB.execute("INSERT INTO sources(kind,url,artist,zone) VALUES(?,?,?,?)", (kind, url, artist, zone))
        DB.commit()
    except sqlite3.IntegrityError:
        return await update.message.reply_text("Такой источник уже есть.")
    await update.message.reply_text("Добавил. Проверить, читается ли он: /test")


@guard
async def cmd_delsource(update, ctx):
    try:
        sid = int(ctx.args[0].lstrip("#"))
    except (IndexError, ValueError):
        return await update.message.reply_text("Формат: /delsource 5")
    n = DB.execute("DELETE FROM sources WHERE id=?", (sid,)).rowcount
    DB.commit()
    await update.message.reply_text("Удалил." if n else "Нет такого номера, см. /sources")


@guard
async def cmd_test(update, ctx):
    await update.message.reply_text("Проверяю источники…")
    lines = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": UA}) as client:
        for sid, kind, url, a, z in get_sources():
            try:
                text = await fetch_text(client, url)
                n_dates = len(DATE_RE.findall(text))
                size = len(text.strip())
                mark = "✅" if size > 1500 and n_dates >= 3 else "⚠️"
                note = "" if mark == "✅" else " — мало текста или дат (афиша на другой странице или грузится скриптом)"
                lines.append(f"{mark} #{sid} {url}: {size // 1000}k симв., дат {n_dates}{note}")
            except Exception as e:
                lines.append(f"❌ #{sid} {url}: {type(e).__name__} {str(e)[:80]}")
        try:
            r = await client.get("https://kudago.com/public-api/v1.4/search/",
                                 params={"q": "джаз", "ctype": "event", "location": "msk"})
            r.raise_for_status()
            lines.append(f"✅ KudaGo: найдено {r.json().get('count', '?')} по запросу «джаз»")
        except Exception as e:
            lines.append(f"❌ KudaGo: {type(e).__name__}")
    await send_long(ctx.bot, update.effective_chat.id, html.escape("\n".join(lines)))


@guard
async def cmd_check(update, ctx):
    await update.message.reply_text("Проверяю, это займёт минуту-две…")
    await run_check(ctx.bot, update.effective_chat.id, manual=True)


async def weekly(ctx: ContextTypes.DEFAULT_TYPE):
    chat = owner()
    if chat:
        await run_check(ctx.bot, chat)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    seed()
    app = Application.builder().token(BOT_TOKEN).build()
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("list", cmd_list),
                     ("add", cmd_add), ("remove", cmd_remove), ("sources", cmd_sources),
                     ("addsource", cmd_addsource), ("delsource", cmd_delsource),
                     ("test", cmd_test), ("check", cmd_check)]:
        app.add_handler(CommandHandler(name, fn))
    # в python-telegram-bot 0 = воскресенье
    app.job_queue.run_daily(weekly, time=dtime(CHECK_HOUR, 0, tzinfo=TZ),
                            days=(CHECK_WEEKDAY % 7,))
    log.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
