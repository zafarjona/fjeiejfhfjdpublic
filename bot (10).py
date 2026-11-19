"""
XABAR HUB — AI Newsroom (bitta fayl).

Ishga tushirish:   python bot.py
Tarmoqsiz sinov:   python bot.py --selftest

Ish jarayoni: RSS -> Fact Checker -> AI Editor -> AI rasm -> Pillow dizayn -> Telegram kanal.
Hamma narsa (kalit, kanal, sozlamalar, RSS) Telegram ichidagi admin paneldan boshqariladi.
"""
import asyncio
import base64
import calendar
import hashlib
import html
import io
import json
import logging
import math
import os
import random
import re
import sqlite3
import sys
import tempfile
import threading
import time
import warnings
from collections import Counter
from datetime import datetime, timedelta
from functools import lru_cache
from urllib.parse import quote, quote_plus, urlparse
from zoneinfo import ZoneInfo

import feedparser
import httpx
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           LinkPreviewOptions, Message)
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

# --selftest da vaqtinchalik baza ishlatiladi (config o'qilishidan OLDIN o'rnatiladi)
if "--selftest" in sys.argv:
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("xabar_hub")


# ======================================================================
# SOZLAMALAR (config)
# ======================================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ENV_ADMIN_IDS = [int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.strip().isdigit()]
DB_PATH = os.getenv("DB_PATH", "data/xabar_hub.db")
DATA_DIR = os.path.dirname(DB_PATH) or "."
# Ixtiyoriy: Turso (bulutdagi SQLite). Ikkalasi ham berilsa baza Turso'da saqlanadi (redeploy'da yo'qolmaydi).
TURSO_URL = "" if "--selftest" in sys.argv else os.getenv("TURSO_URL", "").strip()
TURSO_TOKEN = "" if "--selftest" in sys.argv else os.getenv("TURSO_TOKEN", "").strip()
IMAGE_DIR = os.path.join(DATA_DIR, "images")
TZ = "Asia/Tashkent"

PROVIDERS = ("gemini", "groq", "openrouter")
PROVIDER_TITLES = {"gemini": "Gemini", "groq": "Groq", "openrouter": "OpenRouter"}
KEY_PATTERNS = {
    "gemini": r"(?:AIza|AQ\.)[0-9A-Za-z_\-]{30,}",
    "groq": r"gsk_[0-9A-Za-z]{20,}",
    "openrouter": r"sk-or-[0-9A-Za-z_\-]{20,}",
}

CATEGORIES = ["O'ZBEKISTON", "DUNYO", "SIYOSAT", "IQTISODIYOT", "TEXNOLOGIYA", "ILM-FAN",
              "SPORT", "JAMIYAT", "MADANIYAT", "FAVQULODDA", "BOSHQA"]

SIGNATURE = "📡 <b>XABAR HUB</b> | Dunyo xabarlari — bir markazda."

DEFAULT_SETTINGS = {
    "channel_id": "",
    "channel_title": "",
    "auto_publish": "1",
    "paused": "0",
    "scan_interval": "30",
    "daily_limit": "10",
    "min_sources": "2",
    "max_age_hours": "24",
    "active_from": "0",
    "active_to": "24",
    "post_times": "",
    "posts_per_time": "1",
    "reaction_buttons": "1",
    "channel_username": "",
    "use_source_image": "0",
    "text_providers": "gemini,groq,openrouter",
    "image_providers": "gemini,pollinations",
    "gemini_model": "gemini-3.8-flash",
    "gemini_image_model": "gemini-3.1-flash-image",
    "groq_model": "llama-3.3-70b-versatile",
    "openrouter_model": "meta-llama/llama-3.3-70b-instruct:free",
    "gemini_image_off_until": "0",
    "video_min_score": "70",
    "video_min_views": "20000",
    "video_max_seconds": "300",
}

# Kontent modullari: nom -> (sarlavha, standart vaqtlar). Yangilik moduli alohida (post_times / interval).
# Har modul uchun settings'da: mod_<nom>_on (0/1) va mod_<nom>_times (HH:MM,HH:MM).
MODULES = {
    "morning": ("🌤 Ertalabki post", "08:00"),
    "history": ("📜 Bugun tarixda", "10:00"),
    "fact": ("💡 Kun fakti", ""),          # 10:00 da «Bugun tarixda» bilan navbatlashadi (Bosqich 3)
    "video": ("🎬 Video", "11:30"),
    "poll": ("🗳 Kun tanlovi", "16:00"),
    "quiz": ("🧩 Quiz", "18:00"),
    "digest": ("📋 Kunlik xulosa", "21:00"),
    "best": ("🏆 Haftaning eng yaxshi posti", "20:00"),
    "tech": ("💻 Texnologiya yangiligi", ""),
}
# «Hozir post qo'yish» menyusi tartibi (news — mavjud yangilik mexanizmi)
POST_MENU = [("news", "📰 Yangilik"), ("quiz", None), ("video", None), ("morning", None), ("history", None),
             ("fact", None), ("poll", None), ("digest", None), ("tech", None)]
for _n, (_title, _times) in MODULES.items():
    DEFAULT_SETTINGS[f"mod_{_n}_on"] = "0"
    DEFAULT_SETTINGS[f"mod_{_n}_times"] = _times


def module_title(name: str) -> str:
    return "📰 Yangilik" if name == "news" else MODULES[name][0]


TIME_RE = re.compile(r"([01]?\d|2[0-3]):[0-5]\d")


def parse_times(raw: str) -> list:
    """'09:00, 13:30' -> [(9, 0), (13, 30)] (noto'g'rilari tashlanadi, takrorlar olib tashlanadi)."""
    out = set()
    for t in re.split(r"[,\s]+", raw or ""):
        if TIME_RE.fullmatch(t):
            h, m = t.split(":")
            out.add((int(h), int(m)))
    return sorted(out)


def normalize_times(val: str):
    """Foydalanuvchi kiritgan vaqtlarni tekshiradi. '-' = bo'sh. Noto'g'ri bo'lsa None."""
    val = (val or "").strip()
    if val in ("-", "0", "off"):
        return ""
    items = [x for x in re.split(r"[,\s]+", val) if x]
    if not items or any(not TIME_RE.fullmatch(x) for x in items):
        return None
    return ",".join(f"{h:02d}:{m:02d}" for h, m in parse_times(",".join(items)))

# key: (sarlavha, turi, min, max)
SETTING_META = {
    "scan_interval": ("Qidiruv oralig'i (daqiqa)", "int", 5, 720),
    "daily_limit": ("Kunlik post limiti", "int", 1, 100),
    "min_sources": ("Kamida nechta mustaqil manba", "int", 1, 5),
    "max_age_hours": ("Yangilik eng ko'pi bilan necha soat eski", "int", 1, 72),
    "active_from": ("Faol soat: dan (0-23)", "int", 0, 23),
    "active_to": ("Faol soat: gacha (1-24)", "int", 1, 24),
    "post_times": ("Post vaqtlari (09:00,13:00,18:00; bo'sh = interval)", "times", 0, 0),
    "posts_per_time": ("Har vaqtda nechta post", "int", 1, 10),
    "auto_publish": ("Avto-nashr (o'chirilsa — admin tasdig'i)", "bool", 0, 1),
    "use_source_image": ("Manba rasmini ishlatish (AI rasm bo'lmasa)", "bool", 0, 1),
    "reaction_buttons": ("Post ostidagi tugmalar (👍 🔥 😮 + Do'stga yuborish)", "bool", 0, 1),
    "text_providers": ("Matn AI tartibi", "list", 0, 0),
    "image_providers": ("Rasm AI tartibi", "list", 0, 0),
    "gemini_model": ("Gemini matn modeli", "str", 0, 0),
    "gemini_image_model": ("Gemini rasm modeli", "str", 0, 0),
    "groq_model": ("Groq modeli", "str", 0, 0),
    "openrouter_model": ("OpenRouter modeli", "str", 0, 0),
    "video_min_score": ("Video: minimal AI ball (1-100)", "int", 1, 100),
    "video_min_views": ("Video: minimal ko'rishlar soni", "int", 0, 100000000),
    "video_max_seconds": ("Video: eng uzun davomiylik (soniya)", "int", 15, 1800),
}

# (url, til)
DEFAULT_FEEDS = [
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "en"),
    ("https://feeds.bbci.co.uk/news/technology/rss.xml", "en"),
    ("https://feeds.bbci.co.uk/news/business/rss.xml", "en"),
    ("https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "en"),
    ("https://feeds.bbci.co.uk/sport/rss.xml", "en"),
    ("https://www.aljazeera.com/xml/rss/all.xml", "en"),
    ("https://rss.dw.com/rdf/rss-en-all", "en"),
    ("https://www.theguardian.com/world/rss", "en"),
    ("https://feeds.npr.org/1001/rss.xml", "en"),
    ("https://kun.uz/news/rss", "uz"),
    ("https://daryo.uz/feed/", "uz"),
]

# Rasmiy manbalar: bitta o'zi yetarli deb hisoblanadi
OFFICIAL_DOMAINS = ["uza.uz", "president.uz", "gov.uz", "mfa.uz", "who.int", "un.org",
                    "nasa.gov", "esa.int", "europa.eu", "imf.org", "worldbank.org"]

# ======================================================================
# BAZA (database)
# ======================================================================

FAR = 4102444800  # 2100-yil


def now_ts() -> int:
    return int(time.time())


def tz_now() -> datetime:
    return datetime.now(ZoneInfo(TZ))


class TRow:
    """sqlite3.Row ga o'xshash qator (Turso/libsql tuple qaytaradi): row["nom"] va row[0] ikkalasi ishlaydi."""
    __slots__ = ("_k", "_v")

    def __init__(self, keys, values):
        self._k = {k: i for i, k in enumerate(keys)}
        self._v = tuple(values)

    def __getitem__(self, i):
        return self._v[self._k[i]] if isinstance(i, str) else self._v[i]

    def keys(self):
        return list(self._k)

    def __iter__(self):
        return iter(self._v)

    def __len__(self):
        return len(self._v)


class TCursor:
    def __init__(self, cur):
        self._cur = cur
        self.rowcount = getattr(cur, "rowcount", -1)
        self.lastrowid = getattr(cur, "lastrowid", None)

    def fetchall(self):
        cols = [d[0] for d in (self._cur.description or [])]
        return [TRow(cols, r) for r in self._cur.fetchall()]


class DB:
    def __init__(self, path: str = DB_PATH, turso: tuple | None = None):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        os.makedirs(IMAGE_DIR, exist_ok=True)
        self.lock = threading.RLock()
        self.remote = None
        turso = turso or ((TURSO_URL, TURSO_TOKEN) if TURSO_URL and TURSO_TOKEN else None)
        if turso:
            try:
                self.remote = turso
                self.conn = self._connect_remote()
                log.info("Baza: Turso (%s)", turso[0].split("//")[-1].split(".")[0])
            except Exception as e:
                self.remote = None
                log.error("Turso'ga ulanib bo'lmadi (%s: %s) — vaqtincha mahalliy baza ishlatiladi!",
                          e.__class__.__name__, e)
        if not self.remote:
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        self._init()

    def _connect_remote(self):
        import libsql  # pip install libsql
        return libsql.connect(database=self.remote[0], auth_token=self.remote[1])

    def _run(self, sql, args=()):
        """Turso uchun: bog'lanish uzilsa bir marta qayta ulanib urinadi; UNIQUE xatosini sqlite3.IntegrityError qiladi."""
        for attempt in (0, 1):
            try:
                cur = self.conn.execute(sql, args)
                self.conn.commit()
                return cur
            except Exception as e:
                msg = str(e).lower()
                if "constraint" in msg or "unique" in msg:
                    raise sqlite3.IntegrityError(str(e)) from e
                if attempt:
                    raise
                log.warning("Turso so'rovi xato (%s), qayta ulanmoqda", e.__class__.__name__)
                self.conn = self._connect_remote()

    # ---------- past daraja ----------
    def _x(self, sql, args=()):
        with self.lock:
            if self.remote:
                return TCursor(self._run(sql, args))
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _q(self, sql, args=()):
        with self.lock:
            if self.remote:
                return TCursor(self._run(sql, args)).fetchall()
            return self.conn.execute(sql, args).fetchall()

    def _init(self):
        self._x("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        self._x("CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY)")
        self._x("""CREATE TABLE IF NOT EXISTS api_keys(
            id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, api_key TEXT UNIQUE,
            status TEXT DEFAULT 'active', fails INTEGER DEFAULT 0, cooldown_until INTEGER DEFAULT 0,
            last_used INTEGER DEFAULT 0, last_error TEXT DEFAULT '', ok_count INTEGER DEFAULT 0,
            added_at INTEGER)""")
        self._x("""CREATE TABLE IF NOT EXISTS feeds(
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, enabled INTEGER DEFAULT 1, lang TEXT DEFAULT 'en')""")
        self._x("""CREATE TABLE IF NOT EXISTS seen(
            fingerprint TEXT PRIMARY KEY, url TEXT, title TEXT, status TEXT, reason TEXT,
            until INTEGER, created_at INTEGER)""")
        self._x("""CREATE TABLE IF NOT EXISTS posts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT, url TEXT, headline TEXT, category TEXT,
            caption TEXT, image_path TEXT, status TEXT, sources TEXT, error TEXT DEFAULT '',
            day TEXT, created_at INTEGER, published_at INTEGER, channel_msg_id INTEGER)""")
        self._x("CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, source TEXT, message TEXT)")
        # --- Bosqich 0: kelgusi modullar jadvallari (mavjudlarini o'zgartirmaydi) ---
        self._x("""CREATE TABLE IF NOT EXISTS quiz_questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, qtype TEXT, difficulty INTEGER, image_path TEXT,
            question TEXT, options_json TEXT, correct_idx INTEGER, explanation TEXT,
            fingerprint TEXT UNIQUE, created_at INTEGER)""")
        self._x("""CREATE TABLE IF NOT EXISTS quiz_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, question_id INTEGER, posted_at INTEGER, message_id INTEGER)""")
        self._x("""CREATE TABLE IF NOT EXISTS published_videos(
            id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT UNIQUE, title TEXT, channel TEXT, url TEXT,
            score INTEGER, category TEXT, posted_at INTEGER)""")
        self._x("""CREATE TABLE IF NOT EXISTS reactions(
            post_id INTEGER, user_id INTEGER, emoji TEXT, PRIMARY KEY(post_id, user_id))""")
        self._x("""CREATE TABLE IF NOT EXISTS content_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT, ref TEXT, posted_at INTEGER,
            message_id INTEGER, status TEXT, error TEXT DEFAULT '')""")
        self._x("""CREATE TABLE IF NOT EXISTS quiz_answers(
            question_id INTEGER, user_id INTEGER, idx INTEGER, correct INTEGER, day TEXT, answered_at INTEGER,
            PRIMARY KEY(question_id, user_id))""")
        self._x("""CREATE TABLE IF NOT EXISTS video_rejects(
            video_id TEXT PRIMARY KEY, reason TEXT, at INTEGER)""")
        self._x("""CREATE TABLE IF NOT EXISTS daily_polls(
            id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, options_json TEXT, posted_at INTEGER)""")
        for k, v in DEFAULT_SETTINGS.items():
            self._x("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        # eskirgan (o'chirilgan) Gemini modellarini yangisiga almashtirish
        for k, olds in (("gemini_model", ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash",
                                          "gemini-3-flash-preview", "gemini-3.5-flash", "gemini-3.6-flash")),
                        ("gemini_image_model", ("gemini-2.5-flash-image", "gemini-3.1-flash-image-preview"))):
            if self.get(k) in olds:
                self.set(k, DEFAULT_SETTINGS[k])
        if self.get("feeds_seeded") != "1":
            for url, lang in DEFAULT_FEEDS:
                self._x("INSERT OR IGNORE INTO feeds(url,lang) VALUES(?,?)", (url, lang))
            self.set("feeds_seeded", "1")

    def seed_from_env(self):
        for prov, env in (("gemini", "GEMINI_API_KEYS"), ("groq", "GROQ_API_KEYS"), ("openrouter", "OPENROUTER_API_KEYS")):
            raw = os.getenv(env, "")
            keys = [k for k in re.split(r"[,\s]+", raw) if k.strip()]
            if keys:
                self.add_keys(prov, keys)
        ch = os.getenv("CHANNEL_ID", "").strip()
        if ch and not self.get("channel_id"):
            self.set("channel_id", ch)
        for uid in ENV_ADMIN_IDS:
            self.add_admin(uid)

    # ---------- sozlamalar ----------
    def get(self, key: str) -> str:
        rows = self._q("SELECT value FROM settings WHERE key=?", (key,))
        if rows:
            return rows[0]["value"]
        return DEFAULT_SETTINGS.get(key, "")

    def get_int(self, key: str) -> int:
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return int(DEFAULT_SETTINGS.get(key, "0") or 0)

    def set(self, key: str, value):
        self._x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))

    # ---------- modullar ----------
    def mod_on(self, name: str) -> bool:
        return self.get(f"mod_{name}_on") == "1"

    def mod_times(self, name: str) -> list:
        return parse_times(self.get(f"mod_{name}_times"))

    # ---------- reaksiyalar (bir foydalanuvchi bir postga bir marta) ----------
    def add_reaction(self, post_id: int, user_id: int, emoji: str) -> bool:
        try:
            self._x("INSERT INTO reactions(post_id,user_id,emoji) VALUES(?,?,?)", (post_id, user_id, emoji))
            return True
        except sqlite3.IntegrityError:
            return False

    def reaction_counts(self, post_id: int) -> dict:
        return {r["emoji"]: r["n"] for r in self._q(
            "SELECT emoji, COUNT(*) AS n FROM reactions WHERE post_id=? GROUP BY emoji", (post_id,))}

    def ranked_posts(self, since_ts: int = 0, limit: int = 5) -> list:
        """Reaksiya soni bo'yicha eng yaxshi postlar (yangiliklar + modul postlari). since_ts=0 — hammasi.
        Modul postlarining reaksiya id'si = -content_log.id. Reaksiyasi yo'q postlar kirmaydi."""
        totals = {r["post_id"]: r["n"] for r in self._q("SELECT post_id, COUNT(*) AS n FROM reactions GROUP BY post_id")}
        out = []
        for r in self._q("SELECT id, headline, channel_msg_id, published_at FROM posts "
                         "WHERE status='published' AND COALESCE(published_at,0)>=?", (since_ts,)):
            if totals.get(r["id"]):
                out.append({"pid": r["id"], "title": r["headline"], "msg_id": r["channel_msg_id"],
                            "n": totals[r["id"]], "at": r["published_at"] or 0, "icon": "📰"})
        for r in self._q("SELECT id, module, ref, message_id, posted_at FROM content_log "
                         "WHERE status='ok' AND posted_at>=? AND module NOT IN ('poll','best')", (since_ts,)):
            if totals.get(-r["id"]):
                ref = str(r["ref"]).split(":", 1)[-1] if r["module"] == "history" else str(r["ref"])
                title = MODULES[r["module"]][0] if r["module"] in MODULES else r["module"]
                out.append({"pid": -r["id"], "title": f"{title} — {ref[:60]}", "msg_id": r["message_id"],
                            "n": totals[-r["id"]], "at": r["posted_at"], "icon": ""})
        out.sort(key=lambda x: (-x["n"], -x["at"]))
        return out[:limit]

    def log_content(self, module: str, ref: str = "", message_id=None, status: str = "ok", error: str = "") -> int:
        cur = self._x("INSERT INTO content_log(module,ref,posted_at,message_id,status,error) VALUES(?,?,?,?,?,?)",
                      (module, ref, now_ts(), message_id, status, error[:500]))
        return cur.lastrowid

    def update_content(self, cid: int, status: str, message_id=None, error: str = ""):
        self._x("UPDATE content_log SET status=?, message_id=COALESCE(?,message_id), error=? WHERE id=?",
                (status, message_id, error[:500], cid))

    def today_published(self) -> list:
        return self._q("SELECT id, headline, category, channel_msg_id FROM posts "
                       "WHERE status='published' AND day=? ORDER BY id", (tz_now().date().isoformat(),))

    def content_exists(self, module: str, ref: str) -> bool:
        """Takror (duplicate) tekshiruvi: shu modul shu ref bilan oldin muvaffaqiyatli nashr qilinganmi."""
        return bool(self._q("SELECT 1 FROM content_log WHERE module=? AND ref=? AND status='ok' LIMIT 1",
                            (module, ref)))

    # ---------- quiz ----------
    def quiz_add(self, q: dict) -> int:
        data = json.dumps({"spec": q["spec"], "opts": q["opts"], "hint": q.get("hint", "")}, ensure_ascii=False)
        cur = self._x("INSERT INTO quiz_questions(qtype,difficulty,image_path,question,options_json,correct_idx,"
                      "explanation,fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (q["qtype"], q["difficulty"], "", q["title"], data, q["correct_idx"], q["explanation"],
                       q["fingerprint"], now_ts()))
        return cur.lastrowid

    def quiz_get(self, qid: int):
        rows = self._q("SELECT * FROM quiz_questions WHERE id=?", (qid,))
        return rows[0] if rows else None

    def quiz_by_fp(self, fp: str):
        rows = self._q("SELECT * FROM quiz_questions WHERE fingerprint=?", (fp,))
        return rows[0] if rows else None

    def quiz_recent(self, fp: str, days: int) -> bool:
        """Shu savol oxirgi `days` kunda kanalga chiqqanmi (takrorlanmaslik qoidasi)."""
        return bool(self._q("SELECT 1 FROM quiz_history h JOIN quiz_questions q ON q.id=h.question_id "
                            "WHERE q.fingerprint=? AND h.posted_at>=? LIMIT 1", (fp, now_ts() - days * 86400)))

    def quiz_record_post(self, qid: int, message_id):
        self._x("INSERT INTO quiz_history(question_id,posted_at,message_id) VALUES(?,?,?)", (qid, now_ts(), message_id))

    def quiz_last_types(self) -> dict:
        return {r["qtype"]: r["t"] for r in self._q(
            "SELECT q.qtype AS qtype, MAX(h.posted_at) AS t FROM quiz_history h "
            "JOIN quiz_questions q ON q.id=h.question_id GROUP BY q.qtype")}

    def quiz_answer_add(self, qid: int, uid: int, idx: int, correct: bool) -> bool:
        """Har foydalanuvchidan faqat birinchi javob saqlanadi (ball/reyting yo'q, faqat umumiy foiz uchun)."""
        try:
            self._x("INSERT INTO quiz_answers(question_id,user_id,idx,correct,day,answered_at) VALUES(?,?,?,?,?,?)",
                    (qid, uid, idx, 1 if correct else 0, tz_now().date().isoformat(), now_ts()))
            return True
        except sqlite3.IntegrityError:
            return False

    def quiz_my_answer(self, qid: int, uid: int):
        rows = self._q("SELECT idx, correct FROM quiz_answers WHERE question_id=? AND user_id=?", (qid, uid))
        return rows[0] if rows else None

    def quiz_stats(self, qid: int) -> tuple:
        """(jami, to'g'ri, bugun jami, bugun to'g'ri)"""
        day = tz_now().date().isoformat()
        r = self._q("SELECT COUNT(*) AS n, COALESCE(SUM(correct),0) AS ok, "
                    "COALESCE(SUM(CASE WHEN day=? THEN 1 ELSE 0 END),0) AS dn, "
                    "COALESCE(SUM(CASE WHEN day=? THEN correct ELSE 0 END),0) AS dok "
                    "FROM quiz_answers WHERE question_id=?", (day, day, qid))[0]
        return int(r["n"]), int(r["ok"]), int(r["dn"]), int(r["dok"])

    # ---------- video ----------
    def video_exists(self, vid: str) -> bool:
        return bool(self._q("SELECT 1 FROM published_videos WHERE video_id=?", (vid,)))

    def video_add(self, vid: str, title: str, channel: str, url: str, score: int, category: str):
        self._x("INSERT INTO published_videos(video_id,title,channel,url,score,category,posted_at) VALUES(?,?,?,?,?,?,?)",
                (vid, title, channel, url, score, category, now_ts()))

    def video_rejected(self, vid: str) -> bool:
        return bool(self._q("SELECT 1 FROM video_rejects WHERE video_id=?", (vid,)))

    def video_reject(self, vid: str, reason: str):
        self._x("INSERT OR REPLACE INTO video_rejects(video_id,reason,at) VALUES(?,?,?)", (vid, reason[:200], now_ts()))

    def video_recent_categories(self, n: int) -> list:
        return [r["category"] for r in self._q("SELECT category FROM published_videos ORDER BY id DESC LIMIT ?", (n,))]

    # ---------- adminlar ----------
    def admin_ids(self):
        return [r["user_id"] for r in self._q("SELECT user_id FROM admins")]

    def is_admin(self, uid: int) -> bool:
        return bool(self._q("SELECT 1 FROM admins WHERE user_id=?", (uid,)))

    def add_admin(self, uid: int):
        self._x("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))

    # ---------- API kalitlar ----------
    def add_keys(self, provider: str, keys):
        added = dup = 0
        for k in keys:
            try:
                self._x("INSERT INTO api_keys(provider,api_key,added_at) VALUES(?,?,?)", (provider, k, now_ts()))
                added += 1
            except sqlite3.IntegrityError:
                dup += 1
        return added, dup

    def next_key(self, provider: str):
        rows = self._q(
            "SELECT * FROM api_keys WHERE provider=? AND status='active' AND cooldown_until<=? "
            "ORDER BY last_used ASC, id ASC LIMIT 1", (provider, now_ts()))
        return rows[0] if rows else None

    def has_key(self, provider: str) -> bool:
        return bool(self._q("SELECT 1 FROM api_keys WHERE provider=? AND status='active' LIMIT 1", (provider,)))

    def touch_key(self, key_id: int):
        self._x("UPDATE api_keys SET last_used=? WHERE id=?", (now_ts(), key_id))

    def mark_ok(self, key_id: int):
        self._x("UPDATE api_keys SET fails=0, last_error='', cooldown_until=0, ok_count=ok_count+1 WHERE id=?", (key_id,))

    def mark_fail(self, key_id: int, error: str, cooldown: int = 0, auth: bool = False):
        with self.lock:
            row = self._q("SELECT fails FROM api_keys WHERE id=?", (key_id,))
            if not row:
                return
            fails = row[0]["fails"] + (1 if auth else 0)
            status = "dead" if auth and fails >= 2 else "active"
            self._x("UPDATE api_keys SET fails=?, status=?, cooldown_until=?, last_error=? WHERE id=?",
                    (fails, status, now_ts() + cooldown, (error or "")[:200], key_id))

    def list_keys(self, provider=None):
        if provider:
            return self._q("SELECT * FROM api_keys WHERE provider=? ORDER BY id", (provider,))
        return self._q("SELECT * FROM api_keys ORDER BY provider, id")

    def count_keys(self, provider: str) -> dict:
        now = now_ts()
        res = {"active": 0, "cooling": 0, "dead": 0}
        for r in self._q("SELECT status, cooldown_until FROM api_keys WHERE provider=?", (provider,)):
            if r["status"] == "dead":
                res["dead"] += 1
            elif r["cooldown_until"] > now:
                res["cooling"] += 1
            else:
                res["active"] += 1
        return res

    def delete_keys(self, ids) -> int:
        n = 0
        for i in ids:
            n += self._x("DELETE FROM api_keys WHERE id=?", (i,)).rowcount
        return n

    def delete_dead(self) -> int:
        return self._x("DELETE FROM api_keys WHERE status='dead'").rowcount

    def delete_all_keys(self) -> int:
        return self._x("DELETE FROM api_keys").rowcount

    def revive_keys(self) -> int:
        return self._x("UPDATE api_keys SET status='active', fails=0, cooldown_until=0").rowcount

    # ---------- RSS manbalar ----------
    def list_feeds(self, enabled_only=False):
        if enabled_only:
            return self._q("SELECT * FROM feeds WHERE enabled=1 ORDER BY id")
        return self._q("SELECT * FROM feeds ORDER BY id")

    def add_feed(self, url: str, lang: str) -> bool:
        try:
            self._x("INSERT INTO feeds(url,lang) VALUES(?,?)", (url, lang))
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_feed(self, fid: int):
        self._x("DELETE FROM feeds WHERE id=?", (fid,))

    def toggle_feed(self, fid: int):
        self._x("UPDATE feeds SET enabled=1-enabled WHERE id=?", (fid,))

    # ---------- ko'rilgan yangiliklar ----------
    def is_seen(self, fp: str) -> bool:
        return bool(self._q("SELECT 1 FROM seen WHERE fingerprint=? AND until>?", (fp, now_ts())))

    def mark_seen(self, fp, url, title, status, reason="", hours=None):
        until = FAR if hours is None else now_ts() + int(hours * 3600)
        self._x("INSERT INTO seen(fingerprint,url,title,status,reason,until,created_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET status=excluded.status, reason=excluded.reason, until=excluded.until",
                (fp, url, title, status, reason, until, now_ts()))

    def recent_titles(self, hours=48):
        rows = self._q("SELECT title FROM seen WHERE created_at>? AND status IN ('published','pending')",
                       (now_ts() - hours * 3600,))
        return [r["title"] for r in rows]

    # ---------- postlar ----------
    def add_post(self, fp, url, headline, category, caption, image_path, status, sources) -> int:
        cur = self._x("INSERT INTO posts(fingerprint,url,headline,category,caption,image_path,status,sources,day,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (fp, url, headline, category, caption, image_path, status, sources,
                       tz_now().date().isoformat(), now_ts()))
        return cur.lastrowid

    def get_post(self, pid: int):
        rows = self._q("SELECT * FROM posts WHERE id=?", (pid,))
        return rows[0] if rows else None

    def claim_post(self, pid: int) -> bool:
        return self._x("UPDATE posts SET status='publishing' WHERE id=? AND status IN ('pending','failed')",
                       (pid,)).rowcount == 1

    def set_post_status(self, pid: int, status: str, error: str = "", msg_id=None):
        pub = now_ts() if status == "published" else None
        self._x("UPDATE posts SET status=?, error=?, channel_msg_id=COALESCE(?,channel_msg_id), "
                "published_at=COALESCE(?,published_at) WHERE id=?", (status, error[:300], msg_id, pub, pid))

    def count_today(self) -> int:
        r = self._q("SELECT COUNT(*) c FROM posts WHERE day=? AND status IN ('published','pending','publishing')",
                    (tz_now().date().isoformat(),))
        return int(r[0]["c"])

    def recent_posts(self, n=10):
        return self._q("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (n,))

    def stats(self) -> dict:
        def c(sql, a=()):
            return int(self._q(sql, a)[0][0])
        return {
            "today": self.count_today(),
            "published": c("SELECT COUNT(*) FROM posts WHERE status='published'"),
            "pending": c("SELECT COUNT(*) FROM posts WHERE status='pending'"),
            "failed": c("SELECT COUNT(*) FROM posts WHERE status='failed'"),
            "rejected": c("SELECT COUNT(*) FROM posts WHERE status='rejected'"),
            "seen": c("SELECT COUNT(*) FROM seen"),
        }

    # ---------- jurnal ----------
    def log(self, source: str, message: str):
        self._x("INSERT INTO logs(ts,source,message) VALUES(?,?,?)", (now_ts(), source, str(message)[:500]))
        self._x("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 300)")

    def recent_logs(self, n=15):
        return self._q("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (n,))

    def clear_logs(self):
        self._x("DELETE FROM logs")

# ======================================================================
# API MIJOZLAR (Gemini / Groq / OpenRouter / Pollinations)
# ======================================================================

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
OPENAI_COMPAT = {
    "groq": {"base": "https://api.groq.com/openai/v1", "json_mode": True},
    "openrouter": {"base": "https://openrouter.ai/api/v1", "json_mode": False},
}


class ApiError(Exception):
    """kind: auth | quota | server | request"""

    def __init__(self, status: int, message: str, kind: str):
        super().__init__(f"[{kind} {status}] {message}")
        self.status = status
        self.message = message
        self.kind = kind


def classify(status: int, text: str) -> str:
    t = (text or "").lower()
    if status == 429 or "resource_exhausted" in t or "rate limit" in t:
        return "quota"
    if status in (401, 403) or "api key not valid" in t or "api_key_invalid" in t or "invalid api key" in t \
            or "invalid_api_key" in t:
        return "auth"
    if status >= 500:
        return "server"
    return "request"


async def _request(method: str, url: str, timeout: float = 60, **kw) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            r = await c.request(method, url, **kw)
    except httpx.TimeoutException:
        raise ApiError(0, "timeout", "server")
    except httpx.HTTPError as e:
        raise ApiError(0, e.__class__.__name__, "server")
    if r.status_code != 200:
        body = (r.text or "")[:300].replace("\n", " ")
        raise ApiError(r.status_code, body, classify(r.status_code, body))
    return r


GEMINI_FALLBACK_MODEL = "gemini-3.1-flash-lite"


async def gemini_text(key: str, model: str, system: str, prompt: str, json_mode: bool = True) -> str:
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    try:
        r = await _request("POST", f"{GEMINI_BASE}/{model}:generateContent", timeout=30,
                           headers={"x-goog-api-key": key}, json=payload)
    except ApiError as e:
        # model band (503/timeout) bo'lsa — yengilroq zaxira modelga o'tamiz
        if e.kind != "server" or model == GEMINI_FALLBACK_MODEL:
            raise
        r = await _request("POST", f"{GEMINI_BASE}/{GEMINI_FALLBACK_MODEL}:generateContent", timeout=30,
                           headers={"x-goog-api-key": key}, json=payload)
    data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        raise ApiError(200, "Gemini javobi bo'sh (bloklangan bo'lishi mumkin)", "request")


async def openai_text(provider: str, key: str, model: str, system: str, prompt: str, json_mode: bool = True) -> str:
    cfg = OPENAI_COMPAT[provider]
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }
    if json_mode and cfg["json_mode"]:
        payload["response_format"] = {"type": "json_object"}
    r = await _request("POST", cfg["base"] + "/chat/completions",
                       headers={"Authorization": f"Bearer {key}"}, json=payload)
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ApiError(200, f"{provider} javobi noto'g'ri", "request")


async def text_call(provider: str, key: str, model: str, system: str, prompt: str, json_mode: bool = True) -> str:
    if provider == "gemini":
        return await gemini_text(key, model, system, prompt, json_mode)
    return await openai_text(provider, key, model, system, prompt, json_mode)


async def ping(provider: str, key: str, model: str) -> bool:
    out = await text_call(provider, key, model, "Qisqa javob ber.", "Faqat 'ok' deb yoz.", json_mode=False)
    return bool(out.strip())


async def gemini_image(key: str, model: str, prompt: str) -> bytes:
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
    r = await _request("POST", f"{GEMINI_BASE}/{model}:generateContent",
                       timeout=90, headers={"x-goog-api-key": key}, json=payload)
    for cand in r.json().get("candidates", []):
        for p in cand.get("content", {}).get("parts", []):
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    raise ApiError(200, "Gemini rasm qaytarmadi", "request")


async def pollinations_image(prompt: str) -> bytes:
    url = "https://image.pollinations.ai/prompt/" + quote(prompt[:600])
    r = await _request("GET", url, timeout=120,
                       params={"width": 1080, "height": 1080, "nologo": "true", "model": "flux",
                               "seed": random.randint(1, 10_000_000)})
    if not r.headers.get("content-type", "").startswith("image/"):
        raise ApiError(200, "Pollinations rasm qaytarmadi", "request")
    return r.content

# ======================================================================
# API KALITLAR PULI
# ======================================================================

class AllKeysFailed(Exception):
    def __init__(self, provider: str, last: Exception | None):
        super().__init__(f"{provider}: barcha kalitlar ishlamadi ({last})")
        self.provider = provider
        self.last = last


def apply_error(db: DB, key_id: int, e: ApiError):
    msg = e.message
    low = msg.lower()
    if e.kind == "auth":
        db.mark_fail(key_id, msg, cooldown=600, auth=True)
    elif e.kind == "quota":
        if "perminute" in low or "per minute" in low or "per_minute" in low:
            cd = 90
        elif "perday" in low or "per day" in low or "daily" in low:
            cd = 6 * 3600
        else:
            cd = 900
        db.mark_fail(key_id, msg, cooldown=cd)
    elif e.kind == "server":
        db.mark_fail(key_id, msg, cooldown=30)


async def run_with_pool(db: DB, provider: str, func, attempts: int = 60):
    """func(key) -> natija. 'request' turidagi xato (model/so'rov xatosi) kalit aybi emas — darrov ko'tariladi."""
    last = None
    server_errors = 0
    for _ in range(attempts):
        row = db.next_key(provider)
        if row is None:
            break
        db.touch_key(row["id"])
        try:
            result = await func(row["api_key"])
            db.mark_ok(row["id"])
            return result
        except ApiError as e:
            last = e
            if e.kind == "request":
                raise
            apply_error(db, row["id"], e)
            db.log(f"api:{provider}", f"kalit #{row['id']} — {e.kind} {e.status}: {e.message[:120]}")
            if e.kind == "server":
                server_errors += 1
                if server_errors >= 2:
                    break
    raise AllKeysFailed(provider, last)

# ======================================================================
# NEWS SCANNER
# ======================================================================

warnings.filterwarnings("ignore", category=UserWarning, module="bs4")
UA = {"User-Agent": "Mozilla/5.0 (compatible; XabarHubBot/2.0)"}
STOP = set("the and for with from that this was were are has have had will its into over after about more than "
           "new says say said who what when how why not but you your they their them his her out off per via "
           "va bilan uchun bu shu ham yoki lekin biroq endi hali bor yoq edi".split())


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)).strip()


def tokens(s: str) -> set:
    return {w for w in re.findall(r"[\w']+", (s or "").lower()) if len(w) > 2 and w not in STOP}


def overlap(a: str, b: str) -> tuple[float, int]:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0, 0
    common = len(ta & tb)
    return common / min(len(ta), len(tb)), common


def similar(a: str, b: str, threshold: float = 0.7) -> bool:
    ratio, common = overlap(a, b)
    return ratio >= threshold and common >= 4


def fingerprint(title: str) -> str:
    norm = re.sub(r"[^\w ]", "", (title or "").lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def entry_ts(entry) -> float | None:
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    return float(calendar.timegm(st)) if st else None


async def fetch_feed(client: httpx.AsyncClient, feed, db: DB) -> list[dict]:
    try:
        r = await client.get(feed["url"])
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    except Exception as e:
        db.log("rss", f"{feed['url'][:60]} — {e.__class__.__name__}")
        return []
    pub_name = clean(parsed.feed.get("title", "")) or domain_of(feed["url"])
    items = []
    for e in parsed.entries[:40]:
        title = clean(e.get("title", ""))
        link = e.get("link", "")
        ts = entry_ts(e)
        if len(title) < 20 or not link or ts is None:
            continue
        items.append({"title": title, "link": link, "summary": clean(e.get("summary", ""))[:1800],
                      "publisher": pub_name, "ts": ts, "feed_url": feed["url"], "lang": feed["lang"]})
    if not items:
        db.log("rss", f"{feed['url'][:60]} — yangilik topilmadi")
    return items


async def collect(db: DB, client: httpx.AsyncClient, limit: int = 80, feed_filter: str = "") -> list[dict]:
    feeds = db.list_feeds(enabled_only=True)
    if feed_filter:
        feeds = [f for f in feeds if feed_filter in f["url"].lower()]
    results = await asyncio.gather(*[fetch_feed(client, f, db) for f in feeds])
    now = time.time()
    max_age = db.get_int("max_age_hours") * 3600
    recent = db.recent_titles(48)
    per_feed = []
    for items in results:
        lst = []
        for it in items:
            if now - it["ts"] > max_age or it["ts"] > now + 900:
                continue
            fp = fingerprint(it["title"])
            if db.is_seen(fp) or any(similar(it["title"], t) for t in recent):
                continue
            it["fp"] = fp
            lst.append(it)
        lst.sort(key=lambda x: -x["ts"])
        per_feed.append(lst)
    merged, seen_fp, titles = [], set(), []
    while any(per_feed) and len(merged) < limit:
        for lst in per_feed:
            if not lst:
                continue
            it = lst.pop(0)
            if it["fp"] in seen_fp or any(similar(it["title"], t) for t in titles):
                continue
            seen_fp.add(it["fp"])
            titles.append(it["title"])
            merged.append(it)
    return merged


async def fetch_article(client: httpx.AsyncClient, url: str) -> dict:
    out = {"image_url": None, "text": "", "description": ""}
    try:
        r = await client.get(url, headers=UA)
        r.raise_for_status()
        soup = BeautifulSoup(r.text[:1_500_000], "html.parser")
    except Exception:
        return out

    def meta(*names):
        for n in names:
            tag = soup.find("meta", property=n) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    out["image_url"] = meta("og:image", "twitter:image")
    out["description"] = meta("og:description", "description") or ""
    paras = [clean(p.get_text(" ")) for p in soup.find_all("p")]
    text = " ".join(p for p in paras if len(p) > 50)
    out["text"] = text[:3500]
    return out


async def download_image(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        r = await client.get(url, headers=UA)
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("image/") and len(r.content) < 8_000_000:
            return r.content
    except Exception:
        pass
    return None


async def validate_feed(url: str) -> tuple[bool, int, str]:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=UA) as c:
            r = await c.get(url)
            r.raise_for_status()
        parsed = feedparser.parse(r.content)
        n = len(parsed.entries)
        return n > 0, n, clean(parsed.feed.get("title", "")) or domain_of(url)
    except Exception as e:
        return False, 0, e.__class__.__name__

# ======================================================================
# FACT CHECKER
# ======================================================================

GN = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
LANG = {"uz": ("uz", "UZ", "UZ:uz"), "en": ("en-US", "US", "US:en")}


def pub_key(name: str) -> str:
    n = re.sub(r"[^a-z0-9а-яўқғҳ ]", "", (name or "").lower()).strip()
    words = [w for w in n.split() if w not in ("the", "al")]
    return words[0] if words else n


def is_official(link: str) -> bool:
    d = domain_of(link)
    return any(d == o or d.endswith("." + o) for o in OFFICIAL_DOMAINS)


async def cross_check(client: httpx.AsyncClient, cand: dict, db: DB) -> dict:
    min_sources = db.get_int("min_sources")
    publishers = {pub_key(cand["publisher"]): cand["publisher"]}
    titles = []
    words = re.sub(r"[^\w' ]", " ", cand["title"]).split()[:14]
    hl, gl, ceid = LANG.get(cand.get("lang", "en"), LANG["en"])
    url = GN.format(q=quote_plus(" ".join(words) + " when:2d"), hl=hl, gl=gl, ceid=ceid)
    try:
        r = await client.get(url, headers=UA)
        r.raise_for_status()
        for e in feedparser.parse(r.content).entries[:30]:
            src = clean((e.get("source") or {}).get("title", "")) if isinstance(e.get("source"), dict) else ""
            title = clean(e.get("title", ""))
            if src and title.endswith(" - " + src):
                title = title[: -len(" - " + src)]
            ratio, common = overlap(cand["title"], title)
            if src and ratio >= 0.55 and common >= 3:
                key = pub_key(src)
                if key not in publishers:
                    publishers[key] = src
                    titles.append(f"{src}: {title}")
    except Exception as e:
        db.log("fact", f"Google News qidiruvi xato: {e.__class__.__name__}")
    official = is_official(cand["link"])
    return {"publishers": list(publishers.values()), "count": len(publishers), "titles": titles,
            "official": official, "ok": official or len(publishers) >= min_sources}

# ======================================================================
# AI EDITOR
# ======================================================================

class AllProvidersFailed(Exception):
    pass


EDITOR_SYSTEM = ("Sen XABAR HUB tahririyatining professional muharririsan (CNN, BBC, Reuters standartlari). "
          "Faqat berilgan ma'lumotlarga tayan, hech narsa to'qima. Faqat to'g'ri JSON qaytar.")


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise ValueError("JSON topilmadi")
        return json.loads(m.group(0))


async def generate_json(db: DB, system: str, prompt: str) -> dict:
    order = [p.strip() for p in db.get("text_providers").split(",") if p.strip() in PROVIDERS]
    last = None
    for provider in order:
        if not db.has_key(provider):
            continue
        model = db.get(f"{provider}_model")
        try:
            text = await run_with_pool(db, provider, lambda k, p=provider, m=model: text_call(p, k, m, system, prompt))
            return parse_json(text)
        except AllKeysFailed as e:
            db.log("ai", str(e))
            last = e
        except ApiError as e:
            db.log(f"ai:{provider}", f"so'rov xatosi: {e.message[:150]}")
            last = e
        except ValueError as e:
            db.log(f"ai:{provider}", f"JSON o'qib bo'lmadi: {e}")
            last = e
    raise AllProvidersFailed(f"Hech bir AI ishlamadi: {last}")


def _clean(s) -> str:
    return re.sub(r"[*`#_]{1,}", "", str(s or "")).strip()


async def write_post(db: DB, art: dict) -> dict | None:
    """art: title, summary, text, publisher, link, published(datetime), confirmations(list[str])."""
    conf = "\n".join(f"- {x}" for x in art.get("confirmations", [])[:6]) or "- (topilmadi)"
    pub = art.get("published")
    pub_s = datetime.fromtimestamp(pub, tz_now().tzinfo).strftime("%Y-%m-%d %H:%M") if pub else "noma'lum"
    prompt = f"""Bugungi sana: {tz_now().strftime('%Y-%m-%d %H:%M')} (Toshkent)
Yangilik e'lon qilingan vaqt: {pub_s}
Asosiy manba: {art['publisher']} — {art['link']}
Shu voqeani yoritgan boshqa nashrlar sarlavhalari:
{conf}

Sarlavha: {art['title']}
Qisqa mazmun: {art.get('summary', '')[:1200]}
Maqola matni (parcha): {art.get('text', '')[:3000]}

QOIDALAR:
1. Lotin yozuvidagi o'zbek tilida, xolis jurnalistik uslubda yoz. Imlo xatosiz.
2. Sensatsion yoki yolg'on sarlavha yo'q. Shaxsiy fikr qo'shma.
3. Faqat yuqoridagi ma'lumotdagi fakt, raqam, ism va sanalarni ishlat. Hech narsa to'qima.
4. Ma'lumot yetarli bo'lmasa, manbalar bir-biriga zid bo'lsa, xabar mish-mish yoki tasdiqlanmagan bo'lsa, yoki voqea eski bo'lsa: status="skip".
5. Tasdiqlanmagan da'voni fakt kabi yozma — kimning aytganini ko'rsat ("... xabar berdi").
6. headline: 90 belgidan oshmasin. body: 2-3 qisqa paragraf, jami 600 belgidan oshmasin, paragraflar bo'sh qator bilan ajratilsin.
7. details: 0-4 ta muhim tafsilot (har biri 90 belgigacha, raqam/sana/ism).
8. image_prompt: INGLIZ tilida, mavzuga mos fotografik sahna tavsifi (40 so'zgacha). Matn, harf, logotip va aniq real shaxslar yuzi bo'lmasin.
9. category: {', '.join(CATEGORIES)} dan bittasi.

Faqat shu JSON: {{"status":"ready|skip","reason":"...","category":"...","headline":"...","body":"...","details":["..."],"image_prompt":"..."}}"""
    data = await generate_json(db, EDITOR_SYSTEM, prompt)
    if str(data.get("status", "")).lower() != "ready":
        db.log("editor", f"skip: {str(data.get('reason', ''))[:150]} | {art['title'][:80]}")
        return None
    headline = _clean(data.get("headline"))[:110]
    body = _clean(data.get("body"))
    if len(headline) < 10 or len(body) < 60:
        db.log("editor", f"AI javobi juda qisqa: {art['title'][:80]}")
        return None
    category = _clean(data.get("category")).upper()
    if category not in CATEGORIES:
        category = "BOSHQA"
    details = [_clean(x)[:120] for x in (data.get("details") or []) if _clean(x)][:4]
    return {"headline": headline, "body": body, "category": category, "details": details,
            "image_prompt": _clean(data.get("image_prompt"))[:400]}

# ======================================================================
# AI IMAGE GENERATOR
# ======================================================================

SUFFIX = (". Photorealistic editorial news photograph, cinematic natural lighting, "
          "no text, no letters, no logos, no watermark, no identifiable real people, square composition")


def valid_image(data) -> bool:
    try:
        im = Image.open(io.BytesIO(data))
        im.verify()
        return im.size[0] >= 256 and im.size[1] >= 256
    except Exception:
        return False


async def generate_image(db: DB, prompt: str) -> bytes | None:
    full = prompt.strip().rstrip(".") + SUFFIX
    for prov in [p.strip() for p in db.get("image_providers").split(",") if p.strip()]:
        try:
            if prov == "gemini":
                if time.time() < db.get_int("gemini_image_off_until") or not db.has_key("gemini"):
                    continue
                model = db.get("gemini_image_model")
                data = await run_with_pool(db, "gemini", lambda k: gemini_image(k, model, full), attempts=3)
            elif prov == "pollinations":
                data = await pollinations_image(full)
            else:
                continue
            if valid_image(data):
                return data
            db.log("image", f"{prov}: yaroqsiz rasm qaytdi")
        except AllKeysFailed as e:
            db.log("image", f"gemini rasm: {str(e)[:150]}")
        except ApiError as e:
            db.log("image", f"{prov}: {e.message[:150]}")
            if prov == "gemini":  # model mavjud emas / bepul emas — 6 soat urinmaymiz
                db.set("gemini_image_off_until", int(time.time()) + 6 * 3600)
    return None

# ======================================================================
# IMAGE DESIGNER
# ======================================================================

W = H = 1080
RED = (215, 25, 32)
NAVY = (8, 14, 28)
WHITE = (255, 255, 255)
MUTED = (190, 200, 215)
HERE = os.path.dirname(os.path.abspath(__file__))
BOLD_FONTS = [
    os.path.join(HERE, "fonts", "Inter-ExtraBold.ttf"), os.path.join(HERE, "fonts", "Inter-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Helvetica.ttc",
]


@lru_cache(maxsize=64)
def font(size: int):
    for p in BOLD_FONTS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def norm(text: str) -> str:
    return re.sub(r"[ʻʼ’‘`´]", "'", text or "")


def vgrad(w, h, top, bottom):
    g = Image.linear_gradient("L").resize((w, h))
    return g.point(lambda v: int(top + (bottom - top) * v / 255))


def wrap(draw, text, fnt, max_w):
    lines, line = [], ""
    for word in text.split():
        test = (line + " " + word).strip()
        if draw.textlength(test, font=fnt) > max_w and line:
            lines.append(line)
            line = word
        else:
            line = test
    if line:
        lines.append(line)
    return lines


def fallback_background() -> Image.Image:
    bg = Image.new("RGB", (W, H), NAVY)
    bg.paste(Image.new("RGB", (W, H), (24, 40, 72)), (0, 0), vgrad(W, H, 255, 0))
    d = ImageDraw.Draw(bg)
    for r, c in ((520, (34, 54, 92)), (380, (40, 62, 104)), (240, (46, 70, 116))):
        d.ellipse((W - r, 40, W + r // 3, 40 + r * 2 // 1 - r // 2), outline=c, width=3)
    d.polygon([(0, 560), (W, 380), (W, 440), (0, 620)], fill=(60, 12, 20))
    return bg


def create_news_image(category: str, headline: str, background: bytes | None = None, ai_generated: bool = False) -> bytes:
    canvas = None
    if background:
        try:
            canvas = ImageOps.fit(Image.open(io.BytesIO(background)).convert("RGB"), (W, H), centering=(0.5, 0.4))
        except Exception:
            canvas = None
    if canvas is None:
        canvas = fallback_background()
        ai_generated = False
    # to'q navy qoplamalar (tepa va past)
    canvas.paste(Image.new("RGB", (W, 260), NAVY), (0, 0), vgrad(W, 260, 235, 0))
    canvas.paste(Image.new("RGB", (W, 700), NAVY), (0, H - 700), vgrad(W, 700, 0, 245))
    d = ImageDraw.Draw(canvas)

    # yuqori: logotip
    d.rounded_rectangle((52, 44, 132, 124), radius=14, fill=RED)
    d.text((92, 84), "XH", font=font(38), fill=WHITE, anchor="mm")
    d.text((152, 70), "XABAR HUB", font=font(44), fill=WHITE, anchor="lm")
    d.text((154, 106), "DUNYO XABARLARI — BIR MARKAZDA", font=font(16), fill=MUTED, anchor="lm")

    # sarlavha: o'lchamni avtomatik moslash
    headline = norm(headline)
    max_w = W - 120
    size, lines = 68, []
    while size >= 38:
        lines = wrap(d, headline, font(size), max_w)
        if len(lines) <= 4:
            break
        size -= 4
    if len(lines) > 4:
        lines = lines[:4]
        lines[-1] = lines[-1].rstrip(" .,") + "…"
    lh = int(size * 1.2)
    bottom = 962
    top = bottom - lh * len(lines)
    d.rectangle((52, top + 6, 60, bottom - 6), fill=RED)
    y = top
    for ln in lines:
        d.text((84, y), ln, font=font(size), fill=WHITE, stroke_width=1, stroke_fill=(0, 0, 0))
        y += lh

    # kategoriya
    cat = norm(category).upper()[:22]
    cw = int(d.textlength(cat, font=font(26))) + 56
    d.rounded_rectangle((52, top - 78, 52 + cw, top - 24), radius=14, fill=RED)
    d.text((52 + cw // 2, top - 51), cat, font=font(26), fill=WHITE, anchor="mm")

    # past: brend belgisi
    d.line((52, 1004, W - 52, 1004), fill=(80, 92, 112), width=2)
    d.text((52, 1040), "XABAR HUB", font=font(24), fill=WHITE, anchor="lm")
    tag = "AI ILLUSTRATSIYA — haqiqiy foto emas" if ai_generated else ""
    d.text((W - 52, 1040), tag, font=font(20), fill=MUTED, anchor="rm")

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=90, optimize=True)
    return out.getvalue()

# ======================================================================
# UMUMIY YORDAMCHI
# ======================================================================

def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=False)


# ======================================================================
# POST MANAGER
# ======================================================================

CAPTION_LIMIT = 1024


def build_caption(art: dict) -> str:
    """art: headline, category, body, details, source_url, publisher, confirmed(list), ai_image(bool)."""
    head = f"🔴 <b>{esc(art['category'])}</b>\n<b>{esc(art['headline'])}</b>\n\n"
    tail_lines = [f"📎 Manba: <a href=\"{html.escape(art['source_url'], quote=True)}\">{esc(art['publisher'])}</a>"]
    others = [c for c in art.get("confirmed", []) if c != art["publisher"]][:3]
    if others:
        tail_lines.append("✔️ Shuningdek xabar bergan: " + esc(", ".join(others)))
    if art.get("ai_image"):
        tail_lines.append("🖼 Rasm: AI illyustratsiya (haqiqiy voqea fotosi emas)")
    tail = "\n" + "\n".join(tail_lines) + "\n\n" + SIGNATURE

    def assemble(body: str, with_details: bool) -> str:
        det = ""
        if with_details and art.get("details"):
            det = "\n\n" + "\n".join("• " + esc(d) for d in art["details"])
        return head + esc(body) + det + tail

    body = art["body"].strip()
    for with_details in (True, False):
        text = assemble(body, with_details)
        if len(text) <= CAPTION_LIMIT:
            return text
    room = CAPTION_LIMIT - len(assemble("", False)) - 2
    cut = body[: max(room, 0)]
    for sep in (". ", "! ", "? "):
        i = cut.rfind(sep)
        if i > 80:
            cut = cut[: i + 1]
            break
    else:
        cut = cut.rsplit(" ", 1)[0] + "…"
    return assemble(cut, False)


def _chat(db: DB):
    ch = db.get("channel_id")
    return int(ch) if ch.lstrip("-").isdigit() else ch


def _save_image(fp: str, data: bytes) -> str:
    os.makedirs(IMAGE_DIR, exist_ok=True)
    path = os.path.join(IMAGE_DIR, f"{fp[:16]}.jpg")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _drop_image(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


# ---------- Post ostidagi tugmalar ----------
REACTIONS = {"up": "👍", "fire": "🔥", "wow": "😮"}
_bot_username = ""


def reaction_rows(counts: dict, post_id: int, share_url: str) -> list:
    """Tugmalar tuzilmasi (sof funksiya): [[{text, cb|url}, ...], ...]"""
    row = []
    for code, emoji in REACTIONS.items():
        n = counts.get(emoji, 0)
        row.append({"text": f"{emoji} {n}" if n else emoji, "cb": f"rx:{post_id}:{code}"})
    rows = [row]
    if share_url:
        rows.append([{"text": "📤 Do'stga yuborish", "url": share_url}])
    return rows


def share_link(link: str, text: str = "XABAR HUB") -> str:
    return f"https://t.me/share/url?url={quote_plus(link)}&text={quote_plus(text)}"


def reaction_keyboard(db: DB, post_id: int, share_url: str = "", extra_rows: list | None = None) -> InlineKeyboardMarkup | None:
    if db.get("reaction_buttons") != "1":
        return None
    rows = reaction_rows(db.reaction_counts(post_id), post_id, share_url)
    return InlineKeyboardMarkup(inline_keyboard=list(extra_rows or []) + [
        [InlineKeyboardButton(text=b["text"], callback_data=b["cb"]) if "cb" in b
         else InlineKeyboardButton(text=b["text"], url=b["url"]) for b in r] for r in rows])


async def post_link(bot: Bot, db: DB, msg_id: int | None = None) -> str:
    """Ulashish uchun havola: ochiq kanalda postning o'zi, bo'lmasa kanal, bo'lmasa botning havolasi."""
    global _bot_username
    uname = db.get("channel_username")
    if not uname and db.get("channel_id") and not msg_id:  # kanal oldin ulangan bo'lsa, username'ni bir marta aniqlaymiz
        try:
            uname = (await bot.get_chat(_chat(db))).username or ""
            db.set("channel_username", uname)
        except Exception:
            pass
    if uname:
        return f"https://t.me/{uname}" + (f"/{msg_id}" if msg_id else "")
    if not _bot_username:
        try:
            _bot_username = (await bot.me()).username or ""
        except Exception:
            pass
    return f"https://t.me/{_bot_username}" if _bot_username else ""


async def publish(bot: Bot, db: DB, post_id: int) -> tuple[bool, str]:
    if not db.claim_post(post_id):
        return False, "Post topilmadi yoki allaqachon ko'rib chiqilgan"
    p = db.get_post(post_id)
    if not db.get("channel_id"):
        db.set_post_status(post_id, "failed", "Kanal ulanmagan")
        return False, "Kanal ulanmagan"
    try:
        with open(p["image_path"], "rb") as f:
            data = f.read()
    except OSError:
        db.set_post_status(post_id, "failed", "Rasm fayli topilmadi")
        return False, "Rasm fayli topilmadi"
    err = ""
    for _ in range(2):
        try:
            link = await post_link(bot, db)
            msg = await bot.send_photo(chat_id=_chat(db), photo=BufferedInputFile(data, "xabar-hub.jpg"),
                                       caption=p["caption"], parse_mode="HTML",
                                       reply_markup=reaction_keyboard(db, post_id, share_link(link) if link else ""))
            db.set_post_status(post_id, "published", msg_id=msg.message_id)
            _drop_image(p["image_path"])
            if db.get("channel_username") and db.get("reaction_buttons") == "1":
                try:  # ulashish havolasini postning aniq havolasiga almashtirish
                    exact = await post_link(bot, db, msg.message_id)
                    await bot.edit_message_reply_markup(chat_id=_chat(db), message_id=msg.message_id,
                                                        reply_markup=reaction_keyboard(db, post_id, share_link(exact)))
                except TelegramAPIError:
                    pass
            return True, ""
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            err = "Telegram limiti"
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}"
            break
    db.set_post_status(post_id, "failed", err)
    db.log("telegram", f"nashr xatosi #{post_id}: {err[:200]}")
    return False, err


def reject(db: DB, post_id: int):
    p = db.get_post(post_id)
    if p:
        db.set_post_status(post_id, "rejected")
        _drop_image(p["image_path"])


def approval_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Nashr qilish", callback_data=f"p:ok:{post_id}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"p:no:{post_id}"),
    ]])


async def submit(bot: Bot, db: DB, art: dict, image: bytes, cand: dict) -> tuple[str, str]:
    """Postni saqlaydi; avto-rejimda darrov nashr qiladi, aks holda adminlarga tasdiqqa yuboradi.
    Qaytaradi: (status, xato)."""
    caption = build_caption(art)
    path = _save_image(cand["fp"], image)
    pid = db.add_post(cand["fp"], cand["link"], art["headline"], art["category"], caption, path, "pending",
                      ", ".join(art.get("confirmed", [])))
    if db.get("auto_publish") == "1":
        ok, err = await publish(bot, db, pid)
        return ("published", "") if ok else ("failed", err)
    sent = 0
    for uid in db.admin_ids():
        try:
            await bot.send_photo(uid, BufferedInputFile(image, "preview.jpg"),
                                 caption=caption[:1010], parse_mode="HTML", reply_markup=approval_keyboard(pid))
            sent += 1
        except Exception as e:
            db.log("telegram", f"adminga yuborib bo'lmadi {uid}: {e.__class__.__name__}")
    return ("pending", "") if sent else ("failed", "Adminga yuborib bo'lmadi")

# ======================================================================
# NEWSROOM
# ======================================================================

class Newsroom:
    def __init__(self, bot, db: DB):
        self.bot = bot
        self.db = db
        self.lock = asyncio.Lock()
        self.last_result = ""

    async def scan(self, manual: bool = False, slot: bool = False, feed_filter: str = "") -> dict:
        if self.lock.locked():
            return {"error": "Qidiruv allaqachon ketmoqda, kuting."}
        async with self.lock:
            try:
                res = await self._scan(manual, slot, feed_filter)
            except Exception as e:
                log.exception("scan failed")
                self.db.log("newsroom", f"scan xatosi: {e.__class__.__name__}: {e}")
                res = {"error": f"Kutilmagan xato: {e.__class__.__name__}"}
            self.last_result = f"{tz_now().strftime('%H:%M')} — {res}"
            return res

    async def _scan(self, manual: bool, slot: bool = False, feed_filter: str = "") -> dict:
        db = self.db
        if not manual:
            if db.get("paused") == "1":
                return {"skipped": "pauza"}
            if not slot and not (db.get_int("active_from") <= tz_now().hour < db.get_int("active_to")):
                return {"skipped": "faol soatdan tashqari"}
        if not db.get("channel_id"):
            return {"error": "Kanal ulanmagan. «📣 Kanal» bo'limidan ulang."}
        if not any(db.has_key(p) for p in PROVIDERS):
            return {"error": "API kalit yo'q. «🔑 API kalitlar» bo'limidan qo'shing."}
        remaining = db.get_int("daily_limit") - db.count_today()
        if slot:
            remaining = min(remaining, max(1, db.get_int("posts_per_time")))
        if remaining <= 0:
            return {"error": "Bugungi post limiti to'ldi."} if manual else {"skipped": "limit to'ldi"}

        st = Counter()
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=UA) as client:
            cands = await collect(db, client, feed_filter=feed_filter)
            st["topildi"] = len(cands)
            checked, max_checks = 0, remaining * 4 + 4
            for c in cands:
                if st["nashr"] + st["tasdiqda"] >= remaining or checked >= max_checks:
                    break
                checked += 1
                try:
                    outcome = await self._process(client, c, st)
                except Exception as e:
                    db.log("newsroom", f"{c['title'][:60]} — {e.__class__.__name__}: {e}")
                    st["xato"] += 1
                    continue
                if outcome == "stop":
                    break
        return dict(st)

    async def _process(self, client, c: dict, st: Counter) -> str:
        db = self.db
        fc = await cross_check(client, c, db)
        if not fc["ok"]:
            db.mark_seen(c["fp"], c["link"], c["title"], "skipped", "manba kam", hours=2)
            st["manba_kam"] += 1
            return "skip"
        page = await fetch_article(client, c["link"])
        art_in = {"title": c["title"], "summary": c["summary"] or page["description"], "text": page["text"],
                  "publisher": c["publisher"], "link": c["link"], "published": c["ts"],
                  "confirmations": fc["titles"]}
        try:
            out = await write_post(db, art_in)
        except AllProvidersFailed as e:
            db.log("ai", str(e)[:300])
            st["ai_ishlamadi"] += 1
            return "stop"
        if not out:
            db.mark_seen(c["fp"], c["link"], c["title"], "skipped", "AI rad etdi")
            st["ai_rad"] += 1
            return "skip"

        bg, ai_flag = None, False
        if out["image_prompt"]:
            bg = await generate_image(db, out["image_prompt"])
            ai_flag = bg is not None
        if bg is None and db.get("use_source_image") == "1" and page["image_url"]:
            bg = await download_image(client, page["image_url"])
        image = await asyncio.to_thread(create_news_image, out["category"], out["headline"], bg, ai_flag)

        art = {**out, "source_url": c["link"], "publisher": c["publisher"], "confirmed": fc["publishers"],
               "ai_image": ai_flag}
        status, err = await submit(self.bot, db, art, image, c)
        if status == "published":
            db.mark_seen(c["fp"], c["link"], c["title"], "published")
            st["nashr"] += 1
            await asyncio.sleep(2)
        elif status == "pending":
            db.mark_seen(c["fp"], c["link"], c["title"], "pending")
            st["tasdiqda"] += 1
        else:
            db.mark_seen(c["fp"], c["link"], c["title"], "failed", err, hours=6)
            st["nashr_xato"] += 1
        return "ok"

# ======================================================================
# SCHEDULER
# ======================================================================

JOB = "news_scan"


class NewsScheduler:
    def __init__(self, db: DB, newsroom):
        self.db = db
        self.newsroom = newsroom
        self.sched = AsyncIOScheduler(timezone=TZ)

    def _interval(self) -> int:
        return max(5, self.db.get_int("scan_interval"))

    def _times(self) -> list:
        out = []
        for t in (self.db.get("post_times") or "").split(","):
            t = t.strip()
            if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t):
                h, m = t.split(":")
                out.append((int(h), int(m)))
        return sorted(set(out))

    def _apply(self, first_run: bool = False):
        for job in list(self.sched.get_jobs()):
            self.sched.remove_job(job.id)
        times = self._times()
        if times:
            for h, m in times:
                self.sched.add_job(self._run_slot, CronTrigger(hour=h, minute=m, timezone=TZ),
                                   id=f"{JOB}_{h:02d}{m:02d}", max_instances=1, coalesce=True,
                                   misfire_grace_time=300)
        else:
            first = datetime.now(ZoneInfo(TZ)) + timedelta(seconds=60)
            self.sched.add_job(self._run, IntervalTrigger(minutes=self._interval()), id=JOB,
                               max_instances=1, coalesce=True, next_run_time=first)

    def start(self):
        self._apply(True)
        self.sched.start()

    def reschedule(self):
        self._apply()

    def next_run_text(self) -> str:
        nxt = [j.next_run_time for j in self.sched.get_jobs() if j.next_run_time]
        if nxt:
            return min(nxt).astimezone(ZoneInfo(TZ)).strftime("%H:%M")
        return "—"

    async def _run_slot(self):
        try:
            if self.db.get("paused") == "1":
                return
            res = await self.newsroom.scan(manual=False, slot=True)
            log.info("Vaqtli nashr: %s", res)
        except Exception:
            log.exception("Vaqtli nashr xatosi")

    async def _run(self):
        try:
            res = await self.newsroom.scan(manual=False)
            log.info("Rejali qidiruv: %s", res)
        except Exception:
            log.exception("Rejali qidiruv xatosi")

    def shutdown(self):
        self.sched.shutdown(wait=False)


# ======================================================================
# KONTENT MODULLARI VA ContentScheduler
# ======================================================================

# Modul ijrochilari: nom -> async def fn(bot, db) -> dict(status="ok"|"error"|"skipped", text=str, ref=str,
# message_id=int|None). Keyingi bosqichlarda har modul shu yerga o'zini ro'yxatdan o'tkazadi:
#     MODULE_RUNNERS["quiz"] = run_quiz
# Ijrochi o'zi takror (duplicate) tekshiruvini qiladi (db.content_exists) va kanalga nashr qiladi.
MODULE_RUNNERS: dict = {}
# Jadval bo'yicha faqat shu hafta kunlarida (0=Du ... 6=Yak) ishlaydigan modullar; qo'lda qo'yish cheklanmaydi
MODULE_DAYS = {"poll": {0, 2, 4, 6},   # haftada 4 marta
               "best": {6}}            # faqat yakshanba


async def run_module(name: str, bot, db: DB, newsroom) -> dict:
    """Bitta modulni ishga tushiradi (qo'lda yoki jadval bo'yicha). Hech qachon exception ko'tarmaydi."""
    title = module_title(name)
    if name in ("news", "tech"):
        ff = "tech" if name == "tech" else ""
        if ff and not any(ff in f["url"].lower() for f in db.list_feeds(enabled_only=True)):
            return {"status": "error", "text": "⚠️ Texnologiya RSS manbasi yo'q. «📡 Manbalar» ga URL'da «tech» bo'lgan "
                                               "RSS qo'shing (masalan BBC technology)."}
        res = await newsroom.scan(manual=True, slot=True, feed_filter=ff)
        status = "error" if res.get("error") else ("ok" if (res.get("nashr") or res.get("tasdiqda")) else "skipped")
        return {"status": status, "text": format_scan(res)}
    runner = MODULE_RUNNERS.get(name)
    if runner is None:
        return {"status": "skipped", "text": f"ℹ️ <b>{esc(title)}</b>: bu modul keyingi bosqichda qo'shiladi."}
    try:
        res = await runner(bot, db)
    except Exception as e:
        log.exception("modul xatosi: %s", name)
        res = {"status": "error", "text": f"⚠️ <b>{esc(title)}</b>: {esc(e.__class__.__name__)}: {esc(str(e)[:200])}"}
    status = res.get("status", "error")
    try:
        if not res.get("logged"):
            db.log_content(name, res.get("ref", ""), res.get("message_id"), status,
                           "" if status == "ok" else re.sub(r"<[^>]+>", "", res.get("text", "")))
    except Exception:
        log.exception("content_log yozilmadi")
    if status == "error":
        db.log("modul", f"{name}: {re.sub(r'<[^>]+>', '', res.get('text', ''))[:250]}")
    if not res.get("text"):
        res["text"] = f"{'✅' if status == 'ok' else 'ℹ️'} <b>{esc(title)}</b>: {status}"
    return res


class ContentScheduler:
    """Har yoqilgan modul uchun cron job (id = mod_<nom>_<HHMM>). Bazadagi sozlamadan tiklanadi,
    sync() takror chaqirilsa takroriy job yaratmaydi. NewsScheduler dan alohida scheduler ishlatadi."""

    PREFIX = "mod_"

    def __init__(self, bot, db: DB, newsroom):
        self.bot = bot
        self.db = db
        self.newsroom = newsroom
        self.sched = AsyncIOScheduler(timezone=TZ)
        self.running: set = set()

    def plan(self) -> dict:
        """Hozirgi sozlamalarga ko'ra kerakli joblar: {job_id: (modul, soat, daqiqa)}."""
        out = {}
        for name in MODULES:
            if not self.db.mod_on(name):
                continue
            for h, m in self.db.mod_times(name):
                out[f"{self.PREFIX}{name}_{h:02d}{m:02d}"] = (name, h, m)
        return out

    def sync(self):
        want = self.plan()
        have = {j.id for j in self.sched.get_jobs() if j.id.startswith(self.PREFIX)}
        for jid in have - set(want):
            self.sched.remove_job(jid)
        for jid, (name, h, m) in want.items():
            if jid in have:
                continue
            self.sched.add_job(self._fire, CronTrigger(hour=h, minute=m, timezone=TZ), args=[name], id=jid,
                               max_instances=1, coalesce=True, misfire_grace_time=300)

    def start(self):
        self.sync()
        self.sched.start()

    def shutdown(self):
        self.sched.shutdown(wait=False)

    async def run(self, name: str) -> dict:
        """Qo'lda yoki jadval bo'yicha ishga tushirish. Bir modul bir vaqtda faqat bitta marta ishlaydi."""
        if name in self.running:
            return {"status": "skipped", "text": f"ℹ️ <b>{esc(module_title(name))}</b>: allaqachon ishlayapti."}
        self.running.add(name)
        try:
            return await run_module(name, self.bot, self.db, self.newsroom)
        except Exception as e:
            log.exception("run xatosi: %s", name)
            return {"status": "error", "text": f"⚠️ <b>{esc(module_title(name))}</b>: {esc(e.__class__.__name__)}"}
        finally:
            self.running.discard(name)

    async def _fire(self, name: str):
        try:
            if self.db.get("paused") == "1" or not self.db.mod_on(name):
                return
            if name in MODULE_DAYS and tz_now().weekday() not in MODULE_DAYS[name]:
                return
            res = await self.run(name)
            log.info("Modul %s: %s", name, res.get("status"))
        except Exception:
            log.exception("Modul jadvali xatosi: %s", name)


# ======================================================================
# AVTOMATIK KONTENT (Bosqich 3): ertalabki post, tarix, fakt, poll, xulosa
# ======================================================================

UZ_MONTHS = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr",
             "noyabr", "dekabr"]
UZ_WEEKDAYS = ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]
TASHKENT = (41.2995, 69.2401)
CBU_URL = "https://cbu.uz/uz/arkhiv-kursov-valyut/json/"
RATE_CCY = ("USD", "EUR", "RUB")


def uz_date(d: datetime, weekday: bool = False) -> str:
    out = f"{d.day}-{UZ_MONTHS[d.month - 1]}"
    return out + f", {UZ_WEEKDAYS[d.weekday()]}" if weekday else out


def fmt_num(x: float, digits: int = 2) -> str:
    return f"{x:,.{digits}f}".replace(",", " ").replace(".", ",")


def weather_desc(code: int) -> str:
    if code == 0:
        return "☀️ Ochiq"
    if code == 1:
        return "🌤 Asosan ochiq"
    if code == 2:
        return "⛅ Qisman bulutli"
    if code == 3:
        return "☁️ Bulutli"
    if code in (45, 48):
        return "🌫 Tuman"
    if code in (51, 53, 55, 56, 57):
        return "🌦 Mayda yomg'ir"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "🌧 Yomg'ir"
    if code in (71, 73, 75, 77, 85, 86):
        return "🌨 Qor"
    if code in (95, 96, 99):
        return "⛈ Momaqaldiroq"
    return "🌡 Ob-havo"


async def fetch_json(url: str, params: dict | None = None, timeout: float = 20, retries: int = 2):
    """Tashqi API: timeout, 2 marta qayta urinish. Oxirgi xato ko'tariladi."""
    last = None
    async with httpx.AsyncClient(timeout=timeout, headers=UA, follow_redirects=True) as client:
        for i in range(retries + 1):
            try:
                r = await client.get(url, params=params)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last = e
                if i < retries:
                    await asyncio.sleep(1.5 * (i + 1))
    raise last


async def fetch_weather() -> dict:
    return await fetch_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": TASHKENT[0], "longitude": TASHKENT[1], "timezone": TZ, "forecast_days": 1,
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max"})


def parse_rates(data) -> list:
    """cbu.uz JSON -> [(ccy, rate, diff)] (USD, EUR, RUB tartibida)."""
    by = {}
    for it in data or []:
        try:
            by[it["Ccy"]] = (float(str(it["Rate"]).replace(",", ".")), float(str(it.get("Diff") or 0).replace(",", ".")))
        except (KeyError, ValueError, TypeError):
            continue
    return [(c, *by[c]) for c in RATE_CCY if c in by]


def build_morning(now: datetime, weather: dict | None, rates: list | None) -> str:
    lines = [f"🌅 <b>Xayrli tong!</b> {uz_date(now, True)}", ""]
    if weather:
        cur, day = weather.get("current", {}), weather.get("daily", {})
        t = cur.get("temperature_2m")
        lo, hi = (day.get("temperature_2m_min") or [None])[0], (day.get("temperature_2m_max") or [None])[0]
        rain = (day.get("precipitation_probability_max") or [None])[0]
        w = f"🏙 <b>Toshkent ob-havosi</b>\n{weather_desc(int(cur.get('weather_code', -1)))}"
        if t is not None:
            w += f", hozir {round(t):+d}°"
        if lo is not None and hi is not None:
            w += f"\n📊 Kun davomida: {round(lo):+d}° … {round(hi):+d}°"
        extra = []
        if rain is not None:
            extra.append(f"yog'ingarchilik ehtimoli {int(rain)}%")
        if cur.get("wind_speed_10m") is not None:
            extra.append(f"shamol {round(cur['wind_speed_10m'] / 3.6):d} m/s")
        if extra:
            w += "\n💨 " + ", ".join(extra)
        lines += [w, ""]
    if rates:
        lines.append("💵 <b>Valyuta kurslari</b> (Markaziy bank)")
        for ccy, rate, diff in rates:
            arrow = f"▲ +{fmt_num(diff)}" if diff > 0 else (f"▼ {fmt_num(diff)}" if diff < 0 else "•")
            lines.append(f"{ccy}: <b>{fmt_num(rate)}</b> so'm ({arrow})")
        lines.append("")
    lines.append(SIGNATURE)
    return "\n".join(lines)


# ---------- umumiy yuborish ----------
def _skipped(text: str) -> dict:
    return {"status": "skipped", "text": f"ℹ️ {text}"}


def _failed(text: str) -> dict:
    return {"status": "error", "text": f"⚠️ {text}"}


async def send_text_post(bot, db: DB, module: str, ref: str, text: str, link_preview=None) -> dict:
    """Matnli postni kanalga reaksiya tugmalari bilan yuboradi va content_log ga yozadi (takror tekshiruvi — chaqiruvchida)."""
    if not db.get("channel_id"):
        return _failed("Kanal ulanmagan. «📣 Kanal» bo'limidan ulang.")
    title = module_title(module)
    cid = db.log_content(module, ref, None, "pending")
    link = await post_link(bot, db)
    err = ""
    for _ in range(2):
        try:
            msg = await bot.send_message(
                chat_id=_chat(db), text=text, parse_mode="HTML",
                link_preview_options=link_preview or LinkPreviewOptions(is_disabled=True),
                reply_markup=reaction_keyboard(db, -cid, share_link(link) if link else ""))
            db.update_content(cid, "ok", msg.message_id)
            return {"status": "ok", "text": f"✅ <b>{esc(title)}</b>: kanalga joylandi", "ref": ref,
                    "message_id": msg.message_id, "logged": True}
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            err = "Telegram limiti"
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}"
            break
    db.update_content(cid, "error", None, err)
    db.log("telegram", f"{module} nashr xatosi: {err[:200]}")
    return {"status": "error", "text": f"⚠️ <b>{esc(title)}</b>: nashr xatosi — {esc(err[:150])}", "logged": True}


# ---------- 🌤 Ertalabki post ----------
async def run_morning(bot, db: DB) -> dict:
    now = tz_now()
    ref = now.date().isoformat()
    if db.content_exists("morning", ref):
        return _skipped("Ertalabki post bugun allaqachon qo'yilgan.")
    w, r = await asyncio.gather(fetch_weather(), fetch_json(CBU_URL), return_exceptions=True)
    weather = None if isinstance(w, Exception) else w
    rates = None if isinstance(r, Exception) else parse_rates(r)
    for name, v in (("ob-havo", w), ("valyuta", r)):
        if isinstance(v, Exception):
            db.log("morning", f"{name} olinmadi: {v.__class__.__name__}: {v}"[:250])
    if not weather and not rates:
        return _failed("Ob-havo ham, valyuta kursi ham olinmadi (manba javob bermadi). «🧾 Xatolar» ga qarang.")
    return await send_text_post(bot, db, "morning", ref, build_morning(now, weather, rates))


# ---------- AI yordamida o'zbekchalashtirish (tarix, fakt) ----------
AI_SYSTEM = ("Sen o'zbek tilidagi (lotin yozuvi) Telegram kanal muharririsan. Faqat senga berilgan ma'lumotga tayan, "
             "o'zingdan yangi fakt, sana yoki raqam qo'shma. Javobni faqat JSON ko'rinishida ber.")


async def ai_uzbek(db: DB, source_text: str, task: str) -> str:
    out = await generate_json(db, AI_SYSTEM, f"{task}\n\nManba matn:\n{source_text[:1500]}\n\n"
                                             'Javob formati: {"text": "..."}')
    text = _clean(out.get("text", ""))
    if len(text) < 30:
        raise ValueError("AI javobi juda qisqa")
    return text[:700]


# ---------- 📜 Bugun tarixda ----------
def pick_history_event(events: list, used: set) -> dict | None:
    good = [e for e in events if e.get("text") and e.get("year") is not None]
    good.sort(key=lambda e: -len(e.get("pages") or []))
    fresh = [e for e in good[:15] if f"{e['year']}:{e['text'][:40]}" not in used]
    return random.choice(fresh) if fresh else None


async def run_history(bot, db: DB) -> dict:
    now = tz_now()
    data = await fetch_json(f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/events/{now:%m}/{now:%d}")
    used = {r["ref"] for r in db._q("SELECT ref FROM content_log WHERE module='history' AND status='ok'")}
    ev = pick_history_event(data.get("events", []), used)
    if not ev:
        return _skipped("Bugun uchun yangi tarixiy voqea qolmadi.")
    ref = f"{ev['year']}:{ev['text'][:40]}"
    try:
        text = await ai_uzbek(db, f"{ev['year']}-yil: {ev['text']}",
                              "Bu tarixiy voqeani o'zbek tilida 2–3 qisqa gapda, aniq va tushunarli yoz.")
    except Exception as e:
        db.log("history", f"AI xatosi: {e.__class__.__name__}: {str(e)[:150]}")
        return _failed("AI ishlamadi (kalit yoki limit). Keyinroq urinib ko'ring.")
    page = ((ev.get("pages") or [{}])[0].get("content_urls") or {}).get("desktop", {}).get("page", "")
    src = f'\n\n📎 Manba: <a href="{html.escape(page, quote=True)}">Wikipedia</a>' if page else "\n\n📎 Manba: Wikipedia"
    body = f"📜 <b>Bugun tarixda — {uz_date(now)}</b>\n\n<b>{ev['year']}-yil.</b> {esc(text)}{src}\n\n{SIGNATURE}"
    return await send_text_post(bot, db, "history", ref, body)


# ---------- 💡 Kun fakti ----------
async def run_fact(bot, db: DB) -> dict:
    now = tz_now()
    cands = []
    try:
        feed = await fetch_json(f"https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{now:%Y}/{now:%m}/{now:%d}")
        if feed.get("tfa"):
            cands.append(feed["tfa"])
    except Exception as e:
        db.log("fact", f"featured olinmadi: {e.__class__.__name__}: {e}"[:200])
    for _ in range(4):
        try:
            cands.append(await fetch_json("https://en.wikipedia.org/api/rest_v1/page/random/summary"))
        except Exception:
            break
    pick = None
    for c in cands:
        title = (c.get("titles") or {}).get("normalized") or c.get("title") or ""
        if title and len(c.get("extract", "")) > 150 and c.get("type", "standard") == "standard" \
                and not db.content_exists("fact", title):
            pick = (title, c)
            break
    if not pick:
        return _failed("Mos maqola topilmadi (manba javob bermadi yoki hammasi oldin joylangan).")
    title, c = pick
    try:
        text = await ai_uzbek(db, f"{title}. {c['extract']}",
                              "Shu ma'lumotdan eng qiziqarli bitta faktni tanlab, o'zbek tilida 2–3 gapda yoz.")
    except Exception as e:
        db.log("fact", f"AI xatosi: {e.__class__.__name__}: {str(e)[:150]}")
        return _failed("AI ishlamadi (kalit yoki limit). Keyinroq urinib ko'ring.")
    page = (c.get("content_urls") or {}).get("desktop", {}).get("page", "")
    src = f'\n\n📎 Manba: <a href="{html.escape(page, quote=True)}">Wikipedia</a>' if page else "\n\n📎 Manba: Wikipedia"
    return await send_text_post(bot, db, "fact", title, f"💡 <b>Kun fakti</b>\n\n{esc(text)}{src}\n\n{SIGNATURE}")


# ---------- 🗳 Kun tanlovi (tayyor bank, AI shart emas) ----------
POLL_BANK = [
    ("Ertalabni nima bilan boshlaysiz?", ["☕ Choy", "☕ Qahva", "💧 Suv", "🍳 Nonushta"]),
    ("Qaysi fasl sizga ko'proq yoqadi?", ["🌸 Bahor", "☀️ Yoz", "🍂 Kuz", "❄️ Qish"]),
    ("Telefondan kuniga necha soat foydalanasiz?", ["1–2 soat", "3–4 soat", "5–6 soat", "6 soatdan ko'p"]),
    ("Dam olish kunini qanday o'tkazishni yoqtirasiz?", ["🏠 Uyda", "🌳 Tabiatda", "🛍 Savdo markazida", "👨‍👩‍👧 Mehmonda"]),
    ("Qaysi taom sevimlingiz?", ["🍚 Osh", "🥟 Manti", "🍢 Shashlik", "🍜 Lag'mon"]),
    ("Kitob o'qishga vaqt topasizmi?", ["Har kuni", "Haftada bir necha marta", "Oyda bir-ikki marta", "Deyarli yo'q"]),
    ("Yangiliklarni qayerdan ko'proq olasiz?", ["Telegram", "Instagram", "YouTube", "Veb-saytlar"]),
    ("Sun'iy intellektdan foydalanasizmi?", ["Har kuni", "Ba'zan", "Hali yo'q, lekin xohlayman", "Qiziqmayman"]),
    ("Qaysi transportni ko'proq tanlaysiz?", ["🚗 Shaxsiy mashina", "🚌 Jamoat transporti", "🚕 Taksi", "🚲 Velosiped / piyoda"]),
    ("Odatda soat nechada uxlaysiz?", ["22:00 gacha", "22:00–00:00", "00:00–02:00", "02:00 dan keyin"]),
    ("Chet tilini o'rganyapsizmi?", ["Ha, ingliz tilini", "Ha, boshqa tilni", "Hozircha yo'q, rejam bor", "Yo'q"]),
    ("Sport bilan shug'ullanasizmi?", ["Muntazam", "Ba'zan", "Faqat yurish", "Yo'q"]),
    ("Qaysi ichimlik sizga ko'proq yoqadi?", ["🍵 Ko'k choy", "☕ Qahva", "🥤 Gazli ichimlik", "🧃 Sharbat"]),
    ("Onlayn xarid qilasizmi?", ["Tez-tez", "Ba'zan", "Kamdan-kam", "Hech qachon"]),
    ("Kelajakda qaysi sohaga talab ko'proq bo'ladi deb o'ylaysiz?",
     ["IT va sun'iy intellekt", "Tibbiyot", "Qurilish", "Qishloq xo'jaligi"]),
    ("Eng yaxshi dam olish joyi qaysi?", ["⛰ Tog'lar", "🏖 Dengiz", "🏙 Katta shahar", "🏡 Qishloq"]),
    ("Qaysi vaqtda eng samarali ishlaysiz?", ["🌅 Ertalab", "☀️ Kunduzi", "🌆 Kechqurun", "🌙 Tunda"]),
    ("Telegramda nechta kanalni kuzatib borasiz?", ["1–3 ta", "4–10 ta", "10 tadan ortiq", "Faqat chatlar"]),
    ("Qaysi mavzudagi yangiliklar sizga ko'proq qiziq?", ["Texnologiya", "Sport", "Iqtisodiyot", "Fan va tabiat"]),
    ("Pul tejashga harakat qilasizmi?", ["Doim", "Ba'zan", "Qiyin", "Hali o'ylamaganman"]),
]


def pick_poll(used_counts: dict) -> int:
    """Eng kam ishlatilganlaridan tasodifiy bittasini tanlaydi (bank tugasa, aylanma boshlanadi)."""
    low = min(used_counts.get(i, 0) for i in range(len(POLL_BANK)))
    return random.choice([i for i in range(len(POLL_BANK)) if used_counts.get(i, 0) == low])


async def run_poll(bot, db: DB) -> dict:
    if not db.get("channel_id"):
        return _failed("Kanal ulanmagan. «📣 Kanal» bo'limidan ulang.")
    counts = {}
    for r in db._q("SELECT ref FROM content_log WHERE module='poll' AND status='ok'"):
        if str(r["ref"]).isdigit():
            counts[int(r["ref"])] = counts.get(int(r["ref"]), 0) + 1
    idx = pick_poll(counts)
    q, opts = POLL_BANK[idx]
    cid = db.log_content("poll", str(idx), None, "pending")
    try:
        msg = await bot.send_poll(chat_id=_chat(db), question=q, options=opts, is_anonymous=True)
    except Exception as e:
        err = f"{e.__class__.__name__}: {e}"
        db.update_content(cid, "error", None, err)
        db.log("telegram", f"poll xatosi: {err[:200]}")
        return {"status": "error", "text": f"⚠️ <b>Kun tanlovi</b>: nashr xatosi — {esc(err[:150])}", "logged": True}
    db.update_content(cid, "ok", msg.message_id)
    db._x("INSERT INTO daily_polls(question,options_json,posted_at) VALUES(?,?,?)",
          (q, json.dumps(opts, ensure_ascii=False), now_ts()))
    return {"status": "ok", "text": "✅ <b>Kun tanlovi</b>: kanalga joylandi", "ref": str(idx),
            "message_id": msg.message_id, "logged": True}


# ---------- 📋 Kunlik xulosa (bazadan, AI shart emas) ----------
def build_digest(now: datetime, posts: list, uname: str, db: DB) -> str:
    ranked = sorted(posts, key=lambda p: (-sum(db.reaction_counts(p["id"]).values()), -p["id"]))[:5]
    lines = [f"📋 <b>Kun xulosasi — {uz_date(now)}</b>", ""]
    for i, p in enumerate(ranked, 1):
        head = esc(p["headline"])
        if uname and p["channel_msg_id"]:
            head = f'<a href="https://t.me/{uname}/{p["channel_msg_id"]}">{head}</a>'
        lines.append(f"{i}. {head} <i>({esc(p['category'])})</i>")
    lines += ["", SIGNATURE]
    return "\n".join(lines)


async def run_digest(bot, db: DB) -> dict:
    now = tz_now()
    ref = now.date().isoformat()
    if db.content_exists("digest", ref):
        return _skipped("Kunlik xulosa bugun allaqachon qo'yilgan.")
    posts = db.today_published()
    if not posts:
        return _skipped("Bugun nashr qilingan yangilik yo'q, xulosa qilinmadi.")
    uname = db.get("channel_username")
    return await send_text_post(bot, db, "digest", ref, build_digest(now, posts, uname, db))


# ---------- 🏆 Haftaning eng yaxshi posti (reaksiya bo'yicha) ----------
def post_url(uname: str, msg_id) -> str:
    return f"https://t.me/{uname}/{msg_id}" if uname and msg_id else ""


def build_best(now: datetime, top: dict, counts: dict, uname: str) -> str:
    title = esc(top["title"])
    url = post_url(uname, top["msg_id"])
    if url:
        title = f'<a href="{url}">{title}</a>'
    parts = " ".join(f"{e} {counts[e]}" for e in REACTIONS.values() if counts.get(e))
    return (f"🏆 <b>Haftaning eng yaxshi posti</b>\n\n{title}\n\n{parts} — jami {top['n']} reaksiya\n\n"
            f"Rahmat, birga o'qiyotganingiz uchun! 🙌\n\n{SIGNATURE}")


async def run_best(bot, db: DB) -> dict:
    now = tz_now()
    iso = now.isocalendar()
    ref = f"{iso[0]}-W{iso[1]:02d}"
    if db.content_exists("best", ref):
        return _skipped("Haftaning eng yaxshi posti bu hafta allaqachon qo'yilgan.")
    top = db.ranked_posts(now_ts() - 7 * 86400, 1)
    if not top:
        return _skipped("Oxirgi 7 kunda reaksiya olgan post yo'q — tanlanmadi.")
    top = top[0]
    text = build_best(now, top, db.reaction_counts(top["pid"]), db.get("channel_username"))
    return await send_text_post(bot, db, "best", ref, text)


MODULE_RUNNERS.update({"best": run_best, "morning": run_morning, "history": run_history, "fact": run_fact,
                       "poll": run_poll, "digest": run_digest})


# ======================================================================
# QUIZ (Bosqich 2): savollarni KOD yaratadi, rasm Pillow bilan, javob botda tekshiriladi
# Ball, reyting va leaderboard YO'Q: faqat «Bugun X% to'g'ri topdi» umumiy foizi.
# ======================================================================

QUIZ_TYPES = ("seq", "odd", "matrix", "trend", "eq", "logic")
QUIZ_TYPE_TITLES = {"seq": "Raqamli ketma-ketlik", "odd": "Ortiqcha shakl", "matrix": "Matritsa",
                    "trend": "Naqsh davomi", "eq": "Belgili tenglamalar", "logic": "Mantiqiy savol"}
QUIZ_QUESTIONS = {"seq": "Ketma-ketlikning keyingi sonini toping", "odd": "Qaysi shakl ortiqcha?",
                  "matrix": "Yetishmayotgan katakni toping", "trend": "Naqsh davomini toping",
                  "eq": "«?» ning qiymatini toping", "logic": "Mantiqiy savol"}
QUIZ_LETTERS = "ABCDEF"
QUIZ_NO_REPEAT_DAYS = 30
QUIZ_PALETTE = ["#EF4444", "#3B82F6", "#22C55E", "#FACC15", "#A855F7", "#FB923C"]
QUIZ_COLOR_NAMES = ["qizil", "ko'k", "yashil", "sariq", "binafsha", "to'q sariq"]
QUIZ_SHAPES = ["tri", "sq", "pen", "hex", "cir", "star"]
QUIZ_SHAPE_NAMES = {"tri": "uchburchak", "sq": "kvadrat", "pen": "beshburchak", "hex": "oltiburchak",
                    "cir": "doira", "star": "yulduz"}
QUIZ_W = 1080
Q_BG, Q_CARD, Q_LINE, Q_TEXT, Q_MUTED, Q_ACCENT = "#0F172A", "#1E293B", "#334155", "#FFFFFF", "#94A3B8", "#FACC15"


def quiz_difficulty(weekday: int, rng: random.Random) -> int:
    """Haftalik reja: Du-Cho oson (1), Pa o'rta (2), Ju-Sha qiyin (3), Yak aralash."""
    if weekday <= 2:
        return 1
    if weekday == 3:
        return 2
    if weekday <= 5:
        return 3
    return rng.choice([1, 2, 3])


def _finish(rng, qtype: str, diff: int, params, opts: list, correct, expl: str, hint: str, spec: dict,
            shuffle: bool = True, correct_idx: int | None = None):
    """Savolni yakuniy shaklga keltiradi. Variantlar takrorlanmasligi va to'g'ri javob yagonaligi tekshiriladi."""
    keys = [json.dumps(o, sort_keys=True, ensure_ascii=False) for o in opts]
    if len(set(keys)) != len(keys):
        return None
    if correct_idx is None:
        ck = json.dumps(correct, sort_keys=True, ensure_ascii=False)
        if keys.count(ck) != 1:
            return None
        correct_idx = keys.index(ck)
    if shuffle:
        order = list(range(len(opts)))
        rng.shuffle(order)
        opts = [opts[i] for i in order]
        correct_idx = order.index(correct_idx)
    fp = hashlib.sha1((qtype + json.dumps(params, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()[:20]
    return {"qtype": qtype, "difficulty": diff, "fingerprint": fp, "title": QUIZ_QUESTIONS[qtype], "spec": spec,
            "opts": opts, "correct_idx": correct_idx, "explanation": expl, "hint": hint}


# ---------- 1. Raqamli ketma-ketlik ----------
def _seq_terms(rng, kind: str, n: int) -> tuple[list, str, str]:
    """n ta had qaytaradi (oxirgisi javob), qoida matni va ishora."""
    if kind == "arith":
        a, s = rng.randint(1, 25), rng.randint(2, 9)
        return [a + s * i for i in range(n)], f"Har keyingi son oldingisidan {s} ga katta.", "Qo'shni sonlar orasidagi farqqa qarang."
    if kind == "arith_neg":
        a, s = rng.randint(50, 99), rng.randint(3, 9)
        return [a - s * i for i in range(n)], f"Har keyingi son oldingisidan {s} ga kichik.", "Sonlar kamayapti: har safar qancha?"
    if kind == "geom":
        a, r = rng.randint(1, 4), rng.choice([2, 3])
        return [a * r ** i for i in range(n)], f"Har keyingi son oldingisidan {r} marta katta.", "Qo'shni sonlarni bir-biriga bo'lib ko'ring."
    if kind == "square":
        o = rng.randint(1, 6)
        return [(o + i) ** 2 for i in range(n)], "Bu ketma-ket sonlarning kvadratlari.", "Har bir son biror sonning kvadratimi?"
    if kind == "cube":
        o = rng.randint(1, 3)
        return [(o + i) ** 3 for i in range(n)], "Bu ketma-ket sonlarning kublari.", "Har bir son biror sonning kubimi?"
    if kind == "prod":
        o = rng.randint(1, 4)
        return [(o + i) * (o + i + 1) for i in range(n)], "Har bir had n·(n+1) ko'rinishida (ketma-ket ikki son ko'paytmasi).", "Har bir sonni ikkita qo'shni son ko'paytmasi sifatida yozing."
    if kind == "fib":
        a, b = rng.randint(1, 5), rng.randint(1, 6)
        t = [a, b]
        while len(t) < n:
            t.append(t[-1] + t[-2])
        return t, "Har keyingi son oldingi ikkitasining yig'indisi.", "Uchinchi son birinchi ikkitasi bilan qanday bog'langan?"
    if kind == "diff2":
        a, s = rng.randint(1, 8), rng.randint(1, 2)
        t = [a]
        for i in range(n - 1):
            t.append(t[-1] + s * (i + 1))
        return t, f"Ayirmalar {s}, {2 * s}, {3 * s}... tarzida ortib boradi.", "Ayirmalarning o'zi qanday o'zgarayapti?"
    if kind == "altmul":
        s, a = rng.randint(1, 5), rng.randint(2, 6)
        t = [s]
        for i in range(n - 1):
            t.append(t[-1] + a if i % 2 == 0 else t[-1] * 2)
        return t, f"Navbatma-navbat: {a} qo'shiladi, keyin 2 ga ko'paytiriladi.", "Ikkita turli amal navbatlashmoqda."
    if kind == "inter":
        a, p, b, q = rng.randint(1, 9), rng.randint(2, 5), rng.randint(20, 40), rng.randint(2, 6)
        t = []
        for i in range(n):
            t.append(a + p * (i // 2) if i % 2 == 0 else b + q * (i // 2))
        return t, f"Ikkita ketma-ketlik aralash: toq o'rindagilar +{p}, juft o'rindagilar +{q} dan o'sadi.", "Sonlarni bittadan oralatib ikki qatorga ajrating."
    primes = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31]
    o = rng.randint(0, 3)
    return primes[o:o + n], "Bular ketma-ket tub sonlar.", "Faqat 1 va o'ziga bo'linadigan sonlar."


SEQ_KINDS = {1: ["arith", "arith_neg", "geom"], 2: ["square", "fib", "diff2", "primes", "prod"],
             3: ["cube", "altmul", "inter", "diff2", "fib"]}


def gen_seq(rng, d):
    kind = rng.choice(SEQ_KINDS[d])
    vis = {"inter": 7, "fib": 6, "primes": 6}.get(kind, 5)
    terms, expl, hint = _seq_terms(rng, kind, vis + 1)
    if len(set(terms)) < len(terms) or max(terms) > 5000 or min(terms) < 0:
        return None
    ans, last, prev = terms[-1], terms[-2], terms[-3]
    wrong = [last + (last - prev), last * 2, last + 1, ans + (last - prev)]
    near = [ans + k for k in (1, -1, 2, -2, 3, -3, 10, -10)]
    cand = []
    for x in wrong + near:
        if x != ans and x > 0 and x not in cand and x not in terms[:-1]:
            cand.append(x)
    picks = cand[:2] + rng.sample(cand[2:], min(1, len(cand[2:])))
    if len(picks) < 3:
        return None
    opts = [{"t": str(x)} for x in [ans] + picks]
    spec = {"kind": "seq", "terms": terms[:-1]}
    return _finish(rng, "seq", d, {"k": kind, "t": terms[:-1]}, opts, {"t": str(ans)}, expl, hint, spec)


# ---------- 2. Ortiqcha shakl ----------
def _odd_unique(items: list, pos: int) -> bool:
    """Aynan bitta xususiyat bo'yicha 4 ta bir xil + 1 ta farqli bo'lsin va u pos dagi shakl bo'lsin."""
    found = []
    for a in ("k", "c", "d"):
        vals = [it[a] for it in items]
        for i in range(len(vals)):
            rest = vals[:i] + vals[i + 1:]
            if len(set(rest)) == 1 and vals[i] != rest[0]:
                found.append(i)
    return found == [pos]


def gen_odd(rng, d):
    attr = {1: "c", 2: "k", 3: "d"}[d]
    kinds = rng.sample(QUIZ_SHAPES, 4)
    colors = rng.sample(range(6), 4)
    for _ in range(300):
        pos = rng.randrange(5)
        items = []
        for _i in range(5):
            items.append({"k": rng.choice(kinds), "c": rng.choice(colors[:3]),
                          "d": rng.choice([1, 2, 3]) if attr == "d" else 0})
        if attr == "c":
            v, w = rng.sample(colors, 2)
        elif attr == "k":
            v, w = rng.sample(kinds, 2)
        else:
            v, w = rng.sample([1, 2, 3, 4], 2)
        for i, it in enumerate(items):
            it[attr] = w if i == pos else v
        if _odd_unique(items, pos):
            break
    else:
        return None
    L = QUIZ_LETTERS[pos]
    if attr == "c":
        expl = f"To'rtta shakl {QUIZ_COLOR_NAMES[v]} rangda, faqat {L} shakli {QUIZ_COLOR_NAMES[w]}."
        hint = "Shakllarning rangiga e'tibor bering."
    elif attr == "k":
        expl = f"To'rtta shakl {QUIZ_SHAPE_NAMES[v]}, faqat {L} shakli {QUIZ_SHAPE_NAMES[w]}."
        hint = "Ranglarga emas, shakllarning burchaklariga qarang."
    else:
        expl = f"To'rtta shaklning ichida {v} tadan nuqta bor, {L} shaklida esa {w} ta."
        hint = "Shakllarning ichidagi nuqtalarni sanang."
    opts = [{"t": QUIZ_LETTERS[i]} for i in range(5)]
    spec = {"kind": "odd", "items": items}
    return _finish(rng, "odd", d, {"i": items, "p": pos}, opts, None, expl, hint, spec, shuffle=False, correct_idx=pos)


# ---------- 3. Matritsa 3x3 ----------
def _mx_val(rule: str, r: int, c: int) -> int:
    return {"latinA": (r + c) % 3, "latinB": (r + 2 * c) % 3, "row": r, "col": c, "const": 0}[rule]


def gen_matrix(rng, d):
    attrs = ["k", "c", "d"]
    rng.shuffle(attrs)
    rules = {a: "const" for a in attrs}
    rules[attrs[0]] = "latinA"
    if d == 1:
        rules[attrs[1]] = rng.choice(["row", "col"])
    elif d == 2:
        rules[attrs[1]] = "latinB"
    else:
        rules[attrs[1]] = "latinB"
        rules[attrs[2]] = rng.choice(["row", "col"])
    vals = {"k": rng.sample(QUIZ_SHAPES, 3), "c": rng.sample(range(6), 3), "d": rng.sample([1, 2, 3], 3)}

    def cell(r, c):
        out = {}
        for a in ("k", "c", "d"):
            out[a] = 0 if (a == "d" and rules[a] == "const") else vals[a][_mx_val(rules[a], r, c)]
        return out
    cells = [[cell(r, c) for c in range(3)] for r in range(3)]
    ans = cells[2][2]
    pools = {"k": [x for x in QUIZ_SHAPES if x != ans["k"]], "c": [x for x in range(6) if x != ans["c"]],
             "d": [x for x in (0, 1, 2, 3) if x != ans["d"]]}
    one, two = [], []
    for a in ("k", "c", "d"):
        for v in pools[a]:
            o = dict(ans)
            o[a] = v
            one.append(o)
    for a, b in (("k", "c"), ("k", "d"), ("c", "d")):
        o = dict(ans)
        o[a], o[b] = rng.choice(pools[a]), rng.choice(pools[b])
        two.append(o)
    rng.shuffle(one)
    picks = []
    for o in one[:4] + two:
        if o != ans and o not in picks:
            picks.append(o)
        if len(picks) == 5:
            break
    if len(picks) < 5:
        return None
    names = {"k": "shakl turi", "c": "rang", "d": "nuqtalar soni"}
    parts = []
    for a in ("k", "c", "d"):
        r_ = rules[a]
        if r_ in ("latinA", "latinB"):
            parts.append(f"{names[a]} har qator va har ustunda bir martadan takrorlanadi")
        elif r_ == "row":
            parts.append(f"{names[a]} har qatorda bir xil")
        elif r_ == "col":
            parts.append(f"{names[a]} har ustunda bir xil")
    expl = "Qoida: " + "; ".join(parts) + "."
    flat = [dict(cells[r][c]) if (r, c) != (2, 2) else None for r in range(3) for c in range(3)]
    spec = {"kind": "matrix", "cells": flat}
    return _finish(rng, "matrix", d, {"r": rules, "v": vals}, [dict(ans)] + picks, dict(ans), expl,
                   "Har qator va har ustunni alohida kuzating: shakl, rang va nuqtalar.", spec)


# ---------- 4. Naqsh davomi ----------
def gen_trend(rng, d):
    if d == 1:
        ps, pc, pd = rng.choice([2, 3]), 1, 1
    elif d == 2:
        ps, pc, pd = rng.choice([(2, 3), (3, 2), (3, 3)]) + (1,)
    else:
        ps, pc, pd = rng.choice([(3, 2, 2), (2, 3, 2), (3, 3, 2)])
    kk = rng.sample(QUIZ_SHAPES, ps)
    cc = rng.sample(range(6), pc)
    dd = rng.sample([0, 1, 2, 3], pd) if pd > 1 else [0]

    def item(i):
        return {"k": kk[i % ps], "c": cc[i % pc], "d": dd[i % pd]}
    lc = ps * pc // math.gcd(ps, pc)
    lc = lc * pd // math.gcd(lc, pd)
    n = max(6, lc + 3)
    shown = [item(i) for i in range(n)]
    ans = item(n)
    cand = []
    for j in (n + 1, n + 2, n - 1):
        o = item(j)
        if o != ans and o not in cand:
            cand.append(o)
    extra = []
    for a, pool in (("k", [x for x in QUIZ_SHAPES if x != ans["k"]]), ("c", [x for x in range(6) if x != ans["c"]])):
        o = dict(ans)
        o[a] = rng.choice(pool)
        extra.append(o)
    for o in extra:
        if o != ans and o not in cand:
            cand.append(o)
    if len(cand) < 3:
        return None
    picks = cand[:3]
    expl = f"Shakllar har {ps} tadan" + (f", ranglar har {pc} tadan" if pc > 1 else "") + \
           (f", nuqtalar har {pd} tadan" if pd > 1 else "") + " takrorlanadi."
    spec = {"kind": "trend", "items": shown}
    return _finish(rng, "trend", d, {"p": [ps, pc, pd], "k": kk, "c": cc, "d": dd, "n": n},
                   [dict(ans)] + picks, dict(ans), expl, "Qaysi element qancha tadan keyin takrorlanishini toping.", spec)


# ---------- 5. Belgili tenglamalar ----------
def gen_eq(rng, d):
    sym = rng.sample(range(6), 3)  # shakl turi = QUIZ_SHAPES[sym[i]], rangi mos indeks
    hi = {1: 9, 2: 12, 3: 15}[d]
    va, vb, vc = rng.sample(range(2, hi + 1), 3)
    ca = rng.randint(2, 4)
    if d == 1:
        eqs = [{"t": [[0, ca]], "v": ca * va}, {"t": [[0, 1], [1, rng.randint(1, 3)]], "v": 0}]
        eqs[1]["v"] = va + eqs[1]["t"][1][1] * vb
        final = [[0, rng.randint(1, 2)], [1, rng.randint(1, 2)]]
    else:
        m = rng.randint(1, 2)
        n_ = rng.randint(1, 3)
        p_, q_ = rng.randint(1, 3), rng.randint(1, 3)
        eqs = [{"t": [[0, ca]], "v": ca * va}, {"t": [[0, m], [1, n_]], "v": m * va + n_ * vb},
               {"t": [[1, p_], [2, q_]], "v": p_ * vb + q_ * vc}]
        final = [[0, rng.randint(1, 2)], [1, rng.randint(1, 2)], [2, rng.randint(1, 2)]]
    vals = [va, vb, vc]
    ans = sum(c * vals[i] for i, c in final)
    if any(sum(c for _i, c in e["t"]) > 7 for e in eqs) or sum(c for _i, c in final) > 7:
        return None
    wrong_a = sum(c * ((vals[i] + 1) if i == 0 else vals[i]) for i, c in final)
    cand = []
    for x in (wrong_a, ans + 1, ans - 1, ans + 2, ans - 2, ans + 3, ans - 3, ans + 5):
        if x != ans and x > 0 and x not in cand:
            cand.append(x)
    picks = cand[:1] + rng.sample(cand[1:], 2)
    names = [QUIZ_SHAPE_NAMES[QUIZ_SHAPES[s]] for s in sym]
    used = sorted({i for e in eqs for i, _c in e["t"]} | {i for i, _c in final})
    expl = ", ".join(f"{names[i]} = {vals[i]}" for i in used) + f". Demak, «?» = {ans}."
    spec = {"kind": "eq", "sym": sym, "eqs": eqs, "final": final}
    return _finish(rng, "eq", d, {"s": sym, "e": eqs, "f": final}, [{"t": str(x)} for x in [ans] + picks],
                   {"t": str(ans)}, expl, "Avval faqat bitta belgi bor tenglamadan boshlang.", spec)


# ---------- 6. Mantiqiy savollar (oldindan tayyorlangan, tekshirilgan bank) ----------
# (qiyinlik, savol, variantlar (birinchisi TO'G'RI), tushuntirish, ishora)
LOGIC_BANK = [
    (1, "Fermerning 17 ta qo'yi bor edi. 9 tasidan tashqari hammasi o'ldi. Nechta qo'y tirik qoldi?",
     ["9", "8", "17", "0"], "«9 tasidan tashqari hammasi» — demak 9 tasi tirik qoldi.", "Savolning so'zlariga diqqat bilan qarang."),
    (1, "Poygada ikkinchi o'rindagini quvib o'tdingiz. Siz nechanchi o'rindasiz?",
     ["2-o'rinda", "1-o'rinda", "3-o'rinda", "4-o'rinda"], "Ikkinchini quvib o'tib, uning o'rniga chiqdingiz — 2-o'rin.", "Siz kimning o'rniga o'tdingiz?"),
    (1, "Ali Validan baland, Vali Karimdan baland. Eng past bo'yli kim?",
     ["Karim", "Vali", "Ali", "Aniqlab bo'lmaydi"], "Ali > Vali > Karim, eng pasti — Karim.", "Uchalasini bo'yi bo'yicha qatorga tizing."),
    (1, "Agar ertaga payshanba bo'lsa, kechagi kun qaysi?",
     ["Seshanba", "Dushanba", "Chorshanba", "Juma"], "Ertaga payshanba bo'lsa, bugun chorshanba, kecha — seshanba.", "Avval bugun qaysi kunligini toping."),
    (1, "Bir sonning yarmiga 10 qo'shsak, o'sha sonning o'ziga teng bo'ladi. Bu son nechaga teng?",
     ["20", "10", "15", "40"], "20: yarmi 10, 10+10=20.", "Sonning yarmi va o'zi orasidagi farq nechaga teng?"),
    (2, "Bir ishchi ishni 6 kunda, ikkinchisi 3 kunda bajaradi. Birgalikda ishni necha kunda bajaradi?",
     ["2 kun", "3 kun", "4,5 kun", "9 kun"], "Bir kunda 1/6 + 1/3 = 1/2 qismini bajaradi — 2 kun.", "Har birining bir kunlik ulushini qo'shing."),
    (2, "Ruchka va daftar birga 11 000 so'm. Daftar ruchkadan 10 000 so'm qimmat. Ruchka necha so'm?",
     ["500", "1 000", "1 500", "2 000"], "Ruchka x bo'lsa, 2x + 10 000 = 11 000, demak x = 500.", "Javob «1 000» emas, tekshirib ko'ring."),
    (2, "Ko'lni nilufar qoplaydi, uning maydoni har kuni ikki baravar ortadi. 48 kunda butun ko'l qoplansa, yarmi necha kunda qoplangan?",
     ["47-kunda", "24-kunda", "46-kunda", "40-kunda"], "Bir kun oldin maydon yarim edi — 47-kun.", "Oxirgi kunda nima bo'ldi?"),
    (2, "Otaning va o'g'ilning yoshi yig'indisi 50. Ota o'g'ildan 4 marta katta. O'g'il necha yoshda?",
     ["10", "8", "12", "15"], "x + 4x = 50, x = 10.", "O'g'il yoshini x deb belgilang."),
    (2, "Uchta ketma-ket toq sonning yig'indisi 27. Eng kattasi qaysi?",
     ["11", "9", "13", "15"], "7 + 9 + 11 = 27, eng kattasi 11.", "O'rtadagi son yig'indining uchdan biriga teng."),
    (2, "Qutida 5 ta qizil va 5 ta ko'k shar bor. Ko'zni yumib kamida nechta shar olsangiz, albatta bir xil rangdagi ikkita shar chiqadi?",
     ["3", "2", "5", "6"], "Uchinchi sharda albatta bir rang takrorlanadi (Dirixle prinsipi).", "Eng yomon holatni tasavvur qiling."),
    (2, "Barcha mushuklar hayvon. Ba'zi hayvonlar uyda yashaydi. Qaysi xulosa albatta to'g'ri?",
     ["Barcha mushuklar hayvon", "Barcha mushuklar uyda yashaydi", "Ba'zi mushuklar uyda yashaydi", "Hech bir mushuk uyda yashamaydi"],
     "Faqat birinchi gap berilgan shartdan bevosita kelib chiqadi.", "«Albatta» so'ziga e'tibor bering."),
    (2, "Qaysi son ortiqcha: 2, 3, 5, 9, 11?",
     ["9", "2", "5", "11"], "Qolganlari tub sonlar, 9 esa 3 ga bo'linadi.", "Bo'linuvchilarini sanab ko'ring."),
    (3, "Soat 3 marta urilganda zarbalar 3 soniyada eshitildi. 6 marta urilganda zarbalar necha soniyada eshitiladi?",
     ["7,5", "6", "9", "5"], "3 zarba — 2 oraliq = 3 s, oraliq 1,5 s. 6 zarba — 5 oraliq = 7,5 s.", "Zarbalar emas, oraliqlarni sanang."),
    (3, "1 dan 100 gacha bo'lgan sonlarni yozishda 9 raqami necha marta uchraydi?",
     ["20", "11", "19", "21"], "Birlar xonasida 10 ta (9, 19, ... 99), o'nlar xonasida 10 ta (90–99).", "Birlar va o'nlar xonasini alohida sanang."),
    (3, "Bugun dushanba. 100 kundan keyin haftaning qaysi kuni bo'ladi?",
     ["Chorshanba", "Seshanba", "Payshanba", "Juma"], "100 = 14·7 + 2, dushanbadan 2 kun keyin — chorshanba.", "100 ni 7 ga bo'lib, qoldiqqa qarang."),
    (3, "Soat 9:15 da soat va minut strelkalari orasidagi kichik burchak necha gradus?",
     ["172,5°", "90°", "165°", "180°"], "Soat strelkasi 277,5°, minut strelkasi 90°: farq 187,5°, kichik burchak 360° − 187,5° = 172,5°.",
     "Soat strelkasi 9 da emas, biroz oldinga siljigan."),
    (3, "Sahifalari 1 dan 50 gacha raqamlangan kitobda jami nechta raqam ishlatilgan?",
     ["91", "100", "90", "95"], "1–9: 9 ta raqam, 10–50: 41 ta ikki xonali son = 82 ta. Jami 91.", "Bir va ikki xonali sonlarni alohida sanang."),
]


def gen_logic(rng, d):
    pool = [q for q in LOGIC_BANK if q[0] == d] or LOGIC_BANK
    diff, text, opts, expl, hint = rng.choice(pool)
    fp_params = {"q": text}
    labels = [{"t": o} for o in opts]
    return _finish(rng, "logic", diff, fp_params, labels, {"t": opts[0]}, expl, hint, {"kind": "logic", "text": text})


QUIZ_GENERATORS = {"seq": gen_seq, "odd": gen_odd, "matrix": gen_matrix, "trend": gen_trend, "eq": gen_eq,
                   "logic": gen_logic}


def pick_quiz(db: DB, rng: random.Random, weekday: int | None = None) -> dict | None:
    """Yangi savol tanlaydi: eng uzoq ishlatilmagan tur, 30 kun ichida takrorlanmaydi. Bazaga yoziladi."""
    wd = tz_now().weekday() if weekday is None else weekday
    diff = quiz_difficulty(wd, rng)
    last = db.quiz_last_types()
    types = sorted(QUIZ_TYPES, key=lambda t: (last.get(t, 0), rng.random()))
    for t in types:
        for _ in range(25):
            q = QUIZ_GENERATORS[t](rng, diff)
            if not q:
                continue
            if db.quiz_recent(q["fingerprint"], QUIZ_NO_REPEAT_DAYS):
                continue
            row = db.quiz_by_fp(q["fingerprint"])
            q["id"] = row["id"] if row else db.quiz_add(q)
            if row:  # 30 kundan oldin chiqqan savol qayta ishlatiladi: saqlangan shakli olinadi
                return quiz_load(db.quiz_get(q["id"]))
            return q
    return None


def quiz_load(row) -> dict | None:
    if not row:
        return None
    data = json.loads(row["options_json"])
    return {"id": row["id"], "qtype": row["qtype"], "difficulty": row["difficulty"], "title": row["question"],
            "spec": data["spec"], "opts": data["opts"], "hint": data.get("hint", ""), "correct_idx": row["correct_idx"],
            "explanation": row["explanation"], "fingerprint": row["fingerprint"]}


# ---------- Rasm (Pillow, 1080x1080) ----------
def _lum(hexcol: str) -> float:
    r, g, b = int(hexcol[1:3], 16), int(hexcol[3:5], 16), int(hexcol[5:7], 16)
    return 0.299 * r + 0.587 * g + 0.114 * b


def _poly(cx, cy, r, n, rot=-90):
    return [(cx + r * math.cos(math.radians(rot + 360 * i / n)), cy + r * math.sin(math.radians(rot + 360 * i / n)))
            for i in range(n)]


def draw_quiz_shape(d: ImageDraw.ImageDraw, s: dict, cx: float, cy: float, size: float):
    col = QUIZ_PALETTE[s["c"]]
    r = size / 2
    k = s["k"]
    if k == "cir":
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)
    elif k == "sq":
        d.rounded_rectangle((cx - r * .88, cy - r * .88, cx + r * .88, cy + r * .88), radius=int(r * .14), fill=col)
    elif k == "tri":
        d.polygon(_poly(cx, cy + r * .12, r * 1.08, 3), fill=col)
    elif k == "pen":
        d.polygon(_poly(cx, cy + r * .04, r * 1.02, 5), fill=col)
    elif k == "hex":
        d.polygon(_poly(cx, cy, r * 1.02, 6, rot=0), fill=col)
    else:
        pts = []
        for i in range(10):
            rr = r * 1.05 if i % 2 == 0 else r * .45
            a = math.radians(-90 + 36 * i)
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        d.polygon(pts, fill=col)
    n = int(s.get("d", 0) or 0)
    if n:
        dc = "#0F172A" if _lum(col) > 140 else "#FFFFFF"
        dr = max(3, r * .13)
        spots = {1: [(0, .1)], 2: [(-.28, .1), (.28, .1)], 3: [(-.3, .22), (.3, .22), (0, -.18)],
                 4: [(-.28, -.1), (.28, -.1), (-.28, .3), (.28, .3)]}[min(n, 4)]
        for dx, dy in spots:
            d.ellipse((cx + dx * r - dr, cy + dy * r - dr, cx + dx * r + dr, cy + dy * r + dr), fill=dc)


def _ctext(d, xy, text, size, fill=Q_TEXT):
    d.text(xy, text, font=font(size), fill=fill, anchor="mm")


def _tile(d, x, y, w, h, fill=Q_CARD, outline=Q_LINE):
    d.rounded_rectangle((x, y, x + w, y + h), radius=int(min(w, h) * .12), fill=fill, outline=outline, width=3)


def _letter_badge(d, cx, cy, letter, r=30):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=Q_ACCENT)
    _ctext(d, (cx, cy - 2), letter, int(r * 1.3), "#0F172A")


def _shape_tiles(d, shapes: list, y: int, tile: int, gap: int, letters: bool = True):
    n = len(shapes)
    x0 = (QUIZ_W - (n * tile + (n - 1) * gap)) / 2
    for i, s in enumerate(shapes):
        x = x0 + i * (tile + gap)
        _tile(d, x, y, tile, tile)
        if s is None:
            _ctext(d, (x + tile / 2, y + tile / 2), "?", int(tile * .6), Q_ACCENT)
        else:
            draw_quiz_shape(d, s, x + tile / 2, y + tile / 2, tile * .62)
        if letters:
            _letter_badge(d, x + tile / 2, y + tile + 38, QUIZ_LETTERS[i], r=26)


def _text_cards(d, labels: list, y: int, cols: int, h: int, gap: int = 30, width: int = 980, size: int = 64):
    n = len(labels)
    rows = (n + cols - 1) // cols
    w = (width - gap * (cols - 1)) / cols
    x0 = (QUIZ_W - width) / 2
    for i, lab in enumerate(labels):
        r_, c_ = divmod(i, cols)
        x, yy = x0 + c_ * (w + gap), y + r_ * (h + gap)
        _tile(d, x, yy, w, h)
        _letter_badge(d, x + 52, yy + h / 2, QUIZ_LETTERS[i], r=30)
        fs = size
        while fs > 26 and d.textlength(lab, font=font(fs)) > w - 130:
            fs -= 4
        d.text((x + 100 + (w - 120) / 2, yy + h / 2), lab, font=font(fs), fill=Q_TEXT, anchor="mm")
    return y + rows * (h + gap) - gap


def render_quiz(q: dict) -> bytes:
    """Savol rasmi (PNG, 1080x1080). Rasm saqlanmaydi: spec dan har safar qayta chiziladi (disk yo'qolsa ham ishlaydi)."""
    img = Image.new("RGB", (QUIZ_W, QUIZ_W), Q_BG)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, QUIZ_W, 14), fill=Q_ACCENT)
    d.text((60, 52), "XABAR HUB  •  QUIZ", font=font(32), fill=Q_MUTED)
    d.text((QUIZ_W - 60, 52), norm(QUIZ_TYPE_TITLES[q["qtype"]]).upper(), font=font(28), fill=Q_MUTED, anchor="ra")
    spec, kind, opts = q["spec"], q["spec"]["kind"], q["opts"]
    if kind != "logic":
        ttl, fs = norm(q["title"]), 60
        while fs > 30 and d.textlength(ttl, font=font(fs)) > 960:
            fs -= 2
        _ctext(d, (QUIZ_W / 2, 150), ttl, fs)
    if kind == "seq":
        chips = [str(t) for t in spec["terms"]] + ["?"]
        fnt = font(86)
        widths = [int(d.textlength(c, font=fnt)) + 64 for c in chips]
        rows, cur, cw = [], [], 0
        for c, w in zip(chips, widths):
            if cur and cw + w + 20 > 960:
                rows.append(cur)
                cur, cw = [], 0
            cur.append((c, w))
            cw += w + 20
        rows.append(cur)
        y = 260 + (380 - len(rows) * 150) / 2
        for row in rows:
            tw = sum(w for _c, w in row) + 20 * (len(row) - 1)
            x = (QUIZ_W - tw) / 2
            for c, w in row:
                last = c == "?"
                d.rounded_rectangle((x, y, x + w, y + 130), radius=28, fill=Q_ACCENT if last else Q_CARD,
                                    outline=Q_LINE, width=3)
                d.text((x + w / 2, y + 62), c, font=fnt, fill="#0F172A" if last else Q_TEXT, anchor="mm")
                x += w + 20
            y += 150
        _text_cards(d, [o["t"] for o in opts], 700, 2, 130, gap=30, size=70)
    elif kind == "odd":
        _shape_tiles(d, spec["items"], 380, 180, 20)
        _ctext(d, (QUIZ_W / 2, 760), "Qaysi harf ostidagi shakl ortiqcha?", 40, Q_MUTED)
    elif kind == "matrix":
        cell, gap = 150, 14
        x0 = (QUIZ_W - (3 * cell + 2 * gap)) / 2
        for i, s in enumerate(spec["cells"]):
            r_, c_ = divmod(i, 3)
            x, y = x0 + c_ * (cell + gap), 215 + r_ * (cell + gap)
            _tile(d, x, y, cell, cell)
            if s is None:
                _ctext(d, (x + cell / 2, y + cell / 2), "?", 100, Q_ACCENT)
            else:
                draw_quiz_shape(d, s, x + cell / 2, y + cell / 2, cell * .62)
        _shape_tiles(d, opts_shapes(opts), 745, 150, 20)
    elif kind == "trend":
        items = spec["items"] + [None]
        n = len(items)
        gap = 10
        tile = int(min(120, (980 - gap * (n - 1)) / n))
        _shape_tiles(d, items, 300, tile, gap, letters=False)
        _shape_tiles(d, opts_shapes(opts), 640, 190, 40)
    elif kind == "eq":
        rows = []
        for e in spec["eqs"]:
            rows.append((e["t"], str(e["v"]), False))
        rows.append((spec["final"], "?", True))
        y = 250
        for terms, rhs, is_final in rows:
            elems = []
            for j, (si, cnt) in enumerate(terms):
                for u in range(cnt):
                    if elems:
                        elems.append(("+", 44))
                    elems.append((("shape", spec["sym"][si]), 80))
            elems.append(("=", 64))
            rf = font(78)
            elems.append((("num", rhs), int(d.textlength(rhs, font=rf)) + 50 if not is_final else 100))
            tw = sum(w for _e, w in elems) + 12 * (len(elems) - 1)
            x = (QUIZ_W - tw) / 2
            for e, w in elems:
                cx, cy = x + w / 2, y + 45
                if e == "+" or e == "=":
                    _ctext(d, (cx, cy), e, 64, Q_MUTED)
                elif e[0] == "shape":
                    draw_quiz_shape(d, {"k": QUIZ_SHAPES[e[1]], "c": e[1], "d": 0}, cx, cy, 76)
                else:
                    if is_final:
                        d.rounded_rectangle((x, y - 8, x + w, y + 98), radius=22, fill=Q_ACCENT)
                    _ctext(d, (cx, cy), e[1], 78, "#0F172A" if is_final else Q_TEXT)
                x += w + 12
            y += 130 if not is_final else 0
            if is_final:
                y += 0
        _text_cards(d, [o["t"] for o in opts], 840, 4, 120, gap=16, size=60)
    else:  # logic
        fnt = font(50)
        lines = wrap(d, norm(spec["text"]), fnt, 960)
        y = 150
        for ln in lines[:7]:
            d.text((60, y), ln, font=fnt, fill=Q_TEXT)
            y += 66
        labels = [norm(o["t"]) for o in opts]
        _text_cards(d, labels, y + 50, 1, 100, gap=16, size=48)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def opts_shapes(opts: list) -> list:
    return [o["s"] if "s" in o else o for o in opts]


# ---------- Kanalga nashr ----------
def quiz_channel_caption(q: dict) -> str:
    return (f"🧩 <b>Kun quizi</b> · {esc(QUIZ_TYPE_TITLES[q['qtype']])}\n\n{esc(q['title'])}\n\n"
            f"Javobingiz qaysi? 👇 Botda tekshirib ko'ring.\n\n{SIGNATURE}")


async def quiz_bot_username(bot) -> str:
    global _bot_username
    if not _bot_username:
        try:
            _bot_username = (await bot.me()).username or ""
        except Exception:
            pass
    return _bot_username


def quiz_channel_kb(db: DB, cid: int, qid: int, uname: str, share: str):
    extra = [[InlineKeyboardButton(text="💡 Javobni botda tekshiring", url=f"https://t.me/{uname}?start=quiz_{qid}")]] \
        if uname else []
    kbd = reaction_keyboard(db, -cid, share, extra_rows=extra)
    if kbd is None and extra:
        kbd = InlineKeyboardMarkup(inline_keyboard=extra)
    return kbd


async def run_quiz(bot, db: DB) -> dict:
    if not db.get("channel_id"):
        return _failed("Kanal ulanmagan. «📣 Kanal» bo'limidan ulang.")
    q = pick_quiz(db, random.Random())
    if not q:
        return _failed("Yangi quiz savoli yaratib bo'lmadi (hammasi oxirgi 30 kunda chiqqan).")
    try:
        png = await asyncio.to_thread(render_quiz, q)
    except Exception as e:
        log.exception("quiz rasmi chizilmadi")
        return _failed(f"Quiz rasmi chizilmadi: {esc(e.__class__.__name__)}")
    cid = db.log_content("quiz", f"q{q['id']}", None, "pending")
    uname = await quiz_bot_username(bot)
    link = await post_link(bot, db)
    err = ""
    for _ in range(2):
        try:
            msg = await bot.send_photo(chat_id=_chat(db), photo=BufferedInputFile(png, "quiz.png"),
                                       caption=quiz_channel_caption(q), parse_mode="HTML",
                                       reply_markup=quiz_channel_kb(db, cid, q["id"], uname, share_link(link) if link else ""))
            db.update_content(cid, "ok", msg.message_id)
            db.quiz_record_post(q["id"], msg.message_id)
            return {"status": "ok", "text": f"✅ <b>{esc(module_title('quiz'))}</b>: {esc(QUIZ_TYPE_TITLES[q['qtype']])} "
                                            f"(qiyinlik {q['difficulty']}) kanalga joylandi",
                    "ref": f"q{q['id']}", "message_id": msg.message_id, "logged": True}
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            err = "Telegram limiti"
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}"
            break
    db.update_content(cid, "error", None, err)
    db.log("telegram", f"quiz nashr xatosi: {err[:200]}")
    return {"status": "error", "text": f"⚠️ <b>Quiz</b>: nashr xatosi — {esc(err[:150])}", "logged": True}


MODULE_RUNNERS["quiz"] = run_quiz


# ---------- Botda javob tekshirish (ochiq router: har kim foydalanadi) ----------
def quiz_answer(db: DB, qid: int, uid: int, idx: int) -> dict | None:
    """Javobni qabul qiladi (har foydalanuvchidan faqat birinchi javob sanaladi). Ball YO'Q: faqat umumiy foiz."""
    q = quiz_load(db.quiz_get(qid))
    if not q or not (0 <= idx < len(q["opts"])):
        return None
    first = db.quiz_answer_add(qid, uid, idx, idx == q["correct_idx"])
    mine = db.quiz_my_answer(qid, uid)
    my_idx = mine["idx"] if mine else idx
    ok = my_idx == q["correct_idx"]
    return {"q": q, "first": first, "ok": ok, "my_idx": my_idx, "stat": quiz_stat_line(db, qid)}


def quiz_stat_line(db: DB, qid: int) -> str:
    t_all, r_all, t_day, r_day = db.quiz_stats(qid)
    if t_day >= 3:
        return f"📊 Bugun {round(100 * r_day / t_day)}% to'g'ri topdi ({t_day} kishi)."
    if t_all >= 3:
        return f"📊 Javob berganlarning {round(100 * r_all / t_all)}% i to'g'ri topdi ({t_all} kishi)."
    return "📊 Statistika hali to'planmoqda."


def quiz_result_text(res: dict) -> str:
    q = res["q"]
    right = QUIZ_LETTERS[q["correct_idx"]]
    head = "✅ <b>To'g'ri!</b>" if res["ok"] else f"❌ <b>Noto'g'ri.</b> To'g'ri javob: <b>{right}</b>"
    again = "" if res["first"] else "\n<i>(Siz oldin javob bergansiz — birinchi javobingiz sanaladi.)</i>"
    return f"{head}{again}\n\n💬 {esc(q['explanation'])}\n\n{res['stat']}"


def quiz_answer_kb(q: dict, hint: bool = True) -> InlineKeyboardMarkup:
    n = len(q["opts"])
    per = 3 if n == 6 else (5 if n == 5 else 2 if n == 4 and q["qtype"] in ("seq", "logic") else n)
    letters = [InlineKeyboardButton(text=QUIZ_LETTERS[i], callback_data=f"qz:{q['id']}:{i}") for i in range(n)]
    rows = [letters[i:i + per] for i in range(0, n, per)]
    if hint and q.get("hint"):
        rows.append([InlineKeyboardButton(text="💡 Ishora", callback_data=f"qzh:{q['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quiz_private_caption(q: dict, hint: bool = False) -> str:
    cap = f"🧩 <b>{esc(q['title'])}</b>\n\nJavob variantini tanlang:"
    if hint:
        cap += f"\n\n💡 <b>Ishora:</b> {esc(q['hint'])}"
    return cap


qrouter = Router()


class QuizStart(BaseFilter):
    async def __call__(self, m: Message) -> bool:
        return bool(m.text and m.text.startswith("/start quiz_"))


@qrouter.message(QuizStart())
async def quiz_start(m: Message, db: DB):
    try:
        qid = int(m.text.split("quiz_", 1)[1].split()[0])
    except (ValueError, IndexError):
        await m.answer("Savol topilmadi.")
        return
    q = quiz_load(db.quiz_get(qid))
    if not q:
        await m.answer("Bu savol topilmadi.")
        return
    mine = db.quiz_my_answer(qid, m.from_user.id)
    if mine:  # oldin javob bergan: natijani qayta ko'rsatamiz
        res = {"q": q, "first": True, "ok": mine["idx"] == q["correct_idx"], "my_idx": mine["idx"],
               "stat": quiz_stat_line(db, qid)}
        await m.answer(quiz_result_text(res))
        return
    try:
        png = await asyncio.to_thread(render_quiz, q)
        await m.answer_photo(BufferedInputFile(png, "quiz.png"), caption=quiz_private_caption(q),
                             reply_markup=quiz_answer_kb(q))
    except Exception:
        log.exception("quiz savoli yuborilmadi")
        await m.answer("Savolni yuborib bo'lmadi, birozdan keyin urinib ko'ring.")


@qrouter.callback_query(lambda c: bool(c.data) and c.data.startswith("qz:"))
async def cb_quiz_answer(c: CallbackQuery, db: DB):
    try:
        _, qid, idx = c.data.split(":")
        res = quiz_answer(db, int(qid), c.from_user.id, int(idx))
    except ValueError:
        res = None
    if not res:
        await c.answer("Savol topilmadi")
        return
    await c.answer("✅ To'g'ri!" if res["ok"] else "❌ Noto'g'ri")
    text = quiz_result_text(res)
    try:
        await c.message.edit_caption(caption=text, reply_markup=None)
    except (TelegramAPIError, AttributeError):
        try:
            await c.message.answer(text)
        except TelegramAPIError:
            pass


@qrouter.callback_query(lambda c: bool(c.data) and c.data.startswith("qzh:"))
async def cb_quiz_hint(c: CallbackQuery, db: DB):
    try:
        q = quiz_load(db.quiz_get(int(c.data.split(":")[1])))
    except ValueError:
        q = None
    if not q or db.quiz_my_answer(q["id"], c.from_user.id):
        await c.answer("Ishora mavjud emas")
        return
    await c.answer()
    try:
        await c.message.edit_caption(caption=quiz_private_caption(q, hint=True), reply_markup=quiz_answer_kb(q, hint=False))
    except (TelegramAPIError, AttributeError):
        pass


# ======================================================================
# VIDEO (Bosqich 4): faqat YouTube HAVOLASI (yuklab olinmaydi). Manba va muallif ko'rsatiladi.
# Oqim: YouTube Data API v3 -> qoida bilan saralash -> AI baholash (faqat nomzodlar) -> kanalga havola.
# ======================================================================

YT_API = "https://www.googleapis.com/youtube/v3"
YT_PARTS = "snippet,contentDetails,statistics,status"
YT_REGIONS = ("UZ", "RU", "TR", "US")  # birinchisi asosiy, qolganlari zaxira hududlar
YT_QUERIES = ("qiziqarli faktlar", "foydali maslahatlar", "ilm-fan kashfiyotlar", "kulgili lahzalar",
              "texnologiya yangiliklari", "sayohat go'zal joylar", "hayvonlar qiziqarli", "o'zbek madaniyati")
VIDEO_AI_MAX = 4          # bir ishga tushishda ko'pi bilan nechta AI so'rovi
VIDEO_MIN_SECONDS = 15
VIDEO_CATEGORIES = ("foydali", "qiziqarli", "kulgili", "trend")
VIDEO_TARGET = {"useful": 0.5, "funny": 0.3, "trend": 0.2}  # mavzu nisbati: 50% / 30% / 20%
VIDEO_BAD_WORDS = re.compile(r"\b(18\+|porn\w*|xxx|nude|sex\w*|gore|beheading|suicide|o'z joniga)\b", re.I)


class YouTubeError(Exception):
    pass


def yt_key() -> str:
    return os.getenv("YOUTUBE_API_KEY", "").strip()


def parse_iso_duration(s: str) -> int:
    """'PT1H2M3S' -> soniya. Jonli efir ('P0D') yoki noto'g'ri qiymat -> 0."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s or "")
    if not m:
        return 0
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


async def yt_get(endpoint: str, params: dict) -> dict:
    """YouTube API so'rovi. Kalit xato matniga HECH QACHON tushmaydi."""
    p = dict(params)
    p["key"] = yt_key()
    try:
        return await fetch_json(f"{YT_API}/{endpoint}", p, timeout=20, retries=1)
    except Exception as e:
        resp = getattr(e, "response", None)
        code = getattr(resp, "status_code", None)
        reason = ""
        try:
            reason = resp.json()["error"]["errors"][0]["reason"]
        except Exception:
            pass
        if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            msg = "YouTube kunlik kvotasi tugadi (ertaga tiklanadi)"
        elif reason in ("keyInvalid", "API_KEY_INVALID") or code == 400:
            msg = "YouTube kaliti noto'g'ri yoki YouTube Data API v3 yoqilmagan"
        elif code == 403:
            msg = f"YouTube ruxsat bermadi ({reason or '403'}): kalit cheklovlarini tekshiring"
        else:
            msg = f"YouTube API xatosi: {code or e.__class__.__name__}"
        raise YouTubeError(msg) from None


def yt_check(it: dict, min_views: int, max_sec: int) -> str:
    """Majburiy filtrlar. Bo'sh satr = o'tdi, aks holda rad sababi."""
    sn, cd, st, sx = it.get("snippet", {}), it.get("contentDetails", {}), it.get("status", {}), it.get("statistics", {})
    if st.get("privacyStatus") != "public" or st.get("uploadStatus", "processed") != "processed":
        return "ochiq emas"
    if st.get("embeddable") is not True:
        return "embed taqiqlangan"
    if sn.get("liveBroadcastContent", "none") != "none":
        return "jonli efir"
    if any(cd.get("contentRating", {}).values()):
        return "yosh cheklovi"
    rr = cd.get("regionRestriction", {})
    if "UZ" in rr.get("blocked", []) or ("allowed" in rr and "UZ" not in rr["allowed"]):
        return "O'zbekistonda yopiq"
    sec = parse_iso_duration(cd.get("duration", ""))
    if not (VIDEO_MIN_SECONDS <= sec <= max_sec):
        return "davomiyligi mos emas"
    try:
        views = int(sx.get("viewCount", 0))
    except (TypeError, ValueError):
        views = 0
    if views < min_views:
        return "ko'rishlar kam"
    if VIDEO_BAD_WORDS.search(f"{sn.get('title', '')} {sn.get('channelTitle', '')}"):
        return "taqiqlangan so'z"
    return ""


def yt_rule_score(it: dict) -> float:
    """Qoidaviy ball (AI'siz): ko'rishlar (logarifm), yoqtirishlar nisbati, qisqa video uchun bonus."""
    sx = it.get("statistics", {})
    try:
        views, likes = int(sx.get("viewCount", 0)), int(sx.get("likeCount", 0))
    except (TypeError, ValueError):
        views, likes = 0, 0
    ratio = min(likes / views, 0.06) if views else 0
    sec = parse_iso_duration(it.get("contentDetails", {}).get("duration", ""))
    return math.log10(views + 1) * 10 + ratio * 200 + (5 if sec <= 60 else 0)


async def yt_candidates(db: DB, rng: random.Random) -> tuple[list, Counter]:
    """Saralangan nomzodlar (qoida bali bo'yicha) va rad sabablari hisobi."""
    min_views, max_sec = db.get_int("video_min_views"), db.get_int("video_max_seconds")
    items, last_err = {}, None
    for region in YT_REGIONS:
        try:
            data = await yt_get("videos", {"part": YT_PARTS, "chart": "mostPopular", "regionCode": region,
                                           "maxResults": 50})
        except YouTubeError as e:
            last_err = e
            if "kvota" in str(e):
                break
            continue
        for it in data.get("items", []):
            items[it["id"]] = it

    def screen() -> tuple[list, Counter]:
        good, why = [], Counter()
        for vid, it in items.items():
            if db.video_exists(vid) or db.content_exists("video", vid):
                why["oldin joylangan"] += 1
            elif db.video_rejected(vid):
                why["oldin rad etilgan"] += 1
            else:
                r = yt_check(it, min_views, max_sec)
                if r:
                    why[r] += 1
                else:
                    good.append(it)
        return good, why
    good, why = screen()
    if len(good) < 5:  # chart yetmasa — kalit so'z bo'yicha qidiruv (100 kvota, shuning uchun faqat zarur bo'lsa)
        since = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for q in rng.sample(YT_QUERIES, 2):
            try:
                s = await yt_get("search", {"part": "id", "type": "video", "q": q, "maxResults": 25,
                                            "order": "viewCount", "videoEmbeddable": "true", "videoDuration": "short",
                                            "safeSearch": "strict", "publishedAfter": since})
                ids = [x["id"]["videoId"] for x in s.get("items", []) if x.get("id", {}).get("videoId")]
                ids = [i for i in ids if i not in items][:50]
                if ids:
                    d = await yt_get("videos", {"part": YT_PARTS, "id": ",".join(ids)})
                    for it in d.get("items", []):
                        items[it["id"]] = it
            except YouTubeError as e:
                last_err = e
                break
        good, why = screen()
    if not items and last_err:
        raise last_err
    good.sort(key=yt_rule_score, reverse=True)
    seen_ch, out = set(), []
    for it in good:  # avval turli kanallar, keyin qolganlari
        ch = it["snippet"].get("channelId") or it["snippet"].get("channelTitle")
        if ch not in seen_ch:
            out.append(it)
            seen_ch.add(ch)
    taken = {it["id"] for it in out}
    out += [it for it in good if it["id"] not in taken]
    return out, why


# ---------- AI baholash ----------
VIDEO_SYSTEM = (
    "Sen XABAR HUB Telegram kanalining video muharririsan. Senga YouTube videosining FAQAT matnli ma'lumoti beriladi "
    "(sarlavha, tavsif, kanal, ko'rishlar, davomiyligi). Videoning o'zini ko'rmading, shuning uchun mazmunini o'ylab topma "
    "va «videoda ... ko'rsatiladi» kabi da'vo qilma: faqat berilgan ma'lumotga tayan. Faqat JSON qaytar."
)


def video_prompt(it: dict) -> str:
    sn, sx = it["snippet"], it.get("statistics", {})
    sec = parse_iso_duration(it["contentDetails"].get("duration", ""))
    return (
        f"Sarlavha: {sn.get('title', '')}\nKanal: {sn.get('channelTitle', '')}\n"
        f"Tavsif: {(sn.get('description') or '')[:600]}\n"
        f"Ko'rishlar: {sx.get('viewCount', '?')}, yoqtirishlar: {sx.get('likeCount', '?')}, davomiyligi: {sec} soniya\n\n"
        "O'zbek tilida o'qiydigan keng auditoriya uchun kanal postiga mosligini bahola. JSON maydonlari:\n"
        '{"score": 0-100, "category": "foydali|qiziqarli|kulgili|trend", "interesting": true/false, "useful": true/false, '
        '"funny": true/false, "harmful": true/false (zo\'ravonlik, 18+, nafrat, xavfli yoki aldamchi bo\'lsa true), '
        '"copyright_status": "safe|unknown|risky" (boshqa birovning kontentini qayta yuklagan, film/serial/musiqa '
        'parchasi bo\'lsa risky), "uzbek_title": "qisqa o\'zbekcha sarlavha (80 belgigacha)", '
        '"short_description": "1–2 gap o\'zbekcha tavsif, faqat berilgan ma\'lumotdan", "rejection_reason": "rad etsang sababi"}'
    )


def video_group(cat: str) -> str:
    return {"foydali": "useful", "qiziqarli": "useful", "kulgili": "funny", "trend": "trend"}.get(cat, "useful")


def video_preferred_group(recent: list) -> str:
    """Oxirgi nashrlarga qarab nisbatdan eng orqada qolgan guruhni tanlaydi (50/30/20)."""
    if not recent:
        return "useful"
    n = len(recent)
    share = {g: sum(1 for c in recent if video_group(c) == g) / n for g in VIDEO_TARGET}
    return max(VIDEO_TARGET, key=lambda g: VIDEO_TARGET[g] - share[g])


def video_verdict(data: dict, min_score: int) -> dict:
    """AI javobini me'yorlaydi. 'ok' True bo'lsagina nashr qilinadi."""
    def b(x):
        return x is True or str(x).lower() == "true"
    try:
        score = max(0, min(100, int(float(data.get("score", 0)))))
    except (TypeError, ValueError):
        score = 0
    cat = str(data.get("category", "")).strip().lower()
    cat = cat if cat in VIDEO_CATEGORIES else "qiziqarli"
    title, desc = _clean(data.get("uzbek_title"))[:100], _clean(data.get("short_description"))[:280]
    cr = str(data.get("copyright_status", "unknown")).strip().lower()
    reason = _clean(data.get("rejection_reason"))[:150]
    if b(data.get("harmful")):
        reason = reason or "zararli kontent"
    elif cr == "risky":
        reason = reason or "mualliflik huquqi xavfi"
    elif score < min_score:
        reason = reason or f"ball past ({score}<{min_score})"
    elif len(title) < 5 or len(desc) < 10:
        reason = "sarlavha/tavsif yaratilmadi"
    else:
        reason = ""
    return {"ok": not reason, "reason": reason, "score": score, "category": cat, "title": title, "desc": desc,
            "copyright": cr}


def video_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def build_video_post(it: dict, v: dict) -> str:
    return (f"🎬 <b>{esc(v['title'])}</b>\n\n{esc(v['desc'])}\n\n"
            f"Manba: YouTube — {esc(it['snippet'].get('channelTitle', ''))}\n"
            f"{video_url(it['id'])}\n\n{SIGNATURE}")


async def run_video(bot, db: DB) -> dict:
    if not db.get("channel_id"):
        return _failed("Kanal ulanmagan. «📣 Kanal» bo'limidan ulang.")
    if not yt_key():
        return _failed("YOUTUBE_API_KEY topilmadi. Hosting Environment'ga qo'shing va botni qayta ishga tushiring.")
    rng = random.Random()
    try:
        cands, why = await yt_candidates(db, rng)
    except YouTubeError as e:
        db.log("youtube", str(e))
        return _failed(str(e))
    if not cands:
        top = ", ".join(f"{k}: {n}" for k, n in why.most_common(4)) or "nomzod kelmadi"
        return _skipped(f"<b>Video</b>: mos video topilmadi ({esc(top)}). Sozlamalarda ko'rishlar yoki davomiylik "
                        f"chegarasini yumshatib ko'ring.")
    min_score = db.get_int("video_min_score")
    pref = video_preferred_group(db.video_recent_categories(10))
    passed, rejected, ai_err = [], 0, ""
    for it in cands[:VIDEO_AI_MAX]:
        try:
            data = await generate_json(db, VIDEO_SYSTEM, video_prompt(it))
        except Exception as e:
            ai_err = f"{e.__class__.__name__}"
            db.log("video", f"AI baholay olmadi: {str(e)[:150]}")
            break  # AI ishlamayapti: qolgan nomzodlarga ham urinmaymiz (limit tejaladi)
        v = video_verdict(data if isinstance(data, dict) else {}, min_score)
        if not v["ok"]:
            db.video_reject(it["id"], v["reason"])
            rejected += 1
            continue
        passed.append((it, v))
        if video_group(v["category"]) == pref:
            break
    if not passed:
        if ai_err:
            return _failed(f"<b>Video</b>: AI baholay olmadi ({esc(ai_err)}). AI kalitlari va limitni tekshiring; "
                           f"zararli-zararsizligi tekshirilmagan video joylanmaydi.")
        return _skipped(f"<b>Video</b>: {rejected} ta nomzod AI bahosidan o'tmadi (minimal ball {min_score}). "
                        f"Keyingi urinishda boshqalari ko'riladi.")
    it, v = next((p for p in passed if video_group(p[1]["category"]) == pref), max(passed, key=lambda p: p[1]["score"]))
    res = await send_text_post(bot, db, "video", it["id"], build_video_post(it, v),
                               link_preview=LinkPreviewOptions(is_disabled=False, url=video_url(it["id"]),
                                                               prefer_large_media=True, show_above_text=False))
    if res.get("status") == "ok":
        try:
            db.video_add(it["id"], v["title"], it["snippet"].get("channelTitle", ""), video_url(it["id"]),
                         v["score"], v["category"])
        except sqlite3.IntegrityError:
            pass
        res["text"] = (f"✅ <b>{esc(module_title('video'))}</b>: «{esc(v['title'])}» joylandi "
                       f"(ball {v['score']}, {v['category']})")
    return res


MODULE_RUNNERS["video"] = run_video


# ======================================================================
# ADMIN PANEL
# ======================================================================

router = Router()
public = Router()


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
                                                 for row in rows])


BACK = [("⬅️ Menyu", "m:main")]


class Form(StatesGroup):
    add_keys = State()
    del_keys = State()
    channel = State()
    feed = State()
    setting = State()
    mod_times = State()


class IsAdmin(BaseFilter):
    async def __call__(self, event, db: DB) -> bool:
        return bool(event.from_user and db.is_admin(event.from_user.id))


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

_tasks: set = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def show(ev, text: str, markup=None):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text[:4000], reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await ev.message.answer(text[:4000], reply_markup=markup)
        await ev.answer()
    else:
        await ev.answer(text[:4000], reply_markup=markup)


async def send_long(bot: Bot, chat_id: int, text: str, markup=None):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for i, ch in enumerate(chunks):
        await bot.send_message(chat_id, ch, reply_markup=markup if i == len(chunks) - 1 else None)


# ====================== BOSH MENYU ======================
def main_text(db: DB, sched: NewsScheduler) -> str:
    mods_on = sum(1 for n in MODULES if db.mod_on(n))
    keys = []
    for p in PROVIDERS:
        c = db.count_keys(p)
        if sum(c.values()):
            keys.append(f"{PROVIDER_TITLES[p]}: {c['active']} faol")
    ch = db.get("channel_id")
    chan = f"{esc(db.get('channel_title') or ch)}" if ch else "❌ ulanmagan"
    s = db.stats()
    return (
        "📡 <b>XABAR HUB NEWSROOM</b>\n\n"
        f"Holat: {'⏸ Pauza' if db.get('paused') == '1' else '▶️ Faol'}\n"
        f"Kanal: {chan}\n"
        f"Kalitlar: {', '.join(keys) if keys else '❌ yo`q'}\n"
        f"Rejim: {'avto-nashr' if db.get('auto_publish') == '1' else 'admin tasdig`i bilan'}\n"
        f"Bugun: {s['today']}/{db.get_int('daily_limit')} post\n"
        f"Modullar: {mods_on}/{len(MODULES)} yoqiq\n"
        f"Keyingi qidiruv: {sched.next_run_text()}"
    )


def main_kb(db: DB):
    paused = db.get("paused") == "1"
    return kb(
        [("🚀 Hozir post qo'yish", "post:now")],
        [("📰 Hozir qidirish", "scan:now"), ("📊 Statistika", "st:menu")],
        [("🔑 API kalitlar", "k:menu"), ("📣 Kanal", "c:menu")],
        [("🧩 Modullar", "mod:menu"), ("📡 Manbalar (RSS)", "f:menu")],
        [("⚙️ Sozlamalar", "s:menu")],
        [("📋 Postlar", "p:menu"), ("🧾 Xatolar", "e:menu")],
        [("▶️ Davom ettirish" if paused else "⏸ Pauza", "t:pause")],
    )


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message, state: FSMContext, db: DB, sched: NewsScheduler):
    await state.clear()
    await m.answer(main_text(db, sched), reply_markup=main_kb(db))


@router.callback_query(lambda c: c.data == "m:main")
async def cb_main(c: CallbackQuery, state: FSMContext, db: DB, sched: NewsScheduler):
    await state.clear()
    await show(c, main_text(db, sched), main_kb(db))


@public.callback_query(lambda c: bool(c.data) and c.data.startswith("rx:"))
async def cb_react(c: CallbackQuery, db: DB):
    try:
        _, pid, code = c.data.split(":")
        pid = int(pid)
        emoji = REACTIONS[code]
    except (ValueError, KeyError):
        await c.answer()
        return
    if not db.add_reaction(pid, c.from_user.id, emoji):
        await c.answer("Siz bu postga allaqachon baho bergansiz 🙂")
        return
    await c.answer(f"{emoji} qabul qilindi")
    try:  # sanoqni yangilash; mavjud «Do'stga yuborish» havolasi saqlanadi
        share, extra = "", []
        for row in (c.message.reply_markup.inline_keyboard if c.message and c.message.reply_markup else []):
            for b in row:
                if getattr(b, "url", None):
                    if b.url.startswith("https://t.me/share"):
                        share = b.url
                    elif "start=quiz_" in b.url:  # quiz posti: «Javobni botda tekshiring» tugmasi saqlanadi
                        extra.append([InlineKeyboardButton(text=b.text, url=b.url)])
        await c.message.edit_reply_markup(reply_markup=reaction_keyboard(db, pid, share, extra_rows=extra))
    except (TelegramAPIError, AttributeError):
        pass


@public.message(CommandStart())
async def public_start(m: Message, db: DB, sched: NewsScheduler):
    if m.from_user and not db.admin_ids():
        db.add_admin(m.from_user.id)
        db.log("admin", f"birinchi admin: {m.from_user.id}")
        await m.answer("✅ Siz admin bo'ldingiz.\n\n" + main_text(db, sched), reply_markup=main_kb(db))
    else:
        await m.answer("XABAR HUB NEWSROOM. Boshqaruv faqat adminlar uchun.")


@router.callback_query(lambda c: c.data == "t:pause")
async def cb_pause(c: CallbackQuery, db: DB, sched: NewsScheduler):
    db.set("paused", "0" if db.get("paused") == "1" else "1")
    await show(c, main_text(db, sched), main_kb(db))


# ====================== QIDIRISH ======================
def format_scan(res: dict) -> str:
    if res.get("error"):
        return f"⚠️ {esc(res['error'])}"
    if res.get("skipped"):
        return f"ℹ️ O'tkazib yuborildi: {esc(res['skipped'])}"
    names = {"topildi": "Topildi", "nashr": "✅ Nashr qilindi", "tasdiqda": "⏳ Tasdiq kutmoqda",
             "manba_kam": "Manba yetmadi", "ai_rad": "AI rad etdi", "ai_ishlamadi": "⚠️ AI ishlamadi",
             "nashr_xato": "⚠️ Nashr xatosi", "xato": "⚠️ Xato"}
    lines = [f"{names[k]}: {res[k]}" for k in names if res.get(k)]
    return "📰 <b>Qidiruv tugadi</b>\n" + ("\n".join(lines) if lines else "Yangi yangilik topilmadi.")


async def do_scan(bot: Bot, chat_id: int, newsroom: Newsroom):
    res = await newsroom.scan(manual=True)
    await bot.send_message(chat_id, format_scan(res), reply_markup=kb(BACK))


def post_menu_kb():
    items = [(f"pn:{n}", t or MODULES[n][0]) for n, t in POST_MENU]
    rows = [[(items[i][1], items[i][0])] + ([(items[i + 1][1], items[i + 1][0])] if i + 1 < len(items) else [])
            for i in range(0, len(items), 2)]
    rows.append([("🚀 Hammasini ketma-ket", "pn:all")])
    rows.append(BACK)
    return kb(*rows)


_all_running = False


async def do_module(bot: Bot, chat_id: int, content: ContentScheduler, name: str):
    """Bitta turdagi postni darhol chiqaradi; natija (muvaffaqiyat yoki sabab) admin chatga alohida xabar."""
    res = await content.run(name)
    await bot.send_message(chat_id, res.get("text") or "—", reply_markup=kb(BACK) if name != "news" else None)


async def do_all(bot: Bot, chat_id: int, content: ContentScheduler):
    global _all_running
    if _all_running:
        await bot.send_message(chat_id, "ℹ️ «Hammasini ketma-ket» allaqachon ishlayapti.")
        return
    _all_running = True
    try:
        names = [n for n, _t in POST_MENU]
        for i, name in enumerate(names):
            await do_module(bot, chat_id, content, name)
            if i < len(names) - 1:
                await asyncio.sleep(random.randint(30, 60))
        await bot.send_message(chat_id, "✅ Ketma-ket nashr tugadi.", reply_markup=kb(BACK))
    except Exception:
        log.exception("do_all xatosi")
    finally:
        _all_running = False


@router.callback_query(lambda c: c.data == "post:now")
async def cb_post_now(c: CallbackQuery):
    await show(c, "🚀 <b>Hozir post qo'yish</b>\n\nQaysi turdagi postni darhol chiqaramiz? "
                  "(Modul yoqilgan-yoqilmaganiga qaramaydi; takror tekshiruvi ishlaydi.)", post_menu_kb())


@router.callback_query(lambda c: c.data.startswith("pn:"))
async def cb_post_now_item(c: CallbackQuery, bot: Bot, content: ContentScheduler):
    name = c.data.split(":", 1)[1]
    if name == "all":
        await c.answer("🚀 Ketma-ket nashr boshlandi (har tur orasida 30–60 soniya)")
        spawn(do_all(bot, c.message.chat.id, content))
    elif name == "news" or name in MODULES:
        await c.answer(f"{module_title(name)} tayyorlanmoqda, bir necha daqiqa ketishi mumkin")
        spawn(do_module(bot, c.message.chat.id, content, name))
    else:
        await c.answer("Noma'lum tur")


# ====================== MODULLAR ======================
def modules_view(db: DB):
    lines = ["🧩 <b>Modullar</b>\n",
             "📰 Yangilik — vaqti «⚙️ Sozlamalar» dagi <i>post_times</i> / interval orqali\n"]
    rows = []
    for n, (title, _t) in MODULES.items():
        on = db.mod_on(n)
        times = db.get(f"mod_{n}_times") or "vaqt yo'q"
        lines.append(f"{'✅' if on else '⛔'} {title} — <b>{esc(times)}</b>")
        rows.append([(f"{'✅' if on else '⛔'} {title}", f"mod:t:{n}"), ("🕐 Vaqt", f"mod:e:{n}")])
    lines.append("\n✅/⛔ — yoqish/o'chirish. 🕐 — vaqtni o'zgartirish (Toshkent vaqti).")
    rows.append(BACK)
    return "\n".join(lines), kb(*rows)


@router.callback_query(lambda c: c.data == "mod:menu")
async def cb_modules(c: CallbackQuery, state: FSMContext, db: DB):
    await state.clear()
    text, markup = modules_view(db)
    await show(c, text, markup)


@router.callback_query(lambda c: c.data.startswith("mod:t:"))
async def cb_module_toggle(c: CallbackQuery, db: DB, content: ContentScheduler):
    name = c.data.split(":", 2)[2]
    if name not in MODULES:
        await c.answer("Noma'lum modul")
        return
    db.set(f"mod_{name}_on", "0" if db.mod_on(name) else "1")
    content.sync()
    text, markup = modules_view(db)
    await show(c, text, markup)


@router.callback_query(lambda c: c.data.startswith("mod:e:"))
async def cb_module_edit(c: CallbackQuery, state: FSMContext, db: DB):
    name = c.data.split(":", 2)[2]
    if name not in MODULES:
        await c.answer("Noma'lum modul")
        return
    await state.set_state(Form.mod_times)
    await state.update_data(mod=name)
    await show(c, f"{MODULES[name][0]}\nHozirgi vaqtlar: <code>{esc(db.get(f'mod_{name}_times') or '—')}</code>\n\n"
                  "Yangi vaqtlarni HH:MM ko'rinishida vergul bilan yuboring (Toshkent vaqti). "
                  "Masalan: 08:00 yoki 10:00,15:30\nVaqtlarni o'chirish: - yuboring.",
               kb([("❌ Bekor", "mod:menu")]))


@router.message(Form.mod_times)
async def msg_module_times(m: Message, state: FSMContext, db: DB, content: ContentScheduler):
    name = (await state.get_data()).get("mod")
    if name not in MODULES:
        await state.clear()
        return
    val = normalize_times(m.text or "")
    if val is None:
        await m.answer("Format: 09:00,13:00,18:30 (soat 0–23, daqiqa 00–59). Yoki - yuboring.")
        return
    db.set(f"mod_{name}_times", val)
    content.sync()
    await state.clear()
    await m.answer(f"✅ Saqlandi: {MODULES[name][0]} — <b>{esc(val or 'vaqt yo`q')}</b>",
                   reply_markup=kb([("🧩 Modullar", "mod:menu")]))


@router.callback_query(lambda c: c.data == "scan:now")
async def cb_scan(c: CallbackQuery, bot: Bot, newsroom: Newsroom):
    await c.answer("🔎 Qidiruv boshlandi, bir necha daqiqa ketishi mumkin")
    spawn(do_scan(bot, c.message.chat.id, newsroom))


# ====================== API KALITLAR ======================
def key_status(r) -> str:
    if r["status"] == "dead":
        return "❌"
    return "⏳" if r["cooldown_until"] > time.time() else "✅"


def mask(k: str) -> str:
    return k[:4] + "…" + k[-4:]


@router.callback_query(lambda c: c.data == "k:menu")
async def cb_keys(c: CallbackQuery, state: FSMContext, db: DB):
    await state.clear()
    lines = ["🔑 <b>API kalitlar</b>\n"]
    for p in PROVIDERS:
        cn = db.count_keys(p)
        lines.append(f"{PROVIDER_TITLES[p]}: ✅{cn['active']} ⏳{cn['cooling']} ❌{cn['dead']}")
    lines.append("\nKalitlar navbat bilan ishlatiladi; limitga yetsa keyingisiga o'tadi. "
                 "Gemini kalitini bir nechta (50+) qo'shishingiz mumkin.")
    await show(c, "\n".join(lines), kb(
        [("➕ Kalit qo'shish", "k:add"), ("📋 Ro'yxat", "k:list")],
        [("🧪 Tekshirish", "k:test"), ("♻️ Qayta tiklash", "k:revive")],
        [("🗑 O'chirish", "k:del")], BACK))


@router.callback_query(lambda c: c.data == "k:add")
async def cb_key_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.add_keys)
    await show(c, "Kalit(lar)ni yuboring. Ko'p bo'lsa — hammasini bitta xabarda, har birini yangi qatordan.\n\n"
                  "Turi avtomatik aniqlanadi:\n• Gemini: AIza… yoki AQ.…\n• Groq: gsk_…\n• OpenRouter: sk-or-…\n\n"
                  "Xavfsizlik uchun xabaringiz o'chiriladi.", kb([("❌ Bekor", "k:menu")]))


@router.message(Form.add_keys)
async def msg_key_add(m: Message, state: FSMContext, db: DB):
    text = m.text or ""
    found = {}
    for prov, pat in KEY_PATTERNS.items():
        keys = list(dict.fromkeys(re.findall(pat, text)))
        if keys:
            found[prov] = keys
    try:
        await m.delete()
    except TelegramAPIError:
        pass
    if not found:
        await m.answer("Kalit topilmadi. Gemini kaliti «AIza» yoki «AQ.» bilan boshlanadi. Qayta yuboring yoki bekor qiling.",
                       reply_markup=kb([("❌ Bekor", "k:menu")]))
        return
    parts = []
    for prov, keys in found.items():
        added, dup = db.add_keys(prov, keys)
        parts.append(f"{PROVIDER_TITLES[prov]}: +{added}" + (f" (takror: {dup})" if dup else ""))
    await state.clear()
    await m.answer("✅ Qo'shildi:\n" + "\n".join(parts) + "\n\n«🧪 Tekshirish» bilan ishlashini bilib oling.",
                   reply_markup=kb([("🧪 Tekshirish", "k:test"), ("🔑 Kalitlar", "k:menu")]))


@router.callback_query(lambda c: c.data == "k:list")
async def cb_key_list(c: CallbackQuery, bot: Bot, db: DB):
    rows = db.list_keys()
    await c.answer()
    if not rows:
        await bot.send_message(c.message.chat.id, "Kalit yo'q.", reply_markup=kb(BACK))
        return
    lines = ["🔑 <b>Kalitlar ro'yxati</b> (ID · holat)\n"]
    for r in rows:
        err = f" — {esc(r['last_error'][:40])}" if r["last_error"] else ""
        lines.append(f"{r['id']}. {key_status(r)} {PROVIDER_TITLES[r['provider']]} {mask(r['api_key'])} "
                     f"· ✓{r['ok_count']}{err}")
    await send_long(bot, c.message.chat.id, "\n".join(lines), kb([("🔑 Kalitlar", "k:menu")]))


async def test_keys(bot: Bot, chat_id: int, db: DB):
    rows = db.list_keys()
    ok = bad = 0
    for r in rows:
        try:
            await ping(r["provider"], r["api_key"], db.get(f"{r['provider']}_model"))
            db.mark_ok(r["id"])
            ok += 1
        except ApiError as e:
            apply_error(db, r["id"], e)
            bad += 1
        await asyncio.sleep(0.5)
    await bot.send_message(chat_id, f"🧪 Tekshiruv tugadi\n✅ Ishlaydi: {ok}\n❌ Xato/limit: {bad}\n\n"
                                    "Batafsil: «📋 Ro'yxat».", reply_markup=kb([("🔑 Kalitlar", "k:menu")]))


@router.callback_query(lambda c: c.data == "k:test")
async def cb_key_test(c: CallbackQuery, bot: Bot, db: DB):
    n = len(db.list_keys())
    if not n:
        await c.answer("Avval kalit qo'shing", show_alert=True)
        return
    await c.answer(f"🧪 {n} ta kalit tekshirilmoqda...")
    spawn(test_keys(bot, c.message.chat.id, db))


@router.callback_query(lambda c: c.data == "k:revive")
async def cb_key_revive(c: CallbackQuery, db: DB):
    n = db.revive_keys()
    await c.answer(f"♻️ {n} ta kalit qayta faollashtirildi", show_alert=True)


@router.callback_query(lambda c: c.data == "k:del")
async def cb_key_del(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.del_keys)
    await show(c, "O'chirish uchun:\n• ID raqamlarni yuboring (masalan: <code>3 5 7</code>)\n"
                  "• <code>yaroqsiz</code> — ❌ kalitlarni o'chiradi\n• <code>hammasi</code> — barchasini o'chiradi",
               kb([("❌ Bekor", "k:menu")]))


@router.message(Form.del_keys)
async def msg_key_del(m: Message, state: FSMContext, db: DB):
    t = (m.text or "").strip().lower()
    if t == "hammasi":
        n = db.delete_all_keys()
    elif t == "yaroqsiz":
        n = db.delete_dead()
    else:
        ids = [int(x) for x in re.findall(r"\d+", t)]
        if not ids:
            await m.answer("ID raqam yuboring yoki bekor qiling.", reply_markup=kb([("❌ Bekor", "k:menu")]))
            return
        n = db.delete_keys(ids)
    await state.clear()
    await m.answer(f"🗑 O'chirildi: {n}", reply_markup=kb([("🔑 Kalitlar", "k:menu")]))


# ====================== KANAL ======================
@router.callback_query(lambda c: c.data == "c:menu")
async def cb_channel(c: CallbackQuery, state: FSMContext, db: DB):
    await state.clear()
    ch = db.get("channel_id")
    cur = f"{esc(db.get('channel_title') or '')} <code>{esc(ch)}</code>" if ch else "❌ ulanmagan"
    await show(c, f"📣 <b>Kanal</b>\n\nHozirgi: {cur}",
               kb([("🔗 Ulash / almashtirish", "c:set")], [("🧪 Test xabar", "c:test")], BACK))


@router.callback_query(lambda c: c.data == "c:set")
async def cb_channel_set(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.channel)
    await show(c, "1) Botni kanalga <b>administrator</b> qiling (post yuborish huquqi bilan).\n"
                  "2) Kanal <code>@username</code>ni yuboring yoki kanaldan biror postni shu yerga <b>forward</b> qiling.",
               kb([("❌ Bekor", "c:menu")]))


@router.message(Form.channel)
async def msg_channel(m: Message, state: FSMContext, bot: Bot, db: DB):
    ref = None
    origin = getattr(m, "forward_origin", None)
    if origin is not None and getattr(origin, "chat", None) is not None:
        ref = origin.chat.id
    else:
        t = (m.text or "").strip()
        t = re.sub(r"^https?://t\.me/", "", t).strip("/")
        if t.lstrip("-").isdigit():
            ref = int(t)
        elif t:
            ref = t if t.startswith("@") else "@" + t
    if ref is None:
        await m.answer("Tushunmadim. @username yoki forward qilingan post yuboring.")
        return
    try:
        chat = await bot.get_chat(ref)
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramAPIError as e:
        await m.answer(f"❌ Kanal topilmadi yoki bot unda yo'q: {esc(str(e)[:150])}\n"
                       "Botni kanalga admin qilib qo'shing va qayta yuboring.",
                       reply_markup=kb([("❌ Bekor", "c:menu")]))
        return
    can_post = member.status == "creator" or (member.status == "administrator"
                                              and getattr(member, "can_post_messages", True) is not False)
    if not can_post:
        await m.answer("❌ Bot bu kanalda admin emas yoki post yuborish huquqi yo'q. Huquq bering va qayta yuboring.",
                       reply_markup=kb([("❌ Bekor", "c:menu")]))
        return
    db.set("channel_id", str(chat.id))
    db.set("channel_title", chat.title or str(chat.id))
    db.set("channel_username", getattr(chat, "username", None) or "")
    await state.clear()
    await m.answer(f"✅ Kanal ulandi: <b>{esc(chat.title or chat.id)}</b>", reply_markup=kb(BACK))


@router.callback_query(lambda c: c.data == "c:test")
async def cb_channel_test(c: CallbackQuery, bot: Bot, db: DB):
    ch = db.get("channel_id")
    if not ch:
        await c.answer("Kanal ulanmagan", show_alert=True)
        return
    try:
        await bot.send_message(int(ch) if ch.lstrip("-").isdigit() else ch, "✅ XABAR HUB ulandi.")
        await c.answer("Test xabar yuborildi")
    except TelegramAPIError as e:
        await c.answer(f"Xato: {str(e)[:150]}", show_alert=True)


# ====================== RSS MANBALAR ======================
@router.callback_query(lambda c: c.data == "f:menu")
async def cb_feeds(c: CallbackQuery, state: FSMContext, db: DB):
    await state.clear()
    feeds = db.list_feeds()
    rows = []
    for f in feeds:
        rows.append([(f"{'✅' if f['enabled'] else '⛔'} {domain_of(f['url'])[:28]}", f"f:tg:{f['id']}"),
                     ("🗑", f"f:rm:{f['id']}")])
    rows.append([("➕ Manba qo'shish", "f:add")])
    rows.append(BACK)
    await show(c, f"📡 <b>RSS manbalar</b> ({len(feeds)} ta)\nBosib yoqish/o'chirish; 🗑 — olib tashlash.", kb(*rows))


@router.callback_query(lambda c: c.data.startswith("f:tg:"))
async def cb_feed_toggle(c: CallbackQuery, state: FSMContext, db: DB):
    db.toggle_feed(int(c.data.split(":")[2]))
    await cb_feeds(c, state, db)


@router.callback_query(lambda c: c.data.startswith("f:rm:"))
async def cb_feed_remove(c: CallbackQuery, state: FSMContext, db: DB):
    db.remove_feed(int(c.data.split(":")[2]))
    await cb_feeds(c, state, db)


@router.callback_query(lambda c: c.data == "f:add")
async def cb_feed_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.feed)
    await show(c, "RSS havolasini yuboring (masalan: https://site.uz/rss).\nHavola tekshiriladi.",
               kb([("❌ Bekor", "f:menu")]))


@router.message(Form.feed)
async def msg_feed(m: Message, state: FSMContext, db: DB):
    url = (m.text or "").strip()
    if not url.startswith("http"):
        await m.answer("Havola http bilan boshlanishi kerak.")
        return
    ok, n, title = await validate_feed(url)
    if not ok:
        await m.answer(f"❌ RSS o'qilmadi ({esc(title)}). Boshqa havola yuboring.",
                       reply_markup=kb([("❌ Bekor", "f:menu")]))
        return
    lang = "uz" if domain_of(url).endswith(".uz") else "en"
    added = db.add_feed(url, lang)
    await state.clear()
    await m.answer(f"✅ {'Qo`shildi' if added else 'Allaqachon bor'}: {esc(title)} ({n} ta yangilik, til: {lang})",
                   reply_markup=kb([("📡 Manbalar", "f:menu")]))


# ====================== SOZLAMALAR ======================
def setting_value(db: DB, key: str) -> str:
    v = db.get(key)
    if key == "post_times":
        return v or "interval rejimi"
    return ("✅ yoqiq" if v == "1" else "⛔ o'chiq") if SETTING_META[key][1] == "bool" else v


@router.callback_query(lambda c: c.data == "s:menu")
async def cb_settings(c: CallbackQuery, state: FSMContext, db: DB):
    await state.clear()
    rows, lines = [], ["⚙️ <b>Sozlamalar</b>\n"]
    for key, (label, kind, *_r) in SETTING_META.items():
        lines.append(f"• {label}: <b>{esc(setting_value(db, key))}</b>")
        rows.append([(f"{label[:30]}: {setting_value(db, key)[:14]}", f"s:set:{key}")])
    rows.append(BACK)
    await show(c, "\n".join(lines), kb(*rows))


@router.callback_query(lambda c: c.data.startswith("s:set:"))
async def cb_setting(c: CallbackQuery, state: FSMContext, db: DB, sched: NewsScheduler):
    key = c.data.split(":", 2)[2]
    meta = SETTING_META.get(key)
    if not meta:
        await c.answer("Noma'lum sozlama")
        return
    if meta[1] == "bool":
        db.set(key, "0" if db.get(key) == "1" else "1")
        await cb_settings(c, state, db)
        return
    await state.set_state(Form.setting)
    await state.update_data(key=key)
    hint = {"int": f"Son yuboring ({meta[2]}–{meta[3]}).",
            "list": "Vergul bilan yuboring (tartib bo'yicha). Masalan: gemini,groq,openrouter "
                    "(rasm uchun: gemini,pollinations).",
            "str": "Yangi qiymatni yuboring.",
            "times": "Vaqtlarni HH:MM ko'rinishida vergul bilan yuboring (Toshkent vaqti). "
                     "Masalan: 09:00,13:00,18:30\nO'chirish (interval rejimiga qaytish): - yuboring."}[meta[1]]
    await show(c, f"{meta[0]}\nHozir: <code>{esc(db.get(key))}</code>\n\n{hint}", kb([("❌ Bekor", "s:menu")]))


@router.message(Form.setting)
async def msg_setting(m: Message, state: FSMContext, db: DB, sched: NewsScheduler):
    key = (await state.get_data()).get("key")
    meta = SETTING_META.get(key or "")
    val = (m.text or "").strip()
    if not meta:
        await state.clear()
        return
    if meta[1] == "int":
        if not val.isdigit() or not (meta[2] <= int(val) <= meta[3]):
            await m.answer(f"{meta[2]}–{meta[3]} oralig'ida son yuboring.")
            return
        val = str(int(val))
    elif meta[1] == "times":
        if val in ("-", "0", "off"):
            val = ""
        else:
            items = [x.strip() for x in val.replace(" ", ",").split(",") if x.strip()]
            if not items or any(not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", x) for x in items):
                await m.answer("Format: 09:00,13:00,18:30 (soat 0–23, daqiqa 00–59).")
                return
            val = ",".join(sorted({f"{int(x.split(':')[0]):02d}:{x.split(':')[1]}" for x in items}))
    elif meta[1] == "list":
        allowed = set(PROVIDERS) | {"pollinations"}
        items = [x.strip().lower() for x in val.split(",") if x.strip()]
        if not items or any(x not in allowed for x in items):
            await m.answer("Faqat: " + ", ".join(sorted(allowed)))
            return
        val = ",".join(items)
    elif not val or len(val) > 100 or " " in val:
        await m.answer("Qiymat bo'sh yoki noto'g'ri.")
        return
    db.set(key, val)
    if key in ("scan_interval", "post_times", "posts_per_time"):
        sched.reschedule()
    await state.clear()
    await m.answer(f"✅ Saqlandi: {esc(meta[0])} = <b>{esc(val or 'interval rejimi')}</b>", reply_markup=kb([("⚙️ Sozlamalar", "s:menu")]))


# ====================== STATISTIKA / POSTLAR / XATOLAR ======================
@router.callback_query(lambda c: c.data == "st:menu")
async def cb_stats(c: CallbackQuery, db: DB, newsroom: Newsroom, sched: NewsScheduler):
    s = db.stats()
    await show(c, "📊 <b>Statistika</b>\n\n"
                  f"Bugun: {s['today']}/{db.get_int('daily_limit')}\n"
                  f"Jami nashr: {s['published']}\n⏳ Tasdiq kutmoqda: {s['pending']}\n"
                  f"❌ Rad etilgan: {s['rejected']}\n⚠️ Xatolik: {s['failed']}\n"
                  f"Ko'rib chiqilgan yangiliklar: {s['seen']}\n\n"
                  f"Oxirgi qidiruv: {esc(newsroom.last_result or '—')}\nKeyingisi: {sched.next_run_text()}",
               kb([("🏆 Top postlar (7 kun)", "st:top:7"), ("🏆 30 kun", "st:top:30")],
                  [("🏆 Hammasi", "st:top:0"), ("🏆 Haftalik postni hozir qo'yish", "pn:best")], BACK))


@router.callback_query(lambda c: c.data.startswith("st:top:"))
async def cb_top(c: CallbackQuery, db: DB):
    try:
        days = int(c.data.split(":")[2])
    except ValueError:
        await c.answer()
        return
    top = db.ranked_posts(now_ts() - days * 86400 if days else 0, 10)
    label = f"oxirgi {days} kun" if days else "butun davr"
    if not top:
        await show(c, f"🏆 <b>Top postlar</b> ({label})\n\nHali reaksiya olgan post yo'q.", kb([("📊 Statistika", "st:menu")]))
        return
    uname = db.get("channel_username")
    lines = [f"🏆 <b>Top postlar</b> ({label}) — reaksiya soni bo'yicha\n",
             "<i>Eslatma: Bot API kanal ko'rishlarini bermaydi, shuning uchun reyting faqat tugmalar bosilishiga asoslangan.</i>\n"]
    for i, p in enumerate(top, 1):
        t = esc(p["title"][:90])
        url = post_url(uname, p["msg_id"])
        lines.append(f"{i}. {p['icon']} " + (f'<a href="{url}">{t}</a>' if url else t) + f" — <b>{p['n']}</b>")
    await show(c, "\n".join(lines), kb([("📊 Statistika", "st:menu")]))


@router.callback_query(lambda c: c.data == "p:menu")
async def cb_posts(c: CallbackQuery, db: DB):
    icons = {"published": "✅", "pending": "⏳", "failed": "⚠️", "rejected": "❌", "publishing": "📤"}
    rows = db.recent_posts(10)
    if not rows:
        await show(c, "Hali post yo'q.", kb(BACK))
        return
    lines = ["📋 <b>Oxirgi postlar</b>\n"]
    for r in rows:
        lines.append(f"{icons.get(r['status'], '•')} #{r['id']} {esc(r['headline'][:70])}")
    await show(c, "\n".join(lines), kb(BACK))


@router.callback_query(lambda c: c.data.startswith("p:ok:"))
async def cb_post_ok(c: CallbackQuery, bot: Bot, db: DB):
    ok, err = await publish(bot, db, int(c.data.split(":")[2]))
    await c.answer("✅ Nashr qilindi" if ok else f"⚠️ {err[:150]}", show_alert=not ok)
    try:
        await c.message.edit_caption(caption=("✅ NASHR QILINDI\n\n" if ok else "⚠️ XATO: " + esc(err[:100]) + "\n\n")
                                             + (c.message.caption or "")[:900], reply_markup=None)
    except TelegramAPIError:
        pass


@router.callback_query(lambda c: c.data.startswith("p:no:"))
async def cb_post_no(c: CallbackQuery, db: DB):
    reject(db, int(c.data.split(":")[2]))
    await c.answer("❌ Rad etildi")
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass


@router.callback_query(lambda c: c.data == "e:menu")
async def cb_errors(c: CallbackQuery, db: DB):
    rows = db.recent_logs(15)
    if not rows:
        await show(c, "🧾 Xatolar jurnali bo'sh.", kb(BACK))
        return
    lines = ["🧾 <b>Oxirgi xatolar</b>\n"]
    for r in rows:
        t = time.strftime("%d.%m %H:%M", time.localtime(r["ts"]))
        lines.append(f"{t} [{esc(r['source'])}] {esc(r['message'][:140])}")
    await show(c, "\n".join(lines), kb([("🧹 Tozalash", "e:clear")], BACK))


@router.callback_query(lambda c: c.data == "e:clear")
async def cb_errors_clear(c: CallbackQuery, db: DB):
    db.clear_logs()
    await show(c, "🧹 Jurnal tozalandi.", kb(BACK))

# ======================================================================
# ISHGA TUSHIRISH
# ======================================================================

async def start_health_server():
    """Render Web Service uchun (PORT bo'lsa) — UptimeRobot shu manzilni uyg'otib turadi."""
    port = os.getenv("PORT")
    if not port:
        return None

    async def ok(_):
        return web.Response(text="XABAR HUB ishlayapti")

    app = web.Application()
    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Health server: port %s", port)
    return runner


async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env faylga yoki hosting Environment'ga qo'ying.")
    db = DB()
    db.seed_from_env()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    newsroom = Newsroom(bot, db)
    sched = NewsScheduler(db, newsroom)
    content = ContentScheduler(bot, db, newsroom)
    dp = Dispatcher(storage=MemoryStorage(), db=db, newsroom=newsroom, sched=sched, content=content)
    dp.include_router(qrouter)  # quiz deep-link va tugmalari admin filtridan oldin
    dp.include_router(router)
    dp.include_router(public)
    runner = await start_health_server()
    sched.start()
    content.start()
    if not db.remote:
        log.info("Baza: %s (doimiy disk yoki Turso bo'lmasa, qayta o'rnatishda yo'qoladi)", os.path.abspath(DB_PATH))
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("XABAR HUB NEWSROOM ishga tushdi")
    try:
        await dp.start_polling(bot)
    finally:
        sched.shutdown()
        content.shutdown()
        if runner:
            await runner.cleanup()
        await bot.session.close()


# ======================================================================
# TARMOQSIZ SINOV:  python bot.py --selftest
# ======================================================================

def selftest_quiz(check):
    """Bosqich 2 (Quiz) tekshiruvlari: generatorlar, rasm, takrorlanmaslik, javob tekshirish."""
    import types as _t
    ns = _t.SimpleNamespace
    rng = random.Random(7)

    def verify_matrix(q):
        cells = q["spec"]["cells"]
        ans = q["opts"][q["correct_idx"]]
        for a in ("k", "c", "d"):
            g = [[cells[r * 3 + c][a] if cells[r * 3 + c] else None for c in range(3)] for r in range(3)]
            known = [(r, c) for r in range(3) for c in range(3) if (r, c) != (2, 2)]
            cands = set()
            if all(g[r][c] == g[r][0] for r, c in known if c != 0 or True if g[r][0] is not None):
                cands.add(g[2][0])
            if all(g[r][c] == g[0][c] for r, c in known):
                cands.add(g[0][2])
            if len({g[r][c] for r, c in known}) == 1:
                cands.add(g[0][0])
            S = set(g[0])
            rows_ok = set(g[1]) == S and len(S) == 3 and g[2][0] != g[2][1] and {g[2][0], g[2][1]} <= S
            cols_ok = {g[0][0], g[1][0], g[2][0]} == {g[0][1], g[1][1], g[2][1]} and len({g[0][0], g[1][0], g[2][0]}) == 3 \
                and len({g[0][2], g[1][2]}) == 2
            if rows_ok and cols_ok:
                m1, m2 = S - {g[2][0], g[2][1]}, {g[0][0], g[1][0], g[2][0]} - {g[0][2], g[1][2]}
                if m1 == m2 and len(m1) == 1:
                    cands.add(next(iter(m1)))
            if cands != {ans[a]}:
                return False
        return True

    def verify_trend(q):
        shown, ans = q["spec"]["items"], q["opts"][q["correct_idx"]]
        n = len(shown)
        for P in range(1, n):
            if all(shown[i] == shown[i % P] for i in range(n)):
                return ans == shown[n % P]
        return False

    def verify_eq(q):
        sp = q["spec"]
        sols = set()
        for a in range(2, 16):
            for b in range(2, 16):
                for c in range(2, 16):
                    v = (a, b, c)
                    if all(sum(cnt * v[i] for i, cnt in e["t"]) == e["v"] for e in sp["eqs"]):
                        sols.add(sum(cnt * v[i] for i, cnt in sp["final"]))
        used = {i for e in sp["eqs"] for i, _c in e["t"]} | {i for i, _c in sp["final"]}
        if len(used) < 3:  # ishlatilmagan belgi uchun qiymat farqi natijaga ta'sir qilmaydi
            pass
        return sols == {int(q["opts"][q["correct_idx"]]["t"])}

    for t in QUIZ_TYPES:
        for d in (1, 2, 3):
            made, bad = 0, []
            for _ in range(400):
                q = QUIZ_GENERATORS[t](rng, d)
                if not q:
                    continue
                made += 1
                n = len(q["opts"])
                ok = (2 <= n <= 6 and 0 <= q["correct_idx"] < n and q["explanation"] and q["hint"]
                      and len({json.dumps(o, sort_keys=True) for o in q["opts"]}) == n)
                if t == "odd":
                    ok = ok and n == 5 and _odd_unique(q["spec"]["items"], q["correct_idx"])
                elif t == "matrix":
                    ok = ok and n == 6 and verify_matrix(q)
                elif t == "trend":
                    ok = ok and n == 4 and verify_trend(q)
                elif t == "eq":
                    ok = ok and n == 4 and verify_eq(q)
                elif t == "seq":
                    ok = ok and n == 4 and q["opts"][q["correct_idx"]]["t"] not in [str(x) for x in q["spec"]["terms"]]
                else:
                    src_q = next(x for x in LOGIC_BANK if x[1] == q["spec"]["text"])
                    ok = ok and q["opts"][q["correct_idx"]]["t"] == src_q[2][0]
                if made <= 4:
                    png = render_quiz(q)
                    ok = ok and Image.open(io.BytesIO(png)).size == (1080, 1080)
                if not ok:
                    bad.append(q["fingerprint"])
                if made >= 40:
                    break
            check(f"quiz {t} d{d}: 40 savol, har birida yagona to'g'ri javob va 1080x1080 rasm", made >= 40 and not bad)
    check("mantiq banki: savol va variantlar rasmga sig'adi, javob yagona", all(
        len(q[1]) <= 170 and len(q[2]) == 4 and all(len(o) <= 38 for o in q[2]) and len(set(q[2])) == 4
        and q[0] in (1, 2, 3) for q in LOGIC_BANK) and len({q[1] for q in LOGIC_BANK}) == len(LOGIC_BANK))
    check("qiyinlik haftalik reja", [quiz_difficulty(w, rng) for w in range(6)] == [1, 1, 1, 2, 3, 3]
          and quiz_difficulty(6, rng) in (1, 2, 3))

    class QBot:
        def __init__(self):
            self.sent = []

        async def send_photo(self, **kw):
            self.sent.append(kw)
            return ns(message_id=900 + len(self.sent))

        async def me(self):
            return ns(username="xabar_bot")

    async def flow():
        qdb = DB(os.path.join(tempfile.mkdtemp(), "q.db"))
        qb = QBot()
        check("kanal ulanmasa quiz xato qaytaradi", (await run_quiz(qb, qdb))["status"] == "error")
        qdb.set("channel_id", "-100")
        res = [await run_quiz(qb, qdb) for _ in range(6)]
        check("6 ta quiz kanalga chiqdi", all(r["status"] == "ok" for r in res) and len(qb.sent) == 6)
        rows = qdb._q("SELECT q.qtype AS t FROM quiz_history h JOIN quiz_questions q ON q.id=h.question_id")
        check("turlar navbatlashadi (6 ta post — 6 xil tur)", len({r["t"] for r in rows}) == 6)
        kb0 = qb.sent[0]["reply_markup"].inline_keyboard
        check("post ostida «Javobni botda tekshiring» (deep link) va reaksiya tugmalari",
              "start=quiz_" in kb0[0][0].url and len(kb0) >= 2)
        check("kanal posti rasm (PNG) bilan", qb.sent[0]["photo"] is not None)
        qid = qdb._q("SELECT question_id FROM quiz_history ORDER BY id LIMIT 1")[0]["question_id"]
        fp = qdb.quiz_get(qid)["fingerprint"]
        check("30 kun ichida takrorlanmaydi", qdb.quiz_recent(fp, 30) and not qdb.quiz_recent("yo'q", 30))
        qdb._x("UPDATE quiz_history SET posted_at=?", (now_ts() - 31 * 86400,))
        check("31 kundan keyin yana ruxsat", not qdb.quiz_recent(fp, 30))
        for _ in range(25):
            await run_quiz(qb, qdb)
        ids = [r["question_id"] for r in qdb._q("SELECT question_id FROM quiz_history WHERE posted_at>=?", (now_ts() - 86400,))]
        check("30 kun ichida bitta savol ikki marta chiqmadi", len(ids) == len(set(ids)) and len(ids) >= 20)
        check("ball/reyting jadvali yo'q", not qdb._q("SELECT name FROM sqlite_master WHERE name LIKE '%score%'"))

        q = quiz_load(qdb.quiz_get(qid))
        wrong = (q["correct_idx"] + 1) % len(q["opts"])
        r1 = quiz_answer(qdb, qid, 1, q["correct_idx"])
        check("to'g'ri javob qabul qilindi, statistika hali to'planmoqda",
              r1["ok"] and r1["first"] and "to'planmoqda" in r1["stat"])
        r2 = quiz_answer(qdb, qid, 1, wrong)
        check("ikkinchi javob sanalmaydi (birinchisi saqlanadi)", r2["ok"] and not r2["first"])
        quiz_answer(qdb, qid, 2, q["correct_idx"])
        quiz_answer(qdb, qid, 3, q["correct_idx"])
        r4 = quiz_answer(qdb, qid, 4, wrong)
        check("«Bugun 75% to'g'ri topdi»", "Bugun 75%" in r4["stat"] and not r4["ok"])
        check("noto'g'ri javobda to'g'ri harf va tushuntirish ko'rsatiladi",
              QUIZ_LETTERS[q["correct_idx"]] in quiz_result_text(r4) and esc(q["explanation"]) in quiz_result_text(r4))
        check("noto'g'ri indeks rad etiladi", quiz_answer(qdb, qid, 9, 99) is None and quiz_answer(qdb, 99999, 9, 0) is None)
        kbd = quiz_answer_kb(q).inline_keyboard
        check("javob tugmalari + ishora tugmasi", sum(len(r) for r in kbd) == len(q["opts"]) + 1
              and kbd[-1][0].callback_data == f"qzh:{qid}"
              and not any(b.callback_data.startswith("qzh") for r in quiz_answer_kb(q, hint=False).inline_keyboard for b in r))
        check("reaksiya id'si modul posti uchun content_log dan",
              qdb._q("SELECT COUNT(*) AS n FROM content_log WHERE module='quiz' AND status='ok'")[0]["n"] >= 6)
    asyncio.run(flow())
    check("quiz moduli ro'yxatda va ijrochisi bor", "quiz" in MODULES and MODULE_RUNNERS.get("quiz") is run_quiz)


def selftest_video(check):
    """Bosqich 4 (Video) tekshiruvlari: filtrlar, AI darvozasi, takror, kalit sizib chiqmasligi."""
    import types as _t
    ns = _t.SimpleNamespace
    KEY = "AIzaSyTESTKEY_" + "x" * 25

    def item(vid, dur="PT1M30S", views=150000, likes=None, emb=True, priv="public", rating=None, blocked=None,
             allowed=None, live="none", ch="Kanal A", title="Qiziqarli video"):
        cd = {"duration": dur, "contentRating": rating or {}}
        if blocked or allowed:
            cd["regionRestriction"] = {"blocked": blocked} if blocked else {"allowed": allowed}
        return {"id": vid, "snippet": {"title": title, "channelTitle": ch, "channelId": ch, "description": "tavsif",
                                       "liveBroadcastContent": live},
                "contentDetails": cd, "statistics": {"viewCount": str(views), "likeCount": str(int(views * 0.03) if likes is None else likes)},
                "status": {"embeddable": emb, "privacyStatus": priv, "uploadStatus": "processed", "license": "youtube"}}

    check("davomiylik: PT1M30S, PT1H2M3S, PT45S, jonli efir",
          [parse_iso_duration(x) for x in ("PT1M30S", "PT1H2M3S", "PT45S", "P0D", "xato", "")] == [90, 3723, 45, 0, 0, 0])
    mv, ms = 20000, 300
    bad = {"embed": item("a", emb=False), "private": item("b", priv="private"), "yosh": item("c", rating={"ytRating": "ytAgeRestricted"}),
           "uz": item("d", blocked=["UZ"]), "allowed": item("e", allowed=["US"]), "uzun": item("f", dur="PT20M"),
           "qisqa": item("g", dur="PT5S"), "views": item("h", views=100), "live": item("i", live="live", dur="P0D"),
           "so'z": item("j", title="Mashhur XXX video")}
    check("filtrlar: embed/private/yosh/hudud/davomiylik/ko'rish/jonli/so'z rad etiladi",
          all(yt_check(v, mv, ms) for v in bad.values()) and yt_check(item("ok"), mv, ms) == ""
          and yt_check(item("z", allowed=["UZ", "US"]), mv, ms) == "")
    check("qoida bali: ko'proq ko'rilgan yuqori", yt_rule_score(item("x", views=10 ** 6)) > yt_rule_score(item("y", views=10 ** 4)))
    pg = {"useful": 0, "funny": 0}
    check("mavzu nisbati: tarix bo'sh -> foydali; kulgilar kam bo'lsa kulgili tanlanadi",
          video_preferred_group([]) == "useful" and video_preferred_group(["foydali"] * 5 + ["qiziqarli"] * 3 + ["trend"] * 2) == "funny"
          and video_preferred_group(["kulgili"] * 3 + ["foydali"] * 5 + ["qiziqarli"]) == "trend")
    good_ai = {"score": 85, "category": "foydali", "interesting": True, "useful": True, "funny": False, "harmful": False,
               "copyright_status": "safe", "uzbek_title": "Foydali maslahat", "short_description": "Qisqa foydali tavsif matni.",
               "rejection_reason": ""}
    check("AI hukmi: yaxshi o'tadi; zararli, past ball, xavfli mualliflik, bo'sh matn rad etiladi",
          video_verdict(good_ai, 70)["ok"]
          and not video_verdict({**good_ai, "harmful": True}, 70)["ok"]
          and not video_verdict({**good_ai, "harmful": "true"}, 70)["ok"]
          and not video_verdict({**good_ai, "score": 69}, 70)["ok"]
          and not video_verdict({**good_ai, "copyright_status": "risky"}, 70)["ok"]
          and not video_verdict({**good_ai, "uzbek_title": ""}, 70)["ok"]
          and video_verdict({**good_ai, "score": "88"}, 70)["score"] == 88
          and video_verdict({**good_ai, "category": "boshqa"}, 70)["category"] == "qiziqarli")
    check("sozlamalar menyusida video chegaralari bor", all(k in SETTING_META and k in DEFAULT_SETTINGS for k in
                                                           ("video_min_score", "video_min_views", "video_max_seconds")))

    class VBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kw):
            self.sent.append(kw)
            return ns(message_id=700 + len(self.sent))

        async def me(self):
            return ns(username="xabar_bot")

    g = globals()
    orig_fetch, orig_gen, orig_key = g["fetch_json"], g["generate_json"], os.environ.get("YOUTUBE_API_KEY")
    calls = {"fetch": [], "ai": 0}
    state = {"items": [], "ai": [good_ai], "boom": None}

    async def fake_fetch(url, params=None, **kw):
        calls["fetch"].append((url, dict(params or {})))
        if state["boom"]:
            raise state["boom"]
        if url.endswith("/videos") and params.get("chart"):
            return {"items": state["items"] if params["regionCode"] == "UZ" else []}
        if url.endswith("/search"):
            return {"items": []}
        return {"items": []}

    async def fake_gen(db, system, prompt):
        calls["ai"] += 1
        if "AIERR" in state["ai"]:
            raise AllProvidersFailed("limit")
        a = state["ai"][min(calls["ai"] - 1, len(state["ai"]) - 1)]
        return a

    async def flow():
        g["fetch_json"], g["generate_json"] = fake_fetch, fake_gen
        try:
            vdb = DB(os.path.join(tempfile.mkdtemp(), "v.db"))
            vb = VBot()
            os.environ.pop("YOUTUBE_API_KEY", None)
            check("kanal ulanmasa video xato", (await run_video(vb, vdb))["status"] == "error")
            vdb.set("channel_id", "-100")
            r = await run_video(vb, vdb)
            check("YOUTUBE_API_KEY yo'q bo'lsa tushunarli xato, so'rov yuborilmaydi",
                  r["status"] == "error" and "YOUTUBE_API_KEY" in r["text"] and not calls["fetch"])
            os.environ["YOUTUBE_API_KEY"] = KEY

            state["items"] = [item("v1", ch="A", views=900000), item("v2", ch="B", views=500000), item("v3", ch="C", views=300000),
                              item("v4", ch="D", views=200000), item("bad1", emb=False), item("bad2", dur="PT30M")]
            calls["ai"] = 0
            r = await run_video(vb, vdb)
            post = vb.sent[-1]
            check("video joylandi: sarlavha, manba (YouTube — kanal), havola, preview",
                  r["status"] == "ok" and "Foydali maslahat" in post["text"] and "Manba: YouTube — A" in post["text"]
                  and "https://www.youtube.com/watch?v=v1" in post["text"]
                  and post["link_preview_options"].url == "https://www.youtube.com/watch?v=v1"
                  and post["link_preview_options"].is_disabled is False)
            check("kalit so'rovga qo'shildi, post matnida kalit yo'q",
                  all(p.get("key") == KEY for _u, p in calls["fetch"]) and KEY not in post["text"])
            check("faqat 1 ta AI so'rovi (qoida bilan saralangan eng yaxshi nomzod)", calls["ai"] == 1)
            check("published_videos va content_log yozildi", vdb.video_exists("v1") and vdb.content_exists("video", "v1"))
            check("reaksiya tugmalari bor", post["reply_markup"] is not None)

            r = await run_video(vb, vdb)
            check("takror: v1 qayta joylanmadi (keyingisi tanlandi)",
                  r["status"] == "ok" and "watch?v=v2" in vb.sent[-1]["text"] and vdb.video_exists("v2"))

            vdb2 = DB(os.path.join(tempfile.mkdtemp(), "v2.db"))
            vdb2.set("channel_id", "-100")
            vb2 = VBot()
            calls["ai"] = 0
            state["ai"] = [{**good_ai, "harmful": True, "rejection_reason": "zo'ravonlik"}, {**good_ai, "score": 40},
                           {**good_ai, "copyright_status": "risky"}, {**good_ai, "uzbek_title": ""}]
            r = await run_video(vb2, vdb2)
            check("hammasi rad etilsa joylanmaydi, AI so'rovlari 4 tadan oshmaydi, rad etilganlar eslab qolinadi",
                  r["status"] == "skipped" and not vb2.sent and calls["ai"] == 4
                  and sum(vdb2.video_rejected(x) for x in ("v1", "v2", "v3", "v4")) == 4)
            state["ai"] = [good_ai]
            calls["ai"] = 0
            r = await run_video(vb2, vdb2)
            check("rad etilgan video qayta baholanmaydi (limit tejaladi)", calls["ai"] == 0 and r["status"] == "skipped")

            vdb3 = DB(os.path.join(tempfile.mkdtemp(), "v3.db"))
            vdb3.set("channel_id", "-100")
            vb3 = VBot()
            state["ai"] = ["AIERR"]
            r = await run_video(vb3, vdb3)
            check("AI ishlamasa video joylanmaydi (tekshirilmagan video chiqmaydi), nomzod rad etilmaydi",
                  r["status"] == "error" and not vb3.sent and not vdb3.video_rejected("v1"))

            state["ai"] = [good_ai]
            state["boom"] = RuntimeError(f"403 Forbidden for url https://googleapis.com/x?key={KEY}")
            r = await run_video(vb3, vdb3)
            check("YouTube xatosida kalit hech qayerga (xabar, jurnal) sizib chiqmaydi",
                  r["status"] == "error" and KEY not in r["text"]
                  and not any(KEY in (x["message"] or "") for x in vdb3._q("SELECT message FROM logs")))
            state["boom"] = None
            state["items"] = [item("w1", emb=False), item("w2", views=5)]
            r = await run_video(vb3, vdb3)
            check("mos video bo'lmasa tushunarli sabab bilan o'tkazib yuboriladi", r["status"] == "skipped"
                  and "embed taqiqlangan" in r["text"])
            check("video moduli ijrochisi ro'yxatda", MODULE_RUNNERS.get("video") is run_video)
        finally:
            g["fetch_json"], g["generate_json"] = orig_fetch, orig_gen
            if orig_key is None:
                os.environ.pop("YOUTUBE_API_KEY", None)
            else:
                os.environ["YOUTUBE_API_KEY"] = orig_key
    asyncio.run(flow())


def selftest():
    passed = 0

    def check(name, cond):
        nonlocal passed
        print(("PASS " if cond else "FAIL ") + name)
        assert cond, name
        passed += 1

    db = DB(os.environ["DB_PATH"])
    a, d = db.add_keys("gemini", [f"AIza{'x' * 30}{i:02d}" for i in range(50)])
    check("50 ta Gemini kalit qo'shildi", a == 50 and d == 0)
    check("takror kalit rad etildi", db.add_keys("gemini", [f"AIza{'x' * 30}00"]) == (0, 1))

    used = []

    async def fake_ok(k):
        used.append(k)
        return "ok"

    async def run():
        for _ in range(5):
            await run_with_pool(db, "gemini", fake_ok)
        check("kalitlar navbat bilan aylanadi", len(set(used)) == 5)

        calls = []

        async def quota_then_ok(k):
            calls.append(k)
            if len(calls) <= 3:
                raise ApiError(429, "quota PerMinute", "quota")
            return "done"
        r = await run_with_pool(db, "gemini", quota_then_ok)
        check("429 da keyingi kalitga o'tdi", r == "done" and len(calls) == 4)
        check("limitdagi kalitlar cooldownda", db.count_keys("gemini")["cooling"] == 3)

        async def bad_request(k):
            raise ApiError(400, "model yo'q", "request")
        try:
            await run_with_pool(db, "gemini", bad_request)
            check("request xatosi ko'tarildi", False)
        except ApiError:
            check("request xatosi kalitlarni yemaydi", db.count_keys("gemini")["dead"] == 0)

        db2 = DB(os.path.join(tempfile.mkdtemp(), "x.db"))
        db2.add_keys("groq", ["gsk_" + "a" * 30, "gsk_" + "b" * 30])

        async def auth_fail(k):
            raise ApiError(401, "invalid api key", "auth")
        for _ in range(2):
            try:
                await run_with_pool(db2, "groq", auth_fail)
            except AllKeysFailed:
                pass
            db2._x("UPDATE api_keys SET cooldown_until=0")  # 10 daqiqalik kutish o'tdi deb olamiz
        check("yaroqsiz kalit ikki xatodan keyin 'dead'", db2.count_keys("groq")["dead"] == 2)
        db2.revive_keys()
        check("revive ishladi", db2.count_keys("groq")["active"] == 2)

    asyncio.run(run())

    check("JSON fence bilan parse", parse_json('```json\n{"a":1}\n```') == {"a": 1})
    check("JSON matn ichida parse", parse_json('Mana: {"a":2} tamom') == {"a": 2})
    check("o'xshash sarlavha topildi", similar("Uzbekistan signs major gas deal with China today",
                                                "Uzbekistan signs major gas deal with China"))
    check("boshqa sarlavha o'xshash emas", not similar("Uzbekistan signs gas deal", "Messi scores twice in final"))
    check("bo'shliq regex to'g'ri", clean("a   b \n c") == "a b c")

    art = {"headline": "O'zbekiston <va> Xitoy & hamkorlik shartnomasi imzolandi", "category": "IQTISODIYOT",
           "body": ("Birinchi paragraf matni shu yerda. " * 12 + "\n\n" + "Ikkinchi paragraf. " * 25),
           "details": ["Shartnoma 5 yilga mo'ljallangan", "Hajmi 2 mlrd dollar"],
           "source_url": "https://example.com/a?x=1&y=2", "publisher": "BBC News",
           "confirmed": ["BBC News", "Reuters"], "ai_image": True}
    cap = build_caption(art)
    check("caption <=1024", len(cap) <= 1024)
    check("HTML escape ishladi", "&lt;va&gt;" in cap and "&amp;" in cap)
    check("imzo va manba bor", "XABAR HUB" in cap and "href=" in cap and "AI illyustratsiya" in cap)

    img = create_news_image("TEXNOLOGIYA",
                            "O'zbekistonda yangi sun'iy intellekt markazi ochildi va u mintaqa uchun muhim",
                            None, False)
    card = os.path.join(tempfile.gettempdir(), "test_card.jpg")
    with open(card, "wb") as f:
        f.write(img)
    check("rasm yaratildi (JPEG)", img[:2] == b"\xff\xd8" and len(img) > 10000)
    # --- Bosqich 0: modullar ---
    mdb = DB(os.path.join(tempfile.mkdtemp(), "m.db"))
    tables = {r["name"] for r in mdb._q("SELECT name FROM sqlite_master WHERE type='table'")}
    need = {"quiz_questions", "quiz_history", "published_videos", "reactions", "content_log", "daily_polls",
            "settings", "posts"}
    check("yangi jadvallar yaratildi", need <= tables)
    DB(os.path.join(tempfile.mkdtemp(), "m2.db"))  # migratsiya takror chaqirilsa ham xato bermaydi
    mdb._init()
    check("migratsiya takrorlanuvchan", True)
    check("mod_* standart sozlamalari", all(mdb.get(f"mod_{n}_on") == "0" for n in MODULES)
          and mdb.get("mod_quiz_times") == "18:00" and mdb.get("mod_digest_times") == "21:00")
    check("parse_times", parse_times("18:00, 9:05 25:00 x 18:00") == [(9, 5), (18, 0)])
    check("normalize_times", normalize_times("9:05,18:00") == "09:05,18:00" and normalize_times("-") == ""
          and normalize_times("25:00") is None and normalize_times("abc") is None)

    cs = ContentScheduler(None, mdb, None)
    check("yoqilmagan modul job yaratmaydi", cs.plan() == {})
    mdb.set("mod_quiz_on", "1")
    mdb.set("mod_morning_on", "1")
    mdb.set("mod_morning_times", "08:00,20:15")
    cs.sync()
    cs.sync()  # takror sync takroriy job yaratmasin
    ids = sorted(j.id for j in cs.sched.get_jobs())
    check("job id = mod_<nom>_<HHMM>, takrorsiz",
          ids == ["mod_morning_0800", "mod_morning_2015", "mod_quiz_1800"])
    mdb.set("mod_quiz_on", "0")
    mdb.set("mod_morning_times", "07:30")
    cs.sync()
    check("o'chirilgan/o'zgargan joblar yangilandi",
          sorted(j.id for j in cs.sched.get_jobs()) == ["mod_morning_0730"])

    async def modrun():
        r = await run_module("quiz", None, mdb, None)
        check("hali yozilmagan modul 'skipped' qaytaradi", r["status"] == "skipped")

        async def fake(bot, db):
            return {"status": "ok", "text": "ok", "ref": "r1", "message_id": 5}
        MODULE_RUNNERS["fact"] = fake
        r = await run_module("fact", None, mdb, None)
        check("ijrochi ishladi va content_log yozildi",
              r["status"] == "ok" and mdb.content_exists("fact", "r1"))

        async def boom(bot, db):
            raise RuntimeError("sinov")
        MODULE_RUNNERS["fact"] = boom
        r = await run_module("fact", None, mdb, None)
        check("xato botni yiqitmaydi va jurnalga yoziladi",
              r["status"] == "error" and any("fact" in x["message"] for x in mdb.recent_logs(5)))
        del MODULE_RUNNERS["fact"]
    asyncio.run(modrun())

    # --- Turso (soxta libsql bilan: tuple qatorlar, UNIQUE xatosi ValueError bo'lib keladi) ---
    import types as _t
    state = {"drops": 0}

    class FakeConn:
        def __init__(self, path):
            self.c = sqlite3.connect(path, check_same_thread=False)

        def execute(self, sql, args=()):
            if state["drops"]:
                state["drops"] -= 1
                raise RuntimeError("stream expired")
            try:
                return self.c.execute(sql, args)
            except sqlite3.IntegrityError as e:
                raise ValueError(f"SQLite failure: UNIQUE constraint failed: {e}")

        def commit(self):
            self.c.commit()

    fake_path = os.path.join(tempfile.mkdtemp(), "turso_fake.db")
    sys.modules["libsql"] = _t.SimpleNamespace(connect=lambda database, auth_token: FakeConn(fake_path))
    tdb = DB(os.path.join(tempfile.mkdtemp(), "unused.db"), turso=("libsql://x.turso.io", "tok"))
    check("Turso rejimi yoqildi", tdb.remote is not None)
    tdb.set("channel_id", "-100123")
    check("Turso: sozlama yozildi/o'qildi", tdb.get("channel_id") == "-100123" and tdb.get_int("daily_limit") == 10)
    a, d = tdb.add_keys("groq", ["gsk_" + "a" * 30, "gsk_" + "b" * 30])
    check("Turso: kalit qo'shildi", a == 2 and d == 0)
    check("Turso: takror kalit IntegrityError orqali rad etildi", tdb.add_keys("groq", ["gsk_" + "a" * 30]) == (0, 1))
    check("Turso: qator nom va indeks bilan ishlaydi",
          tdb._q("SELECT provider, api_key FROM api_keys LIMIT 1")[0]["provider"] == "groq"
          and tdb._q("SELECT provider FROM api_keys LIMIT 1")[0][0] == "groq")
    pid = tdb.add_post("fp1", "u", "h", "BOSHQA", "c", "", "pending", "")
    check("Turso: lastrowid/rowcount", pid >= 1 and tdb._x("DELETE FROM api_keys").rowcount == 2)
    state["drops"] = 1
    tdb.set("paused", "1")
    check("Turso: uzilishdan keyin qayta ulandi", tdb.get("paused") == "1")

    # --- Bosqich 1: reaksiyalar ---
    rdb = DB(os.path.join(tempfile.mkdtemp(), "r.db"))
    check("reaksiya birinchi marta sanaldi", rdb.add_reaction(7, 100, "👍") is True)
    check("takror bosish sanalmadi (boshqa emoji ham)",
          rdb.add_reaction(7, 100, "👍") is False and rdb.add_reaction(7, 100, "🔥") is False)
    rdb.add_reaction(7, 101, "👍")
    rdb.add_reaction(7, 102, "🔥")
    rdb.add_reaction(8, 100, "😮")
    check("sanoq to'g'ri va postlar ajratilgan",
          rdb.reaction_counts(7) == {"👍": 2, "🔥": 1} and rdb.reaction_counts(8) == {"😮": 1})
    rows = reaction_rows(rdb.reaction_counts(7), 7, share_link("https://t.me/kanal/5"))
    check("tugmalar: 👍 2, 🔥 1, 😮 (0 — raqamsiz), callback va ulashish",
          [b["text"] for b in rows[0]] == ["👍 2", "🔥 1", "😮"] and rows[0][0]["cb"] == "rx:7:up"
          and rows[1][0]["url"].startswith("https://t.me/share/url?url=https%3A%2F%2Ft.me%2Fkanal%2F5")
          and all(len(b["cb"].encode()) <= 64 for b in rows[0]))
    check("reaction_buttons=0 bo'lsa tugma yo'q", (rdb.set("reaction_buttons", "0"), reaction_keyboard(rdb, 7))[1] is None)

    # --- Bosqich 3: kontent modullari ---
    check("fmt_num / uz_date", fmt_num(12650.5) == "12 650,50" and fmt_num(1234567.891) == "1 234 567,89"
          and uz_date(datetime(2026, 11, 19), True) == "19-noyabr, payshanba")
    rates = parse_rates([{"Ccy": "USD", "Rate": "12650.50", "Diff": "12.5"}, {"Ccy": "EUR", "Rate": "14000", "Diff": "-30"},
                         {"Ccy": "JPY", "Rate": "80", "Diff": "0"}, {"Ccy": "RUB", "Rate": "bad"}])
    check("parse_rates: faqat USD/EUR, buzuq qator tashlandi", [r[0] for r in rates] == ["USD", "EUR"])
    wx = {"current": {"temperature_2m": 4.6, "weather_code": 3, "wind_speed_10m": 10.8},
          "daily": {"temperature_2m_min": [1.2], "temperature_2m_max": [9.4], "precipitation_probability_max": [20]}}
    txt = build_morning(datetime(2026, 11, 19, 8), wx, rates)
    check("ertalabki post: ob-havo + valyuta", "Toshkent" in txt and "+5°" in txt and "20%" in txt and "3 m/s" in txt
          and "▲ +12,50" in txt and "▼ -30,00" in txt and "XABAR HUB" in txt)
    check("faqat valyuta bilan ham tuziladi", "Toshkent" not in build_morning(datetime(2026, 11, 19), None, rates))
    check("weather_desc", weather_desc(0).startswith("☀️") and weather_desc(95).startswith("⛈"))

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kw):
            self.sent.append(("msg", kw))
            return types_ns(message_id=100 + len(self.sent))

        async def send_poll(self, **kw):
            self.sent.append(("poll", kw))
            return types_ns(message_id=200 + len(self.sent))

        async def me(self):
            return types_ns(username="xabar_bot")

    types_ns = _t.SimpleNamespace
    sdb = DB(os.path.join(tempfile.mkdtemp(), "s.db"))
    fb = FakeBot()
    orig_fetch, orig_gen = globals()["fetch_json"], globals()["generate_json"]

    async def fake_fetch(url, params=None, **kw):
        if "open-meteo" in url:
            return wx
        if "cbu.uz" in url:
            return [{"Ccy": "USD", "Rate": "12650.5", "Diff": "1"}]
        if "onthisday" in url:
            return {"events": [{"year": 1863, "text": "Abraham Lincoln delivered the Gettysburg Address in Pennsylvania.",
                                "pages": [{"content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/X"}}}]}]}
        if "featured" in url:
            return {"tfa": {"titles": {"normalized": "Test maqola"}, "type": "standard", "extract": "x" * 200,
                            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/T"}}}}
        raise RuntimeError("tarmoq yo'q")

    async def fake_gen(db, system, prompt):
        return {"text": "Bu o'zbekcha sinov matni bo'lib, yetarlicha uzun jumladan iborat."}

    globals()["fetch_json"], globals()["generate_json"] = fake_fetch, fake_gen

    async def stage3():
        r = await run_morning(fb, sdb)
        check("kanal ulanmagan bo'lsa xato (modul yiqilmaydi)", r["status"] == "error" and not fb.sent)
        sdb.set("channel_id", "-100777")
        r = await run_morning(fb, sdb)
        kw = fb.sent[-1][1]
        check("ertalabki post yuborildi, reaksiya tugmasi bor, content_log yozildi",
              r["status"] == "ok" and "Xayrli tong" in kw["text"] and kw["reply_markup"] is not None
              and sdb.content_exists("morning", tz_now().date().isoformat()))
        r = await run_morning(fb, sdb)
        check("ertalabki post takror qo'yilmadi", r["status"] == "skipped")
        r = await run_history(fb, sdb)
        check("bugun tarixda yuborildi (AI orqali)", r["status"] == "ok" and "1863-yil" in fb.sent[-1][1]["text"])
        r = await run_history(fb, sdb)
        check("tarix: xuddi shu voqea takror chiqmadi", r["status"] == "skipped")
        r = await run_fact(fb, sdb)
        check("kun fakti yuborildi", r["status"] == "ok" and "Kun fakti" in fb.sent[-1][1]["text"])
        r = await run_fact(fb, sdb)
        check("fakt takrori rad etildi", r["status"] == "error")
        n0 = len(fb.sent)
        r = await run_poll(fb, sdb)
        check("poll yuborildi (4 variant, anonim)", r["status"] == "ok" and fb.sent[n0][0] == "poll"
              and len(fb.sent[n0][1]["options"]) == 4)
        r = await run_digest(fb, sdb)
        check("bugun nashr yo'q bo'lsa xulosa o'tkazib yuboriladi", r["status"] == "skipped")
        for i in range(7):
            pid = sdb.add_post(f"f{i}", "u", f"Sarlavha {i}", "DUNYO", "c", "", "published", "")
            sdb.set_post_status(pid, "published", msg_id=50 + i)
        sdb.add_reaction(3, 1, "🔥")  # 3-post eng ko'p baho olgan bo'lsin
        sdb.set("channel_username", "kanal")
        r = await run_digest(fb, sdb)
        body = fb.sent[-1][1]["text"]
        check("xulosa: 5 ta yangilik, eng ko'p reaksiyali birinchi, havola bilan",
              r["status"] == "ok" and body.count("\n") >= 6 and "1. <a href=\"https://t.me/kanal/52\">Sarlavha 2" in body
              and "6." not in body)
    asyncio.run(stage3())

    # --- Bosqich 5: reyting va haftalik eng yaxshi post ---
    async def stage5():
        check("reaksiyasiz bo'lsa reyting bo'sh", DB(os.path.join(tempfile.mkdtemp(), "e.db")).ranked_posts(0, 5) == [])
        morning_cid = sdb._q("SELECT id FROM content_log WHERE module='morning' AND status='ok'")[0]["id"]
        for uid in (11, 12, 13):
            sdb.add_reaction(-morning_cid, uid, "👍")  # modul posti 3 ta reaksiya
        sdb.add_reaction(3, 2, "🔥")                    # 3-yangilik posti jami 2 ta
        top = sdb.ranked_posts(0, 5)
        check("reyting: modul posti (3) > yangilik (2), reaksiyasizlar kirmaydi",
              [t["n"] for t in top] == [3, 2] and top[0]["pid"] == -morning_cid and top[1]["pid"] == 3)
        check("reyting vaqt oynasi (kelajakdan beri — bo'sh)", sdb.ranked_posts(now_ts() + 100, 5) == [])
        n0 = len(fb.sent)
        r = await run_best(fb, sdb)
        body = fb.sent[-1][1]["text"]
        check("haftaning eng yaxshi posti yuborildi (reaksiya hisobi bilan)",
              r["status"] == "ok" and len(fb.sent) == n0 + 1 and "jami 3 reaksiya" in body and "👍 3" in body)
        r = await run_best(fb, sdb)
        check("shu hafta ikkinchi marta qo'yilmaydi", r["status"] == "skipped")
        edb = DB(os.path.join(tempfile.mkdtemp(), "e2.db"))
        edb.set("channel_id", "-1")
        check("reaksiya bo'lmasa o'tkazib yuboriladi", (await run_best(fb, edb))["status"] == "skipped")
    asyncio.run(stage5())
    check("best moduli: yakshanba, standart sozlamalar", MODULE_DAYS["best"] == {6} and sdb.get("mod_best_times") == "20:00")
    globals()["fetch_json"], globals()["generate_json"] = orig_fetch, orig_gen
    check("poll banki: barcha savollar 2–10 variantli", all(2 <= len(o) <= 10 and len(q) <= 300 and len(set(o)) == len(o)
                                                           for q, o in POLL_BANK))
    check("pick_poll eng kam ishlatilganini tanlaydi",
          pick_poll({i: 1 for i in range(len(POLL_BANK)) if i != 4}) == 4)
    check("poll haftada 4 kun", len(MODULE_DAYS["poll"]) == 4)
    check("tech: feed_filter bor", "feed_filter" in collect.__code__.co_varnames)

    selftest_quiz(check)
    selftest_video(check)
    print("HAMMASI:", passed, "tekshiruv o'tdi")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        asyncio.run(main())
