#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Facts Generator ULTRA
Improved Telegram fact post generator
"""

import os
import random
import requests
import yaml
import logging
import sys
import re
import json
from typing import List, Set, Dict, Tuple
from collections import Counter

from bs4 import BeautifulSoup

# =========================
# CONFIG FILES
# =========================

CONFIG_PATH = "config.yaml"
LINKS_PATH = "links.txt"
USED_LINKS_PATH = "used_links.txt"
DEAD_LINKS_PATH = "dead_links.txt"
POSTS_LOG_PATH = "used_posts.txt"
TOPICS_LOG_PATH = "used_topics.txt"   # NEW: тематический трекер

# =========================
# SETTINGS
# =========================

MAX_ARTICLE_CHARS = 2500
MAX_FETCH_ATTEMPTS = 12
HTTP_TIMEOUT = 20
TELEGRAM_LIMIT = 4096

# анти-дубликатор
RECENT_SIMILARITY_THRESHOLD = 0.45   # понижено с 0.6 — было слишком мягко
BIGRAM_SIMILARITY_THRESHOLD = 0.30   # NEW: отдельный порог для биграмм
SIMILARITY_WINDOW = 50               # NEW: сравниваем только с последними N постами
MAX_STORED_POSTS = 300               # сколько постов держать в used_posts.txt

# тематический трекер
TOPIC_BLOCK_WINDOW = 10             # NEW: блокируем ту же тему на N постов вперёд
TOPIC_TOP_WORDS = 8                 # NEW: сколько ключевых слов извлекаем из поста

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

log = logging.getLogger("facts_bot")

# =========================
# CONFIG
# =========================


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return {
        "ai_url": data["ai"]["url"],
        "ai_model": data["ai"]["model"],
        "ai_key": os.environ.get("GROQ_API_KEY"),
        "tg_token": os.environ.get("TG_BOT_TOKEN"),
        "tg_chat": os.environ.get("TG_CHAT_ID"),
    }

# =========================
# LINK LOADING
# =========================


def load_links():
    links = []

    with open(LINKS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            if line.startswith("#"):
                continue

            if line.startswith("["):
                continue

            if line.startswith("http"):
                links.append(line)

    return links


def load_list(path):
    if not os.path.exists(path):
        return set()

    with open(path, "r", encoding="utf-8") as f:
        return {x.strip() for x in f if x.strip()}


def save_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# =========================
# HTTP
# =========================


def http_get(url):
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=HTTP_TIMEOUT,
        )

        if r.status_code >= 400:
            return None

        return r

    except Exception:
        return None

# =========================
# ARTICLE EXTRACTION
# =========================


def extract_article_links(url):
    r = http_get(url)

    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if href.startswith("/"):
            href = requests.compat.urljoin(url, href)

        if any(x in href for x in [
            "/article/",
            "/news/",
            "/story/",
            "/202",
            "/post",
        ]):
            links.append(href)

    return list(set(links))[:20]


def extract_text(html):
    soup = BeautifulSoup(html, "html.parser")
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    text = "\n".join(paragraphs)
    return text[:MAX_ARTICLE_CHARS]


def fetch_article(url, used_urls, dead_urls):
    """
    Получает статью по url.
    Если url — страница-категория, извлекает список статей и выбирает
    одну из неиспользованных.
    Возвращает (article_url, text) или (None, None).
    """

    is_category = any(x in url for x in [
        "history", "culture", "brain", "ideas", "topic",
        "category", "lifeandstyle", "subject", "essays",
        "future", "posts", "articles",
    ])

    if is_category:
        articles = extract_article_links(url)
        articles = [a for a in articles if a not in used_urls and a not in dead_urls]

        if not articles:
            log.info(f"No fresh articles found on category page: {url}")
            return None, None

        random.shuffle(articles)

        for article_url in articles:
            log.info(f"Trying article: {article_url}")
            r = http_get(article_url)

            if not r:
                save_line(DEAD_LINKS_PATH, article_url)
                dead_urls.add(article_url)
                continue

            text = extract_text(r.text)

            if len(text) < 300:
                log.info(f"Too short, skipping: {article_url}")
                continue

            return article_url, text

        return None, None

    r = http_get(url)

    if not r:
        return None, None

    text = extract_text(r.text)

    if len(text) < 300:
        return None, None

    return url, text

# =========================
# AI PROMPT
# =========================

PROMPT = """
Ты пишешь пост для Telegram-канала «Что ты не знал».

Ты — автор живого, разговорного канала с фактами для широкой аудитории 18–35 лет. Твоя задача — рассказывать один яркий факт коротко, просто и по‑человечески, как другу в чате.

Формат поста (строго соблюдай структуру и пустые строки):

1 строка — короткий цепляющий заголовок + 1–2 эмодзи.
Обязательные правила для заголовка:
- Не начинай каждый заголовок с «Что ты не знал».
- Используй разные шаблоны: «Факт дня: …», «Мозг вскипает от этого: …», «Неочевидная штука про …», «Вот что скрывается за …».
- Допускается использовать «Что ты не знал» максимум в каждом третьем посте.

1 блок (2–3 предложения) — один конкретный факт из текста:
- цифра, пример, эксперимент, находка, цитата или конкретный случай;
- без общих фраз уровня «это важно для нашего благополучия» или «это открывает важную дискуссию»;
- можно добавить 1 уместный эмодзи в конце одного из предложений.

пустая строка

2 блок (2–3 предложения) — простое объяснение:
- почему это важно, что это меняет в понимании темы;
- можно сравнить «как было раньше» и «что показывают новые данные»;
- используй разговорный тон: как будто объясняешь другу, а не пишешь статью;
- можно вставить короткую фразу вроде «звучит странно, но так и есть» или «по-честному, это меняет картинку».

пустая строка

3 блок (1–2 предложения) — практический вывод:
- в формате мини-инструкции: начни делать X / обрати внимание на Y / перестань делать Z;
- никаких общих абстракций, только то, что читатель может применить в жизни или в мышлении;
- можно добавить 1 эмодзи в конце предложения, если уместно.

пустая строка

Короткий блок-подсказка:
- Одна строка: «Если хочешь копнуть глубже — загугли: …»
- После этого перечисли через запятую 2–3 коротких поисковых подсказки БЕЗ реальных ссылок и URL.
  Примеры формата: «долгосрочные последствия сотрясений», «история художника Хиллиарда», «идентичность-основанные привычки».

пустая строка

Финал — вопрос читателю:
- вопрос должен цеплять личный опыт или позицию («было ли у тебя так?», «смог бы ты так поступить?», «готов ли ты попробовать это на себе?»);
- избегай общих вопросов вроде «что вы думаете по этому поводу?»;
- не начинай каждый вопрос словами «как ты думаешь» или «что бы ты сделал», меняй конструкции, используй разные подходы.

Последняя строка — 3–4 хэштега на русском:
- сначала тема (#психология, #история, #наука, #политика, #здоровье, #привычки и т.п.), потом уточнения;
- не дублируй одно и то же слово в разных формах;
- не используй в хэштегах имя канала.

Стиль:
- Пиши коротко, живо, разговорным русским, без канцелярита и сложных оборотов.
- В каждом посте должен быть ОДИН главный факт и ОДНА понятная мысль вокруг него, но можно добавить 1–2 маленькие детали или примера.
- Чередуй короткие и чуть более длинные предложения, чтобы текст читался легко.
- Можно использовать максимум 1–2 выделения **жирным** для самых важных слов, но не злоупотребляй.
- Не копируй формулировки из примеров, придумывай свои.

Эмодзи:
- В заголовке — 1–2 ярких эмодзи.
- В каждом блоке допускается не более 1 эмодзи.
- На весь пост максимум 5 эмодзи.
- Не используй один и тот же эмодзи во всех постах подряд, варьируй.

Запрещённые и нежелательные формулировки:
- Не используй фразы: «проливает новый свет», «мы можем глубже понять эпоху», «это открывает дискуссию», «позволяет лучше понять наши корни», «это важно для нашего благополучия», «новые данные показывают».
- Если хочешь сказать то же самое — перефразируй простыми, живыми словами.
- Не превращай текст в сухую новость или научную заметку.

Разнообразие:
- Не делай два поста подряд на одну и ту же тему с тем же углом (например, две одинаковые истории про травмы головы или один и тот же художник).
- Если исходный текст про тему, которая уже была, выбирай другой аспект: новую цифру, необычный пример, редкий кейс, а не пересказывай то же самое иначе.
- Не начинай каждый пост одинаково («Исследования показали, что…», «Недавно учёные обнаружили…»). Меняй заходы: «Представь ситуацию…», «Обычно мы думаем, что…», «Есть одна странная деталь…».

Новизна:
- Каждый новый пост должен звучать как новая история, а не перефразирование предыдущего.
- Избегай повторения одних и тех же формулировок между разными постами.

Допустимая длина всего поста (без источника) — максимум 900 символов.

Твоя задача: по исходному тексту ниже придумать НОВЫЙ пост в этом стиле и структуре, с ОДНИМ главным фактом, понятным объяснением и практическим выводом.

Исходный текст статьи:
{article}
"""

# =========================
# BANNED PHRASES
# =========================

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
]

# =========================
# STOP-WORDS ДЛЯ ТЕМАТИЧЕСКОГО ТРЕКЕРА
# =========================

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

# =========================
# TEXT NORMALISATION
# =========================


def normalize_text(s: str) -> Set[str]:
    """Множество уникальных значимых слов (>3 символов) для Jaccard."""
    s = s.lower()
    s = re.sub(r"[^a-zа-я0-9ё]+", " ", s)
    words = [w for w in s.split() if len(w) > 3 and w not in STOP_WORDS]
    return set(words)


def get_bigrams(s: str) -> Set[Tuple[str, str]]:
    """Множество биграмм из значимых слов."""
    words = sorted(normalize_text(s))   # сортируем для инвариантности порядка
    if len(words) < 2:
        return set()
    return {(words[i], words[i + 1]) for i in range(len(words) - 1)}


def extract_topic_words(s: str, top_n: int = TOPIC_TOP_WORDS) -> List[str]:
    """Извлекает топ-N самых частых значимых слов — «тема» поста."""
    s = s.lower()
    s = re.sub(r"[^a-zа-я0-9ё]+", " ", s)
    words = [w for w in s.split() if len(w) > 4 and w not in STOP_WORDS]
    counted = Counter(words)
    return [w for w, _ in counted.most_common(top_n)]

# =========================
# SIMILARITY METRICS
# =========================


def jaccard_similarity(a: str, b: str) -> float:
    """Jaccard по множеству слов."""
    sa = normalize_text(a)
    sb = normalize_text(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def bigram_jaccard(a: str, b: str) -> float:
    """Jaccard по биграммам — ловит похожие фразы."""
    ba = get_bigrams(a)
    bb = get_bigrams(b)
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union else 0.0


def combined_similarity(a: str, b: str) -> float:
    """Взвешенная комбинация: 60% слова + 40% биграммы."""
    return 0.6 * jaccard_similarity(a, b) + 0.4 * bigram_jaccard(a, b)

# =========================
# POSTS STORAGE
# =========================


def load_posts(path: str = POSTS_LOG_PATH) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def save_post(text: str, path: str = POSTS_LOG_PATH):
    posts = load_posts(path)
    posts.append(text.replace("\n", " \\n "))
    if len(posts) > MAX_STORED_POSTS:
        posts = posts[-MAX_STORED_POSTS:]
    with open(path, "w", encoding="utf-8") as f:
        for p in posts:
            f.write(p + "\n")

# =========================
# TOPIC TRACKER
# =========================


def load_recent_topics(path: str = TOPICS_LOG_PATH) -> List[List[str]]:
    """
    Загружает список тем последних постов.
    Каждая тема — список ключевых слов, сохранённый как JSON-строка.
    """
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


def save_topic(words: List[str], path: str = TOPICS_LOG_PATH):
    """Добавляет тему нового поста и обрезает старые."""
    topics = load_recent_topics(path)
    topics.append(words)
    # держим только последние TOPIC_BLOCK_WINDOW * 3 тем
    max_topics = TOPIC_BLOCK_WINDOW * 3
    if len(topics) > max_topics:
        topics = topics[-max_topics:]
    with open(path, "w", encoding="utf-8") as f:
        for t in topics:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")


def is_topic_repeated(new_post: str, recent_topics: List[List[str]]) -> bool:
    """
    Возвращает True, если тема нового поста слишком близка
    к одной из тем последних TOPIC_BLOCK_WINDOW постов.
    Критерий: пересечение ключевых слов >= 50%.
    """
    new_words = set(extract_topic_words(new_post))
    if not new_words:
        return False

    # сравниваем только с последними N темами
    window = recent_topics[-TOPIC_BLOCK_WINDOW:]

    for old_words in window:
        old_set = set(old_words)
        if not old_set:
            continue
        overlap = len(new_words & old_set)
        # доля пересечения относительно меньшего множества
        ratio = overlap / min(len(new_words), len(old_set))
        if ratio >= 0.5:
            log.info(
                f"Topic overlap {ratio:.2f} with recent post "
                f"(shared: {new_words & old_set})"
            )
            return True

    return False

# =========================
# DUPLICATE & BANALITY CHECKS
# =========================


def is_too_similar_to_previous(
    new_post: str,
    old_posts: List[str],
    word_threshold: float = RECENT_SIMILARITY_THRESHOLD,
    bigram_threshold: float = BIGRAM_SIMILARITY_THRESHOLD,
) -> bool:
    """
    Проверяет сходство нового поста с окном последних SIMILARITY_WINDOW постов.
    Срабатывает если превышен ЛЮБОЙ из двух порогов.
    """
    # берём только последние N постов — актуальнее и быстрее
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
            return True
    return False

# =========================
# AI CALL
# =========================


def call_ai(cfg, article):
    payload = {
        "model": cfg["ai_model"],
        "messages": [
            {"role": "user", "content": PROMPT.format(article=article)}
        ],
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

# =========================
# TELEGRAM
# =========================


def send_telegram(cfg, text, url):
    msg = f"{text}\n\nИсточник: {url}"

    if len(msg) > TELEGRAM_LIMIT:
        msg = msg[:TELEGRAM_LIMIT]

    resp = requests.post(
        f"https://api.telegram.org/bot{cfg['tg_token']}/sendMessage",
        json={
            "chat_id": cfg["tg_chat"],
            "text": msg,
        },
    )
    resp.raise_for_status()

    # логируем текст поста для анти-дублей
    save_post(text)

    # логируем тему поста
    topic_words = extract_topic_words(text)
    save_topic(topic_words)
    log.info(f"Saved topic keywords: {topic_words}")

# =========================
# SOURCE PICKER
# =========================


def pick_sources(all_links):
    used = load_list(USED_LINKS_PATH)
    dead = load_list(DEAD_LINKS_PATH)

    available = [x for x in all_links if x not in used and x not in dead]

    random.shuffle(available)
    return available[:MAX_FETCH_ATTEMPTS], used, dead

# =========================
# MAIN
# =========================


def main():
    log.info("Starting generator")

    cfg = load_config()
    links = load_links()
    candidates, used_urls, dead_urls = pick_sources(links)

    if not candidates:
        log.warning("No available sources. Clear used_links.txt to reset.")
        return

    old_posts = load_posts()
    recent_topics = load_recent_topics()

    for url in candidates:
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

            if len(post) < 200:
                log.info("Generated post is too short, skipping")
                continue

            # 1. фильтр банальных фраз
            if contains_banned_phrases(post):
                log.info("Post contains banned phrases, skipping")
                continue

            # 2. анти-дубликатор по тексту (слова + биграммы)
            if is_too_similar_to_previous(post, old_posts):
                log.info("Generated post is too similar to previous ones, skipping")
                save_line(USED_LINKS_PATH, url)
                used_urls.add(url)
                if article_url != url:
                    save_line(USED_LINKS_PATH, article_url)
                    used_urls.add(article_url)
                continue

            # 3. тематический трекер — блокируем ту же тему
            if is_topic_repeated(post, recent_topics):
                log.info("Topic already covered recently, skipping")
                save_line(USED_LINKS_PATH, url)
                used_urls.add(url)
                if article_url != url:
                    save_line(USED_LINKS_PATH, article_url)
                    used_urls.add(article_url)
                continue

            # отправка
            send_telegram(cfg, post, article_url)

            # сохраняем источники
            save_line(USED_LINKS_PATH, url)
            used_urls.add(url)

            if article_url != url:
                save_line(USED_LINKS_PATH, article_url)
                used_urls.add(article_url)

            log.info("Post sent successfully")
            return

        except Exception as e:
            log.error(f"Failed on {article_url}: {e}")
            continue

    log.warning("All sources exhausted or failed")


if __name__ == "__main__":
    main()
