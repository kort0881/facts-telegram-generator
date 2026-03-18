#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Facts Autopost (KIBER-style)

Берёт статьи из facts_links_clean.txt, выжимает один яркий научный/исторический факт
и постит в Telegram. Использует Groq с бюджетом и state-памятью по аналогии с No-code-protection.
"""

import os
import json
import asyncio
import random
import re
import time
import logging
from dataclasses import dataclass
from typing import Optional, List, Set, Tuple
from urllib.parse import urlparse, urljoin
from collections import Counter

import aiohttp
from bs4 import BeautifulSoup

# ИСПРАВЛЕНИЕ #1: используем AsyncGroq вместо Groq (синхронный клиент нельзя await-ить)
from groq import AsyncGroq

# ============ ЛОГИРОВАНИЕ ============

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("FactsBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ============ ENV ============

def get_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        logger.error(f"Missing env: {name}")
        raise SystemExit(1)
    return val

GROQ_API_KEY       = get_env("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")
CHANNEL_ID         = get_env("CHANNEL_ID")

CACHE_DIR = os.getenv("CACHE_DIR", "cache_facts")
os.makedirs(CACHE_DIR, exist_ok=True)

STATE_FILE       = os.path.join(CACHE_DIR, "facts_state.json")
GROQ_BUDGET_FILE = os.path.join(CACHE_DIR, "facts_groq_budget.json")

FACTS_LINKS_FILE = "facts_links_clean.txt"

HTTP_TIMEOUT            = aiohttp.ClientTimeout(total=25)
TEXT_ONLY_THRESHOLD     = 700
MAX_POSTS_PER_RUN       = 1
MAX_ATTEMPTS            = 15

RECENT_POSTS_CHECK          = 10
RECENT_SIMILARITY_THRESHOLD = 0.40
MIN_TOPIC_DIVERSITY         = 3

MAX_ARTICLE_CHARS = 2500
MIN_ARTICLE_CHARS = 300

MAX_POST_LEN = 900
MIN_POST_LEN = 350

# ============ МОДЕЛИ / БЮДЖЕТ ============

@dataclass
class ModelConfig:
    name: str
    rpm: int
    tpm: int
    daily_tokens: int
    priority: int

MODELS = {
    "heavy": ModelConfig("llama-3.3-70b-versatile", rpm=30, tpm=6000,  daily_tokens=100000, priority=1),
    "light": ModelConfig("llama-3.1-8b-instant",    rpm=30, tpm=20000, daily_tokens=500000, priority=2),
}

class GroqBudget:
    def __init__(self, path: str):
        self.state_file = path
        self.data = self._load()

    def _load(self) -> dict:
        default = {
            "daily_tokens":      {},
            "last_reset":        time.strftime("%Y-%m-%d"),
            "last_request_time": {},
            "request_count":     {},
            "minute_start":      {},
        }
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    if saved.get("last_reset") != time.strftime("%Y-%m-%d"):
                        logger.info("🔄 New day — reset Groq limits")
                        saved["daily_tokens"] = {}
                        saved["last_reset"]   = time.strftime("%Y-%m-%d")
                    default.update(saved)
            except Exception:
                pass
        return default

    def save(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
        except Exception:
            pass

    def add_tokens(self, model: str, tokens: int):
        self.data["daily_tokens"][model] = self.data["daily_tokens"].get(model, 0) + tokens
        self.save()

    def can_use_model(self, model_key: str) -> bool:
        if model_key not in MODELS:
            return False
        cfg  = MODELS[model_key]
        used = self.data["daily_tokens"].get(cfg.name, 0)
        return (cfg.daily_tokens - used) > (cfg.daily_tokens * 0.05)

    async def wait_for_rate_limit(self, model_key: str):
        cfg   = MODELS[model_key]
        model = cfg.name
        now   = time.time()

        if now - self.data["minute_start"].get(model, 0) > 60:
            self.data["minute_start"][model]   = now
            self.data["request_count"][model]  = 0

        if self.data["request_count"].get(model, 0) >= cfg.rpm - 2:
            wait = 60 - (now - self.data["minute_start"][model]) + 1
            logger.info(f"⏳ RPM limit ({model_key}). Waiting {wait:.1f}s")
            await asyncio.sleep(wait)
            self.data["minute_start"][model]  = time.time()
            self.data["request_count"][model] = 0

        last = self.data["last_request_time"].get(model, 0)
        if now - last < 2:
            await asyncio.sleep(2)

        self.data["request_count"][model]     = self.data["request_count"].get(model, 0) + 1
        self.data["last_request_time"][model] = time.time()
        self.save()

budget      = GroqBudget(GROQ_BUDGET_FILE)
# ИСПРАВЛЕНИЕ #1: AsyncGroq — асинхронный клиент, корректно работает с await
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# ============ STATE ============

@dataclass
class FactItem:
    url:   str
    title: str
    text:  str
    uid:   str

class FactsState:
    def __init__(self, path: str):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "posted_ids": [],
            "posts":      [],
            "topics":     [],
        }

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
        except Exception:
            pass

    def is_posted(self, uid: str) -> bool:
        return uid in self.data["posted_ids"]

    def mark_posted(self, uid: str, title: str, text: str, topic: str):
        self.data["posted_ids"].append(uid)
        self.data["posted_ids"] = self.data["posted_ids"][-500:]

        self.data["posts"].append({"title": title, "text": text, "topic": topic})
        self.data["posts"] = self.data["posts"][-500:]

        self.data["topics"].append(topic)
        self.data["topics"] = self.data["topics"][-100:]

        self.save()

    def _similarity(self, a: str, b: str) -> float:
        return combined_similarity(a, b)

    def is_duplicate(self, title: str, text: str) -> bool:
        for p in self.data["posts"]:
            if p["title"] == title:
                return True
        return False

    def is_too_similar_to_recent(self, title: str, text: str) -> bool:
        recent = self.data["posts"][-RECENT_POSTS_CHECK:]
        for p in recent:
            sim = self._similarity(text, p["text"])
            if sim >= RECENT_SIMILARITY_THRESHOLD:
                logger.info(f"🔁 Similar to recent post: sim={sim:.2f}")
                return True
        return False

    def needs_diversity(self) -> Optional[str]:
        recent = self.data["topics"][-RECENT_POSTS_CHECK:]
        if not recent:
            return None
        counts = Counter(recent)
        if len(counts) < MIN_TOPIC_DIVERSITY:
            topic, count = counts.most_common(1)[0]
            logger.info(f"⚠️ Dominant topic '{topic}' in recent posts ({count})")
            return topic
        return None

    def get_recent_topics_stats(self) -> dict:
        return dict(Counter(self.data["topics"][-20:]))

state = FactsState(STATE_FILE)

# ============ NLP / ТОПИКИ / СХОЖЕСТЬ ============

STOP_WORDS = {
    "что","это","как","для","или","при","так","все","они","был","она","его","её","он",
    "мы","вы","но","да","нет","уже","ещё","даже","если","тоже","есть","очень","ведь",
    "себя","свой","своя","своё","свои","тот","эта","этот","не","ни","же","бы","по","до",
    "из","без","над","под","через","между","после","перед","среди","когда","который",
    "которая","которое","которые","можно","нужно","должен","может","будет","были",
    "быть","стало","стал","одно","один","одна","раз","два","три","лет","год",
}

def normalize_text(s: str) -> Set[str]:
    s = s.lower()
    s = re.sub(r"[^a-zа-я0-9ё]+", " ", s)
    words = [w for w in s.split() if len(w) > 3 and w not in STOP_WORDS]
    return set(words)

def get_bigrams(s: str) -> Set[Tuple[str, str]]:
    words = sorted(normalize_text(s))
    if len(words) < 2:
        return set()
    return {(words[i], words[i+1]) for i in range(len(words)-1)}

def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0

def jaccard_similarity(a: str, b: str) -> float:
    return jaccard(normalize_text(a), normalize_text(b))

def bigram_jaccard(a: str, b: str) -> float:
    ba = get_bigrams(a)
    bb = get_bigrams(b)
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union else 0.0

def combined_similarity(a: str, b: str) -> float:
    return 0.6 * jaccard_similarity(a, b) + 0.4 * bigram_jaccard(a, b)

def extract_topic(text: str) -> str:
    t = text.lower()
    # ИСПРАВЛЕНИЕ #3: "привычк" убрано из блока brain — иначе тема habits никогда не присваивалась
    if any(w in t for w in ("мозг", "нейрон", "память", "сон")):
        return "brain"
    if any(w in t for w in ("привычк", "мотиваци", "продуктивност")):
        return "habits"
    if any(w in t for w in ("планет", "космос", "звезд", "галактик", "венер", "марс")):
        return "space"
    if any(w in t for w in ("рим", "египет", "фараон", "импер", "цар", "археолог")):
        return "history"
    if any(w in t for w in ("диабет", "сердц", "здоров", "диета", "ожирен")):
        return "health"
    return "other"

# ============ BANNED / КАЧЕСТВО ============

BANNED_PHRASES = [
    "проливает новый свет",
    "мы можем глубже понять эпоху",
    "это открывает дискуссию",
    "позволяет лучше понять наши корни",
    "это важно для нашего благополучия",
    "новые данные показывают",
    "это поднимает важные вопросы",
    "в наши дни это особенно актуально",
    "в современном мире",
    "как показывают исследования",
    "это меняет картину",
    "по-честному, это меняет картинку",
    "это важно для нас всех",
    "это заставляет задуматься о многом",
    "это открывает новые горизонты",
    "сложно переоценить важность",
    "нельзя недооценивать",
    "это касается каждого из нас",
    "мы все сталкиваемся с этим",
    "меняет наше понимание",
    "загугли",
    "если хочешь копнуть глубже",
    "если хочешь узнать больше",
    "узнай больше в интернете",
    "копнуть глубже",
    "начни интересоваться новостями",
    "обращай внимание на открытия последних лет",
    "следи за новостями",
    "будь в курсе последних открытий",
    "подписывайся",
    "поделись с друзьями",
    "расскажи друзьям",
    "публикует исследования",
    "освещает последние открытия",
    "независимый журнал",
    "делает науку доступной",
    "теперь ты можешь",
    "теперь любой может",
    "был ли у тебя опыт",
]

BANNED_TOPICS = [
    "международный суд", "международного суда", "международный уголовный суд",
    "геноцид", "этническая чистка", "военное преступление", "военные преступления",
    "санкции", "резолюция", "оон", "united nations", "icc", "icj",
    "палестин", "израиль", "сектор газа", "газе", "холокост",
    "этнический конфликт", "референдум", "выборы", "парламент", "президент",
    "верховный суд", "supreme court", "human rights watch", "amnesty international",
]

TITLE_BAN = re.compile(r"^(мозг вскипает|мозг взрывается|что ты не знал)", re.IGNORECASE)

def contains_banned_phrases(text: str) -> bool:
    lower = text.lower()
    for p in BANNED_PHRASES:
        if p in lower:
            logger.info(f"🚫 Banned phrase: {p}")
            return True
    return False

def has_banned_title(text: str) -> bool:
    first = text.strip().split("\n", 1)[0].lower()
    if TITLE_BAN.search(first):
        logger.info(f"🚫 Banned title: {first}")
        return True
    return False

def is_banned_topic(text: str) -> bool:
    lower = text.lower()
    for w in BANNED_TOPICS:
        if w in lower:
            logger.info(f"🚫 Banned topic keyword: {w}")
            return True
    return False

def has_strong_fact(text: str) -> bool:
    t = text.lower()
    has_year        = bool(re.search(r"\b(19\d{2}|20\d{2})\b", t))
    has_number      = bool(re.search(r"\b\d+([.,]\d+)?\b", t))
    has_result_verb = bool(re.search(
        r"\b(показал[аи]?|выяснил[аи]?|обнаружил[аи]?|нашл[ие]|измерил[аи]?|"
        r"увеличил[аи]?|снизил[аи]?|зафиксировал[аи]?|доказал[аи]?|установил[аи]?|"
        r"выявил[аи]?|подтвердил[аи]?|определил[аи]?)\b",
        t
    ))
    if not (has_year and has_number and has_result_verb):
        logger.info(f"Strong fact fail: year={has_year}, num={has_number}, verb={has_result_verb}")
    return has_year and has_number and has_result_verb

def looks_like_announcement(text: str) -> bool:
    if len(text) < MIN_POST_LEN:
        return True
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) < 5:
        return True
    return False

def normalize_blank_lines(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [l.rstrip() for l in text.split("\n")]
    return "\n".join(lines)

def smart_clip(post: str, limit: int = MAX_POST_LEN, min_cut: int = 400) -> str:
    if len(post) <= limit:
        return post
    raw      = post[:limit]
    last_dot = raw.rfind(".")
    last_q   = raw.rfind("?")
    last_exc = raw.rfind("!")
    cut = max(last_dot, last_q, last_exc)
    if cut != -1 and cut >= min_cut:
        logger.info(f"✂️ Smart clip at {cut}")
        return raw[:cut+1].rstrip()
    logger.info(f"✂️ Hard clip at {limit}")
    return raw.rstrip()

# ============ ЗАГРУЗКА ЛИНКОВ / СТАТЕЙ ============

def load_links() -> List[str]:
    if not os.path.exists(FACTS_LINKS_FILE):
        logger.error(f"No links file: {FACTS_LINKS_FILE}")
        return []
    out = []
    with open(FACTS_LINKS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    logger.info(f"📚 Loaded {len(out)} source links")
    return out

BANNED_DOMAINS = [
    "icj-cij.org", "icc-cpi.int", "un.org", "hrw.org", "amnesty.org",
    "theguardian.com/world", "nytimes.com/section/opinion", "vox.com", "lawfareblog.com",
    "justsecurity.org", "brookings.edu", "brennancenter.org",
]

def is_root(url: str) -> bool:
    p = urlparse(url)
    return p.path in ("", "/") and not p.query and not p.fragment

async def http_get(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
            if resp.status >= 400:
                logger.info(f"HTTP {resp.status} for {url}")
                return None
            return await resp.text()
    except Exception as e:
        logger.info(f"HTTP error {url}: {e}")
        return None

def extract_article_text(html: str) -> str:
    soup       = BeautifulSoup(html, "html.parser")
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    text       = "\n".join(paragraphs)
    return text[:MAX_ARTICLE_CHARS]

def extract_article_links(base_url: str, html: str) -> List[str]:
    soup  = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            href = urljoin(base_url, href)
        if any(x in href for x in ["/article", "/news", "/story", "/202", "/post", "/science", "/history", "/space"]):
            links.append(href)
    seen = set()
    out  = []
    for l in links:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out[:20]

async def fetch_article_from_source(session: aiohttp.ClientSession, url: str) -> Optional[FactItem]:
    if any(bad in url for bad in BANNED_DOMAINS):
        logger.info(f"Skip banned domain: {url}")
        return None

    logger.info(f"🌐 Fetch from {url}")
    html = await http_get(session, url)
    if not html:
        return None

    if is_root(url):
        links = extract_article_links(url, html)
        if not links:
            logger.info(f"No article links on {url}")
            return None
        random.shuffle(links)
        for art in links:
            uid = f"fact_{hash(art) & 0xffffffff:x}"
            # ИСПРАВЛЕНИЕ #4: пропускаем уже обработанные статьи ещё до HTTP-запроса
            if state.is_posted(uid):
                continue
            html2 = await http_get(session, art)
            if not html2:
                continue
            text = extract_article_text(html2)
            if len(text) < MIN_ARTICLE_CHARS:
                continue
            return FactItem(url=art, title=art, text=text, uid=uid)
        return None
    else:
        text = extract_article_text(html)
        if len(text) < MIN_ARTICLE_CHARS:
            logger.info(f"Too short article: {url}")
            return None
        uid = f"fact_{hash(url) & 0xffffffff:x}"
        return FactItem(url=url, title=url, text=text, uid=uid)

# ============ PROMPT / GROQ ============

FACT_PROMPT = """
Ты пишешь пост для Telegram-канала «Что ты не знал».
Задача — рассказать ОДИН яркий научный или исторический факт по-человечески.

Формат:

1 строка — цепляющий заголовок + 1–2 эмодзи.
НЕ начинай с «Что ты не знал», «Мозг вскипает», «Мозг взрывается».

1 блок (2–3 предложения) — один конкретный факт из текста.
Обязательно:
- год или диапазон лет;
- хотя бы одно число или процент;
- кто и что сделал (исследователи, учёные, миссия, эксперимент, исторический персонаж);
- результат с глаголом (показали, обнаружили, измерили, выяснили и т.п.).
Фокус на реальном мире: космос, мозг, тело, привычки, история, техника.
Если текст только про сайт/журнал/музей/портал — верни «SKIP».

пустая строка

2 блок (2–3 предложения) — простое объяснение, как это работает или почему так получается.
Разговорный тон, без пафоса.

пустая строка

3 блок (1–2 предложения) — практический вывод для читателя (что можно сделать/переосмыслить сегодня).
Без рекламы, без «подписывайся», без «загугли», без «следи за новостями».

пустая строка

Финал — вопрос читателю про личный опыт/восприятие (без политики и без новостей).

Последняя строка — 3–4 хэштега на русском (#наука, #психология, #история, #здоровье и т.п., без названия канала).

Жёстко запрещено:
- обсуждать текущую политику, войны, решения судов, санкции, права человека;
- оправдывать/обвинять страны, народы, религии;
- писать про «международный суд», «ООН», «санкции», «геноцид», «военные преступления»;
- звать читать сайт, новости, журнал, «узнать больше в интернете»;
- использовать клише: «проливает новый свет», «это поднимает важные вопросы» и т.п.

Если в тексте НЕТ яркой цифры, года, имени или конкретного кейса — верни «SKIP».
Если текст про новости, политику, суды, санкции, права человека — верни «SKIP».

Максимальная длина поста — 900 символов, цель 700–900.

Исходный текст статьи:
{article}
"""

async def call_groq_fact(item: FactItem) -> Optional[str]:
    model_key = "light" if budget.can_use_model("light") else "heavy"
    if not budget.can_use_model(model_key):
        logger.warning("⚠️ Groq budget exhausted")
        return None

    cfg = MODELS[model_key]

    await budget.wait_for_rate_limit(model_key)

    prompt = FACT_PROMPT.format(article=item.text)

    try:
        # ИСПРАВЛЕНИЕ #1: groq_client теперь AsyncGroq — await корректен
        resp = await groq_client.chat.completions.create(
            model=cfg.name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.9,
            top_p=0.9,
        )
        content = resp.choices[0].message.content.strip()
        usage   = getattr(resp, "usage", None)
        tokens  = usage.total_tokens if usage else 600
        budget.add_tokens(cfg.name, tokens)
        return content
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return None

# ============ TELEGRAM ============

async def send_to_telegram(session: aiohttp.ClientSession, text: str, url: str):
    # ИСПРАВЛЕНИЕ #2: убран хардкод «Что ты не знал» в начале сообщения.
    # ИИ сам генерирует заголовок поста, а «Что ты не знал» — забанённый паттерн
    # заголовка. Добавляем только ссылку на источник в конец.
    full = f"{text}\n\nИсточник: {url}"
    if len(full) > 4096:
        full = full[:4096]

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL_ID, "text": full}
    try:
        async with session.post(api_url, json=payload, timeout=HTTP_TIMEOUT) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.error(f"Telegram HTTP {resp.status}: {body[:200]}")
            else:
                logger.info("✅ Posted to Telegram")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ============ MAIN ============

async def main():
    logger.info("🚀 Starting Facts Autopost")

    links = load_links()
    if not links:
        return

    random.shuffle(links)

    async with aiohttp.ClientSession() as session:
        items: List[FactItem] = []
        for url in links[:40]:
            item = await fetch_article_from_source(session, url)
            if item and not state.is_posted(item.uid):
                items.append(item)

        logger.info(f"📦 Got {len(items)} candidate articles")

        if not items:
            logger.info("No candidate facts")
            return

        dominant = state.needs_diversity()
        if dominant:
            others = []
            same   = []
            for it in items:
                t = extract_topic(it.text)
                if t == dominant:
                    same.append(it)
                else:
                    others.append(it)
            items = others + same
            logger.info(f"⚖️ Reordered items: {len(others)} other topics first")
        else:
            random.shuffle(items)

        posts_done        = 0
        attempts          = 0
        duplicates_skipped = 0
        rejected          = 0

        for it in items:
            if posts_done >= MAX_POSTS_PER_RUN:
                break
            if attempts >= MAX_ATTEMPTS:
                logger.info("Max attempts reached")
                break

            attempts += 1
            logger.info(f"🔍 [{attempts}/{MAX_ATTEMPTS}] {it.url}")

            if state.is_duplicate(it.title, it.text):
                topic = extract_topic(it.text)
                state.mark_posted(it.uid, it.title, it.text, topic)
                duplicates_skipped += 1
                continue

            if state.is_too_similar_to_recent(it.title, it.text):
                topic = extract_topic(it.text)
                state.mark_posted(it.uid, it.title, it.text, topic)
                duplicates_skipped += 1
                continue

            post_text = await call_groq_fact(it)
            if not post_text:
                topic = extract_topic(it.text)
                state.mark_posted(it.uid, it.title, it.text, topic)
                rejected += 1
                continue

            post_text = normalize_blank_lines(post_text)

            if post_text.strip().upper().startswith("SKIP"):
                logger.info("AI returned SKIP")
                topic = extract_topic(it.text)
                state.mark_posted(it.uid, it.title, it.text, topic)
                rejected += 1
                continue

            if len(post_text) < MIN_POST_LEN:
                logger.info(f"Post too short ({len(post_text)})")
                topic = extract_topic(it.text)
                state.mark_posted(it.uid, it.title, it.text, topic)
                rejected += 1
                continue

            if len(post_text) > MAX_POST_LEN + 50:
                logger.info(f"Post too long ({len(post_text)}), clipping")
                post_text = smart_clip(post_text)

            if not has_strong_fact(post_text):
                logger.info("No strong fact, skip")
                topic = extract_topic(it.text)
                state.mark_posted(it.uid, it.title, it.text, topic)
                rejected += 1
                continue

            if looks_like_announcement(post_text):
                logger.info("Looks like announcement, skip")
                topic = extract_topic(it.text)
                state.mark_posted(it.uid, it.title, it.text, topic)
                rejected += 1
                continue

            if has_banned_title(post_text):
                logger.info("Banned title, skip")
                topic = extract_topic(it.text)
                state.mark_posted(it.uid, it.title, it.text, topic)
                rejected += 1
                continue

            if contains_banned_phrases(post_text):
                logger.info("Contains banned phrase, skip")
                topic = extract_topic(it.text)
                state.mark_posted(it.uid, it.title, it.text, topic)
                rejected += 1
                continue

            if is_banned_topic(post_text):
                logger.info("Banned topic, skip")
                topic = extract_topic(it.text)
                state.mark_posted(it.uid, it.title, it.text, topic)
                rejected += 1
                continue

            await send_to_telegram(session, post_text, it.url)
            topic = extract_topic(it.text)
            state.mark_posted(it.uid, it.title, it.text, topic)
            posts_done += 1

        logger.info(f"📊 Done: {posts_done} posted, {rejected} rejected, {duplicates_skipped} duplicates")
        stats = state.get_recent_topics_stats()
        if stats:
            logger.info(f"📈 Recent topics: {stats}")

if __name__ == "__main__":
    asyncio.run(main())

