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
from bs4 import BeautifulSoup

# =========================
# CONFIG FILES
# =========================

CONFIG_PATH = "config.yaml"
LINKS_PATH = "links.txt"
USED_LINKS_PATH = "used_links.txt"
DEAD_LINKS_PATH = "dead_links.txt"

# =========================
# SETTINGS
# =========================

MAX_ARTICLE_CHARS = 2500
MAX_FETCH_ATTEMPTS = 12
HTTP_TIMEOUT = 20
TELEGRAM_LIMIT = 4096

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

    return list(set(links))[:20]  # увеличено с 10 до 20 для большего выбора


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

        # Фильтруем уже использованные и мёртвые ссылки
        articles = [a for a in articles if a not in used_urls and a not in dead_urls]

        if not articles:
            log.info(f"No fresh articles found on category page: {url}")
            return None, None

        # Перемешиваем для случайности
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

    # Прямая ссылка на статью
    r = http_get(url)

    if not r:
        return None, None

    text = extract_text(r.text)

    if len(text) < 300:
        return None, None

    return url, text

# =========================
# AI
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

Допустимая длина всего поста (без источника) — максимум 900 символов.

Твоя задача: по исходному тексту ниже придумать НОВЫЙ пост в этом стиле и структуре, с ОДНИМ главным фактом, понятным объяснением и практическим выводом.

Исходный текст статьи:
{article}
"""

def call_ai(cfg, article):
    payload = {
        "model": cfg["ai_model"],
        "messages": [
            {"role": "user", "content": PROMPT.format(article=article)}
        ],
        "max_tokens": 600,
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

    for url in candidates:
        log.info(f"Trying source: {url}")

        article_url, text = fetch_article(url, used_urls, dead_urls)

        if not article_url or not text:
            # Если прямая ссылка — помечаем мёртвой, категорию не трогаем
            is_category = any(x in url for x in [
                "history", "culture", "brain", "ideas", "topic",
                "category", "lifeandstyle", "subject", "essays",
                "future", "posts", "articles",
            ])
            if not is_category:
                save_line(DEAD_LINKS_PATH, url)
                dead_urls.add(url)
            continue

        # Двойная проверка: статья не должна быть в used
        if article_url in used_urls:
            log.info(f"Article already used: {article_url}, skipping")
            continue

        try:
            log.info(f"Generating post from: {article_url}")
            post = call_ai(cfg, text)

            send_telegram(cfg, post, article_url)

            # Сохраняем оба: исходный url-источник и конкретную статью
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
