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
                           Message)
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
    "use_source_image": "0",
    "text_providers": "gemini,groq,openrouter",
    "image_providers": "gemini,pollinations",
    "gemini_model": "gemini-3.8-flash",
    "gemini_image_model": "gemini-3.1-flash-image",
    "groq_model": "llama-3.3-70b-versatile",
    "openrouter_model": "meta-llama/llama-3.3-70b-instruct:free",
    "gemini_image_off_until": "0",
}

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
    "text_providers": ("Matn AI tartibi", "list", 0, 0),
    "image_providers": ("Rasm AI tartibi", "list", 0, 0),
    "gemini_model": ("Gemini matn modeli", "str", 0, 0),
    "gemini_image_model": ("Gemini rasm modeli", "str", 0, 0),
    "groq_model": ("Groq modeli", "str", 0, 0),
    "openrouter_model": ("OpenRouter modeli", "str", 0, 0),
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


class DB:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        os.makedirs(IMAGE_DIR, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._init()

    # ---------- past daraja ----------
    def _x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _q(self, sql, args=()):
        with self.lock:
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


async def collect(db: DB, client: httpx.AsyncClient, limit: int = 80) -> list[dict]:
    feeds = db.list_feeds(enabled_only=True)
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
            msg = await bot.send_photo(chat_id=_chat(db), photo=BufferedInputFile(data, "xabar-hub.jpg"),
                                       caption=p["caption"], parse_mode="HTML")
            db.set_post_status(post_id, "published", msg_id=msg.message_id)
            _drop_image(p["image_path"])
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

    async def scan(self, manual: bool = False, slot: bool = False) -> dict:
        if self.lock.locked():
            return {"error": "Qidiruv allaqachon ketmoqda, kuting."}
        async with self.lock:
            try:
                res = await self._scan(manual, slot)
            except Exception as e:
                log.exception("scan failed")
                self.db.log("newsroom", f"scan xatosi: {e.__class__.__name__}: {e}")
                res = {"error": f"Kutilmagan xato: {e.__class__.__name__}"}
            self.last_result = f"{tz_now().strftime('%H:%M')} — {res}"
            return res

    async def _scan(self, manual: bool, slot: bool = False) -> dict:
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
            cands = await collect(db, client)
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
        f"Keyingi qidiruv: {sched.next_run_text()}"
    )


def main_kb(db: DB):
    paused = db.get("paused") == "1"
    return kb(
        [("🚀 Hozir post qo'yish", "post:now")],
        [("📰 Hozir qidirish", "scan:now"), ("📊 Statistika", "st:menu")],
        [("🔑 API kalitlar", "k:menu"), ("📣 Kanal", "c:menu")],
        [("📡 Manbalar (RSS)", "f:menu"), ("⚙️ Sozlamalar", "s:menu")],
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


async def do_post_now(bot: Bot, chat_id: int, newsroom: Newsroom):
    res = await newsroom.scan(manual=True, slot=True)
    await bot.send_message(chat_id, format_scan(res), reply_markup=kb(BACK))


@router.callback_query(lambda c: c.data == "post:now")
async def cb_post_now(c: CallbackQuery, bot: Bot, newsroom: Newsroom):
    await c.answer("🚀 Post tayyorlanmoqda, bir necha daqiqa ketishi mumkin")
    spawn(do_post_now(bot, c.message.chat.id, newsroom))


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
               kb(BACK))


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
    dp = Dispatcher(storage=MemoryStorage(), db=db, newsroom=newsroom, sched=sched)
    dp.include_router(router)
    dp.include_router(public)
    runner = await start_health_server()
    sched.start()
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("XABAR HUB NEWSROOM ishga tushdi")
    try:
        await dp.start_polling(bot)
    finally:
        sched.shutdown()
        if runner:
            await runner.cleanup()
        await bot.session.close()


# ======================================================================
# TARMOQSIZ SINOV:  python bot.py --selftest
# ======================================================================

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
    print("HAMMASI:", passed, "tekshiruv o'tdi")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        asyncio.run(main())
