#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Facts Generator ULTRA
Improved Telegram fact post generator with enhanced anti-duplication
"""

import os
import random
import re
import sys
import json
import logging
from typing import List, Set, Dict, Tuple
from collections import Counter
from urllib.parse import urlparse, urljoin

import requests
import yaml
from bs4 import BeautifulSoup

# -------------------------
# CONFIG FILES
# -------------------------
CONFIG_PATH = "config.yaml"
LINKS_PATH = "links.txt"
USED_LINKS_PATH = "used_links.txt"
DEAD_LINKS_PATH = "dead_links.txt"
POSTS_LOG_PATH = "used_posts.txt"
TOPICS_LOG_PATH = "used_topics.txt"

# -------------------------
# SETTINGS
# -------------------------
MAX_ARTICLE_CHARS = 2500
MAX_FETCH_ATTEMPTS = 50
HTTP_TIMEOUT = 20
TELEGRAM_LIMIT = 4096

RECENT_SIMILARITY_THRESHOLD = 0.25
BIGRAM_SIMILARITY_THRESHOLD = 0.15
SIMILARITY_WINDOW = 50
MAX_STORED_POSTS = 300

TOPIC_BLOCK_WINDOW = 10
TOPIC_TOP_WORDS = 8

# Фиксированная шапка канала
CHANNEL_HEADER = "Что ты не знал"

# -------------------------
# LOGGING
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("facts_bot")


# -------------------------
# CONFIG LOADING
# -------------------------
def load_config() -> Dict:
    if not os.path.exists(CONFIG_PATH):
        log.warning(f"Config file {CONFIG_PATH} not found, using defaults")
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    cfg = {
        "ai_url": data.get("ai", {}).get("url", ""),
        "ai_model": data.get("ai", {}).get("model", ""),
        "ai_key": os.environ.get("GROQ_API_KEY", ""),
        "tg_token": os.environ.get("TG_BOT_TOKEN", ""),
        "tg_chat": os.environ.get("TG_CHAT_ID", ""),
    }
    for key in [
        "MAX_ARTICLE_CHARS", "MAX_FETCH_ATTEMPTS", "HTTP_TIMEOUT",
        "TELEGRAM_LIMIT", "RECENT_SIMILARITY_THRESHOLD",
        "BIGRAM_SIMILARITY_THRESHOLD", "SIMILARITY_WINDOW",
        "MAX_STORED_POSTS", "TOPIC_BLOCK_WINDOW", "TOPIC_TOP_WORDS",
    ]:
        if key in data:
            globals()[key] = data[key]
            cfg[key.lower()] = data[key]
    return cfg


# -------------------------
# LINK LOADING
# -------------------------
def load_links() -> List[str]:
    links: List[str] = []
    url_pattern = re.compile(r'https?://[^\s)]+')
    with open(LINKS_PATH, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("http"):
                links.append(line)
                continue
            match = url_pattern.search(line)
            if match:
                links.append(match.group(0))
    log.info(f"Loaded {len(links)} source links")
    return links


def load_list(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {x.strip() for x in f if x.strip()}


def save_line(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# -------------------------
# HTTP HELPERS
# -------------------------
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) rv:124.0 Gecko/20100101 Firefox/124.0",
]


def http_get(url: str):
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            log.debug(f"Bad status {r.status_code} for {url}")
            return None
        return r
    except Exception as e:
        log.debug(f"Request error for {url}: {e}")
        return None


# -------------------------
# ARTICLE EXTRACTION
# -------------------------
def extract_article_links(url: str) -> List[str]:
    r = http_get(url)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            href = urljoin(url, href)
        if any(x in href for x in [
            "/article/", "/news/", "/story/", "/202", "/post",
        ]):
            links.append(href)
    return list(dict.fromkeys(links))[:20]


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    text = "\n".join(paragraphs)
    return text[:MAX_ARTICLE_CHARS]


# -------------------------
# ROOT PAGE DETECTION
# -------------------------
def is_root_page(url: str) -> bool:
    """Returns True if the URL points to a root/homepage (no meaningful path)."""
    p = urlparse(url)
    return p.path in ("", "/") and not p.query and not p.fragment


# -------------------------
# ARTICLE FETCHER
# -------------------------
def fetch_article(url: str, used_urls: Set[str], dead_urls: Set[str]) -> Tuple:
    # 1. Root pages are not articles — skip immediately
    if is_root_page(url):
        log.info(f"Root page detected, skipping as non-article: {url}")
        return None, None

    is_category = any(x in url for x in [
        "history", "culture", "brain", "ideas", "topic",
        "category", "lifeandstyle", "subject", "essays",
        "future", "posts", "articles",
    ])

    if is_category:
        candidates = extract_article_links(url)
        candidates = [c for c in candidates if c not in used_urls and c not in dead_urls]
        if not candidates:
            log.info(f"No fresh articles on category page: {url}")
            return None, None
        random.shuffle(candidates)
        for article_url in candidates:
            log.info(f"Trying article: {article_url}")
            r = http_get(article_url)
            if not r:
                save_line(DEAD_LINKS_PATH, article_url)
                dead_urls.add(article_url)
                continue
            text = extract_text(r.text)
            if len(text) < 300:
                log.info(f"Too short (<300), skipping: {article_url}")
                continue
            return article_url, text
        return None, None

    r = http_get(url)
    if not r:
        return None, None
    text = extract_text(r.text)
    if len(text) < 300:
        log.info(f"Too short (<300), skipping: {url}")
        return None, None
    return url, text


# -------------------------
# AI PROMPT
# -------------------------
PROMPT = """
Ты пишешь пост для Telegram‑канала «Что ты не знал».
Ты — автор живого, креативного канала с фактами для аудитории 18–35 лет.
Задача — рассказать ОДИН яркий факт коротко, конкретно и по‑человечески, максимально варьируя стиль.

ВАЖНО: название канала «Что ты не знал» уже добавляется автоматически перед постом.
НЕ пиши «Что ты не знал» в заголовке — только цепляющий заголовок самого факта.

Формат поста (строго соблюдай структуру и пустые строки):

1 строка — короткий цепляющий заголовок + 1–2 эмодзи.
Обязательно варьируй шаблоны заголовка, не используй один шаблон дважды подряд.
Примеры: «Факт дня: …», «Неочевидная штука про …», «Вот что скрывается за …»,
«История молчит об этом: …», «В твоём мозге прямо сейчас: …», «Странная правда о …».
ЗАПРЕЩЕНО начинать заголовок с: «Мозг вскипает», «Что ты не знал», «Мозг взрывается».

1 блок (2–3 предложения) — один КОНКРЕТНЫЙ факт из текста.
ЖЁСТКОЕ ТРЕБОВАНИЕ — блок ДОЛЖЕН содержать ВСЁ нижеперечисленное:
- год или диапазон лет (например: «в 1987 году», «с 2010 по 2020»);
- хотя бы одно конкретное число или процент (например: «91% участников», «в 3 раза быстрее»);
- кто и что сделал (исследователи, учёные, конкретный человек, название эксперимента/проекта);
- результат с глаголом: показали, обнаружили, измерили, выяснили, зафиксировали, увеличили, снизили.
Блок должен ощущаться как мини‑история или сцена.
ЗАПРЕЩЕНО: описывать сайты, журналы, музеи, экспозиции, издания — только факты о реальном мире.
ЗАПРЕЩЕНО: «журнал публикует», «сайт освещает», «издание рассказывает», «музей показывает».
Если исходный текст ТОЛЬКО про сайт/музей/выставку — верни «SKIP».
Можно добавить 1 уместный эмодзи в конце одного из предложений.

пустая строка

2 блок (2–3 предложения) — простое объяснение:
- почему факт важен, как меняет понимание темы;
- можно сравнить «как мы обычно думаем» и «что показывает пример»;
- разговорный тон, как объяснение другу;
- можно добавить живую фразу («звучит странно, но так и есть», «раньше об этом вообще не думали»).

пустая строка

3 блок (1–2 предложения) — практический вывод:
- не реклама, не призыв «подписаться», «перейти на сайт», «посмотреть статьи»;
- ЗАПРЕЩЕНО: «теперь ты можешь», «теперь любой может», «был ли у тебя опыт»;
- личное, прикладное действие для читателя;
- только то, что читатель может реально применить сегодня;
- если факт короткий — пиши короткий вывод, не растягивай водой;
- можно добавить 1 эмодзи, если уместно.

пустая строка

Финал — вопрос читателю:
- цепляй личный опыт («было ли у тебя так?», «смог бы ты так поступить?»);
- избегай «что вы думаете по этому поводу?»;
- варьируй конструкции, не начинай каждый вопрос одинаково.

Последняя строка — 3–4 хэштега на русском:
- сначала тема (#психология, #история, #наука, #здоровье, #привычки и т.п.);
- не дублируй одно слово в разных формах;
- не используй в хэштегах имя канала.

Стиль и длина:
- Коротко, живо, разговорным русским, без канцелярита.
- ОДИН главный факт и ОДНА понятная мысль вокруг него.
- Цель — 700–900 символов (не более 900).
- Чередуй короткие и длинные предложения.
- Максимум 1–2 выделения **жирным**.
- Не копируй формулировки из примеров.

Запрещённые формулировки — НИКОГДА не использовать:
- «проливает новый свет», «мы можем глубже понять эпоху», «это открывает дискуссию»
- «позволяет лучше понять наши корни», «это важно для нашего благополучия»
- «новые данные показывают», «это поднимает важные вопросы»
- «в современном мире это особенно актуально», «это важно для нас всех»
- «это меняет картину», «по-честному, это меняет картинку»
- «загугли», «узнай больше в интернете», «копнуть глубже», «если хочешь узнать больше»
- «начни интересоваться новостями», «следи за новостями», «будь в курсе последних открытий»
- «подписывайся», «поделись с друзьями», «расскажи друзьям»
- «прямо сейчас посмотреть», «самые популярные статьи», «независимый журнал»
- «публикует исследования», «освещает последние открытия», «делает науку доступной»
- «Мозг вскипает», «Мозг взрывается», «Что ты не знал» (в заголовке)
- «теперь ты можешь», «теперь любой может», «был ли у тебя опыт»

Разнообразие:
- Не делай два поста подряд на одну тему.
- Меняй заходы: «Представь ситуацию…», «Обычно мы думаем, что…», «Есть одна странная деталь…».

Если в тексте НЕТ яркой цифры, года, имени или конкретного кейса — верни «SKIP».
Если текст только про сайт, музей или выставку — верни «SKIP».

Допустимая длина поста — максимум 900 символов, цель 700–900.

Исходный текст статьи:
{article}
"""

# -------------------------
# BANNED PHRASES
# -------------------------
BANNED_PHRASES = [
    # классика
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
    "загугли",
    "если хочешь копнуть глубже",
    "если хочешь узнать больше",
    "узнай больше в интернете",
    "копнуть глубже",
    "начни интересоваться новостями",
    "начни интересоваться новостями археологии",
    "обращай внимание на открытия последних лет",
    "следи за новостями",
    "будь в курсе последних открытий",
    "подписывайся",
    "подписывайся на",
    "поделись с друзьями",
    "расскажи друзьям",
    # реклама сайтов/изданий
    "публикует исследования",
    "освещает последние открытия",
    "на его страницах публикуются",
    "подводят итоги самых популярных",
    "независимый журнал",
    "прямо сейчас посмотреть",
    "прямо сейчас узнать",
    "самые популярные статьи месяца",
    "делает науку доступной",
    "делает ее доступной",
    "любой может прочитать",
    "любой желающий может",
    "каждый месяц они публикуют",
    "каждый месяц подводят",
    "из лабораторий, университетов",
    "из лабораторий и университетов",
    "независимое издание",
    "открытый доступ",
    "бесплатный доступ к статьям",
    # soft CTA / «опыт»
    "теперь ты можешь",
    "теперь любой может",
    "был ли у тебя опыт",
]

# -------------------------
# BANNED TITLE TEMPLATES
# -------------------------
_BANNED_TITLE_PATTERNS = re.compile(
    r"^("
    r"мозг вскипает"
    r"|мозг взрывается"
    r"|мозг вскипел"
    r"|что ты не знал"
    r")",
    re.IGNORECASE,
)


def has_banned_title(text: str) -> bool:
    first_line = text.strip().split("\n", 1)[0].lower()
    if _BANNED_TITLE_PATTERNS.search(first_line):
        log.info(f"Banned title template detected: {first_line[:60]}")
        return True
    return False


# -------------------------
# STOP‑WORDS
# -------------------------
STOP_WORDS = {
    "что", "это", "как", "для", "или", "при", "так", "все", "они",
    "был", "она", "его", "её", "он", "мы", "вы", "но", "да", "нет",
    "уже", "ещё", "даже", "если", "тоже", "есть", "очень", "ведь",
    "себя", "свой", "своя", "своё", "свои", "тот", "эта", "этот",
    "не", "ни", "же", "бы", "по", "до", "из", "без", "над", "под",
    "через", "между", "после", "перед", "среди", "хотя", "когда",
    "который", "которая", "которое", "которые", "можно", "нужно",
    "должен", "может", "будет", "были", "быть", "стало", "стал",
    "одно", "один", "одна", "раз", "два", "три", "лет", "год",
    "загугли", "глубже", "хочешь", "копнуть", "если",
}

# -------------------------
# TEXT NORMALISATION
# -------------------------
def normalize_text(s: str) -> Set[str]:
    s = s.lower()
    s = re.sub(r"[^a-zа-я0-9ё]+", " ", s)
    words = [w for w in s.split() if len(w) > 3 and w not in STOP_WORDS]
    return set(words)


def get_bigrams(s: str) -> Set[Tuple[str, str]]:
    words = sorted(normalize_text(s))
    if len(words) < 2:
        return set()
    return {(words[i], words[i + 1]) for i in range(len(words) - 1)}


def extract_topic_words(s: str, top_n: int = TOPIC_TOP_WORDS) -> List[str]:
    s = s.lower()
    s = re.sub(r"[^a-zа-я0-9ё]+", " ", s)
    words = [w for w in s.split() if len(w) > 4 and w not in STOP_WORDS]
    counted = Counter(words)
    return [w for w, _ in counted.most_common(top_n)]


# -------------------------
# SIMILARITY METRICS
# -------------------------
def jaccard_similarity(a: str, b: str) -> float:
    sa = normalize_text(a)
    sb = normalize_text(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


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


# -------------------------
# POSTS STORAGE
# -------------------------
def load_posts(path: str = POSTS_LOG_PATH) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def save_post(text: str, path: str = POSTS_LOG_PATH) -> None:
    posts = load_posts(path)
    posts.append(text.replace("\n", " \\n "))
    if len(posts) > MAX_STORED_POSTS:
        posts = posts[-MAX_STORED_POSTS:]
    with open(path, "w", encoding="utf-8") as f:
        for p in posts:
            f.write(p + "\n")


# -------------------------
# TOPIC TRACKER
# -------------------------
def load_recent_topics(path: str = TOPICS_LOG_PATH) -> List[List[str]]:
    if not os.path.exists(path):
        return []
    result = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return result


def save_topic(words: List[str], path: str = TOPICS_LOG_PATH) -> None:
    topics = load_recent_topics(path)
    topics.append(words)
    max_topics = TOPIC_BLOCK_WINDOW * 3
    if len(topics) > max_topics:
        topics = topics[-max_topics:]
    with open(path, "w", encoding="utf-8") as f:
        for t in topics:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")


def is_topic_repeated(new_post: str, recent_topics: List[List[str]]) -> bool:
    new_words = set(extract_topic_words(new_post))
    if not new_words:
        return False
    window = recent_topics[-TOPIC_BLOCK_WINDOW:]
    for old_words in window:
        old_set = set(old_words)
        if not old_set:
            continue
        overlap = len(new_words & old_set)
        ratio = overlap / min(len(new_words), len(old_set))
        if ratio >= 0.5:
            log.info(f"Topic overlap {ratio:.2f} (shared: {new_words & old_set})")
            return True
    return False


# -------------------------
# DUPLICATE & BANALITY CHECKS
# -------------------------
def is_too_similar_to_previous(
    new_post: str,
    old_posts: List[str],
    word_threshold: float = RECENT_SIMILARITY_THRESHOLD,
    bigram_threshold: float = BIGRAM_SIMILARITY_THRESHOLD,
) -> bool:
    window = old_posts[-SIMILARITY_WINDOW:]
    for old in window:
        if not old:
            continue
        word_sim = jaccard_similarity(new_post, old)
        if word_sim >= word_threshold:
            log.info(f"Word similarity {word_sim:.2f} >= {word_threshold} — duplicate")
            return True
        bigram_sim = bigram_jaccard(new_post, old)
        if bigram_sim >= bigram_threshold:
            log.info(f"Bigram similarity {bigram_sim:.2f} >= {bigram_threshold} — duplicate")
            return True
    return False


def contains_banned_phrases(text: str) -> bool:
    lower = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lower:
            log.info(f"Banned phrase found: «{phrase}»")
            return True
    return False


# -------------------------
# PROMO / WEBSITE-AD DETECTION
# -------------------------
_PROMO_PATTERNS = re.compile(
    r"("
    r"публику(ет|ются|ют)\s+(исследования|статьи|материалы|открытия)"
    r"|освещает\s+(последние|новые|актуальные)"
    r"|независим(ый|ое)\s+(журнал|издание|ресурс|сайт)"
    r"|на\s+(его|её|их)\s+страницах"
    r"|подводят\s+итоги"
    r"|самые\s+популярные\s+статьи"
    r"|прямо\s+сейчас\s+(посмотреть|узнать|перейти|открыть)"
    r"|делает\s+(науку|её|ее)\s+доступной"
    r"|любой\s+может\s+(прочитать|узнать|найти)"
    r"|из\s+лабораторий.{0,20}университет"
    r"|каждый\s+месяц\s+(они|редакция|журнал)"
    r"|открытый\s+доступ\s+к"
    r"|бесплатн\w+\s+доступ"
    r")",
    re.IGNORECASE,
)

_PROMO_TITLE_PATTERNS = re.compile(
    r"("
    r"\d{1,2}\s+лет\s+\w+\s+(публику|освещ|пишет|рассказыва)"
    r"|с\s+\d{4}\s+года\s+\w+\s+(публику|освещ|пишет)"
    r")",
    re.IGNORECASE,
)


def is_promo_for_website(text: str) -> bool:
    if _PROMO_PATTERNS.search(text):
        log.info("Post looks like website promo — skipping")
        return True

    first_line = text.strip().split("\n", 1)[0]
    if _PROMO_TITLE_PATTERNS.search(first_line):
        log.info("Post title looks like website promo — skipping")
        return True

    first_block_end = text.find("\n\n")
    first_block = text[:first_block_end] if first_block_end != -1 else text
    media_words = re.findall(
        r"\b(журнал|издание|сайт|ресурс|портал|платформа|медиа)\b",
        first_block.lower()
    )
    if len(media_words) >= 2:
        log.info(f"First block mentions media entity {len(media_words)}x — likely promo")
        return True

    return False


# -------------------------
# POST CLEANUP
# -------------------------
_GOOGLE_HINT_PATTERNS = re.compile(
    r"(если хочешь (копнуть глубже|узнать больше)|загугли|узнай больше в интернете"
    r"|копнуть глубже|поищи в интернете)",
    re.IGNORECASE,
)

_BAD_CTA_PATTERNS = re.compile(
    r"("
    r"начни интересоваться новостями"
    r"|обращай внимание на открытия последних лет"
    r"|следи за новостями"
    r"|будь в курсе последних открытий"
    r"|подписывайся"
    r"|поделись с друзьями"
    r"|расскажи друзьям"
    r"|прямо сейчас (посмотреть|узнать|перейти|открыть)"
    r"|самые популярные статьи месяца"
    r"|теперь ты можешь"
    r"|теперь любой может"
    r"|был ли у тебя опыт"
    r")",
    re.IGNORECASE,
)


def strip_google_hint(text: str) -> str:
    match = _GOOGLE_HINT_PATTERNS.search(text)
    if not match:
        return text
    cut_pos = match.start()
    newline_pos = text.rfind("\n", 0, cut_pos)
    if newline_pos != -1:
        cut_pos = newline_pos
    cleaned = text[:cut_pos].rstrip()
    log.info("Stripped google-hint block from post")
    return cleaned


def strip_calls_to_action(text: str) -> str:
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        if _BAD_CTA_PATTERNS.search(line):
            log.info(f"Stripped CTA line: {line[:60]}")
            continue
        cleaned.append(line.rstrip())
    return "\n".join(cleaned).strip()


# -------------------------
# CONTENT QUALITY CHECKS
# -------------------------
def looks_too_vague(text: str) -> bool:
    has_digit = bool(re.search(r"\d", text))
    has_year = bool(re.search(r"19\d{2}|20\d{2}", text))
    return not (has_digit or has_year)


def has_strong_fact(text: str) -> bool:
    years = re.findall(r"19\d{2}|20\d{2}", text)
    digits = re.findall(r"\d", text)
    return len(years) >= 1 and len(digits) >= 3


def has_strict_fact_block(text: str) -> bool:
    """
    The first content block (after the headline) must contain:
    - a year
    - a number/digit
    - a result verb (showed, found, measured, etc.)
    """
    parts = text.strip().split("\n\n", 2)
    if len(parts) < 2:
        return False
    first_block = parts[1]  # parts[0] = headline, parts[1] = first block

    has_year = bool(re.search(r"19\d{2}|20\d{2}", first_block))
    has_number = bool(re.search(r"\d", first_block))
    has_result_verb = bool(re.search(
        r"\b(показал[аи]?|выяснил[аи]?|обнаружил[аи]?|нашл[ие]|измерил[аи]?"
        r"|увеличил[аи]?|снизил[аи]?|повысил[аи]?|зафиксировал[аи]?|доказал[аи]?)\b",
        first_block.lower()
    ))

    if not (has_year and has_number and has_result_verb):
        log.info(
            f"Strict fact check failed: year={has_year}, "
            f"number={has_number}, result_verb={has_result_verb}"
        )
    return has_year and has_number and has_result_verb


def has_forbidden_soft_cta(text: str) -> bool:
    """Hard-skip posts with soft CTA / experience phrases that slipped through cleanup."""
    return bool(re.search(
        r"(теперь ты можешь|теперь любой может|был ли у тебя опыт)",
        text.lower()
    ))


def looks_like_announcement(text: str) -> bool:
    if len(text) < 350:
        return True
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) < 5:
        return True
    first_line = text.strip().split("\n", 1)[0].lower()
    if "каждый год" in first_line or "часто происходит" in first_line:
        return True
    return False


def normalize_blank_lines(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines)


# -------------------------
# AI CALL
# -------------------------
def call_ai(cfg: Dict, article: str) -> str:
    payload = {
        "model": cfg["ai_model"],
        "messages": [{"role": "user", "content": PROMPT.format(article=article)}],
        "max_tokens": 600,
        "temperature": 0.9,
        "top_p": 0.9,
    }
    headers = {
        "Authorization": f"Bearer {cfg['ai_key']}",
        "Content-Type": "application/json",
    }
    r = requests.post(cfg["ai_url"], json=payload, headers=headers, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


# -------------------------
# TELEGRAM
# -------------------------
def send_telegram(cfg: Dict, text: str, url: str) -> None:
    # Фиксированная шапка канала перед каждым постом
    full_post = f"{CHANNEL_HEADER}\n\n{text}"
    msg = f"{full_post}\n\nИсточник: {url}"
    if len(msg) > TELEGRAM_LIMIT:
        msg = msg[:TELEGRAM_LIMIT]
    resp = requests.post(
        f"https://api.telegram.org/bot{cfg['tg_token']}/sendMessage",
        json={"chat_id": cfg["tg_chat"], "text": msg},
    )
    resp.raise_for_status()
    save_post(text)  # сохраняем без шапки — чтобы антидубликатор работал корректно
    topic_words = extract_topic_words(text)
    save_topic(topic_words)
    log.info(f"Saved topic keywords: {topic_words}")


# -------------------------
# SOURCE PICKER
# -------------------------
def pick_sources(all_links: List[str]) -> Tuple[List[str], Set[str], Set[str]]:
    used = load_list(USED_LINKS_PATH)
    dead = load_list(DEAD_LINKS_PATH)
    available = [x for x in all_links if x not in used and x not in dead]
    random.shuffle(available)
    return available[:MAX_FETCH_ATTEMPTS], used, dead


# -------------------------
# HELPERS
# -------------------------
def mark_used(url: str, article_url: str, used_urls: Set[str]) -> None:
    save_line(USED_LINKS_PATH, url)
    used_urls.add(url)
    if article_url != url:
        save_line(USED_LINKS_PATH, article_url)
        used_urls.add(article_url)


# -------------------------
# MAIN
# -------------------------
def main() -> None:
    log.info("Starting generator")
    cfg = load_config()

    missing = [k for k in ("ai_url", "ai_model", "ai_key", "tg_token", "tg_chat") if not cfg.get(k)]
    if missing:
        log.error(f"Missing config values: {', '.join(missing)}")
        return

    links = load_links()
    if not links:
        log.error("No links loaded – check links.txt")
        return

    candidates, used_urls, dead_urls = pick_sources(links)
    if not candidates:
        log.warning("No available sources. Clear used_links.txt to reset.")
        return

    old_posts = load_posts()
    recent_topics = load_recent_topics()
    recent_domains: List[str] = []
    MAX_RECENT_DOMAINS = 5

    for url in candidates:
        domain = urlparse(url).netloc
        if domain in recent_domains:
            log.info(f"Skipping {url} — domain {domain} used recently")
            continue

        log.info(f"Trying source: {url}")
        article_url, text = fetch_article(url, used_urls, dead_urls)

        if not article_url or not text:
            is_category = any(x in url for x in [
                "history", "culture", "brain", "ideas", "topic",
                "category", "lifeandstyle", "subject", "essays",
                "future", "posts", "articles",
            ])
            if not is_category:
                save_line(DEAD_LINKS_PATH, url)
                dead_urls.add(url)
            continue

        if article_url in used_urls:
            log.info(f"Article already used: {article_url}, skipping")
            continue

        try:
            log.info(f"Generating post from: {article_url}")
            post = call_ai(cfg, text)

            # --- Cleanup passes ---
            post = strip_google_hint(post)
            post = strip_calls_to_action(post)
            post = normalize_blank_lines(post)

            # --- Hard skip from AI ---
            if post.strip().upper().startswith("SKIP"):
                log.info("AI returned SKIP — no good fact found in article")
                mark_used(url, article_url, used_urls)
                continue

            # --- Length checks ---
            if len(post) < 350:
                log.info(f"Post too short ({len(post)} chars), skipping")
                continue

            if len(post) > 950:
                log.info(f"Post too long ({len(post)} chars), clipping to 900")
                post = post[:900]

            # --- Content quality ---
            if looks_too_vague(post):
                log.info("Post has no concrete data (numbers/years), skipping")
                continue

            if not has_strong_fact(post):
                log.info("Post has no strong fact (year + numbers), skipping")
                continue

            if not has_strict_fact_block(post):
                log.info("Post fails strict first-block fact check, skipping")
                continue

            if looks_like_announcement(post):
                log.info("Post looks like shallow announcement, skipping")
                continue

            # --- Title / phrase bans ---
            if has_banned_title(post):
                log.info("Post has banned title template, skipping")
                continue

            if contains_banned_phrases(post):
                log.info("Post contains banned phrases, skipping")
                continue

            if has_forbidden_soft_cta(post):
                log.info("Post contains forbidden soft CTA/experience phrase, skipping")
                continue

            # --- Website/promo detection ---
            if is_promo_for_website(post):
                log.info("Post is promo for a website/publication, skipping")
                mark_used(url, article_url, used_urls)
                continue

            # --- Deduplication ---
            if old_posts:
                max_sim = max(
                    combined_similarity(post, old)
                    for old in old_posts[-SIMILARITY_WINDOW:] if old
                )
                log.info(f"Max combined similarity to recent posts: {max_sim:.3f}")

            if is_too_similar_to_previous(post, old_posts):
                log.info("Post is too similar to previous ones, skipping")
                mark_used(url, article_url, used_urls)
                continue

            if is_topic_repeated(post, recent_topics):
                log.info("Topic already covered recently, skipping")
                mark_used(url, article_url, used_urls)
                continue

            # --- Send ---
            send_telegram(cfg, post, article_url)
            mark_used(url, article_url, used_urls)

            recent_domains.append(domain)
            if len(recent_domains) > MAX_RECENT_DOMAINS:
                recent_domains.pop(0)

            log.info("Post sent successfully")
            return

        except Exception as e:
            log.error(f"Failed on {article_url}: {e}")
            continue

    log.warning("All sources exhausted or failed")


if __name__ == "__main__":
    main()
