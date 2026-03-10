#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Facts Generator ULTRA
Improved Telegram fact post generator

Key improvements:
- Uses ALL sources instead of one topic
- Extracts real articles from category pages
- Shorter context for better AI quality
- Cleaner Telegram-style posts
- Prevents reused URLs
- Better logging
"""

import os
import random
import requests
import yaml
import datetime
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

    return list(set(links))[:10]


def extract_text(html):

    soup = BeautifulSoup(html, "html.parser")

    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]

    text = "\n".join(paragraphs)

    return text[:MAX_ARTICLE_CHARS]


def fetch_article(url):

    if any(x in url for x in [
        "history",
        "culture",
        "brain",
        "ideas",
        "topic",
        "category",
    ]):

        articles = extract_article_links(url)

        if articles:
            url = random.choice(articles)
            log.info(f"Article extracted: {url}")

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
Ты пишешь пост для Telegram канала "Что ты не знал".

Формат:

1 строка — короткий заголовок + 1 эмодзи

2-3 предложения с интересным фактом

пустая строка

2-3 предложения объяснение

пустая строка

1-2 предложения практический вывод

пустая строка

вопрос читателю

последняя строка — 3-4 хэштега

Пиши коротко, живо, разговорным русским.
Максимум 900 символов.

Текст:
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

    data = r.json()

    return data["choices"][0]["message"]["content"].strip()

# =========================
# TELEGRAM
# =========================


def send_telegram(cfg, text, url):

    msg = f"{text}\n\nИсточник: {url}"

    if len(msg) > TELEGRAM_LIMIT:
        msg = msg[:TELEGRAM_LIMIT]

    requests.post(
        f"https://api.telegram.org/bot{cfg['tg_token']}/sendMessage",
        json={
            "chat_id": cfg["tg_chat"],
            "text": msg,
        },
    )

# =========================
# SOURCE PICKER
# =========================


def pick_sources(all_links):

    used = load_list(USED_LINKS_PATH)
    dead = load_list(DEAD_LINKS_PATH)

    available = [x for x in all_links if x not in used and x not in dead]

    random.shuffle(available)

    return available[:MAX_FETCH_ATTEMPTS]

# =========================
# MAIN
# =========================


def main():

    log.info("Starting generator")

    cfg = load_config()

    links = load_links()

    candidates = pick_sources(links)

    for url in candidates:

        log.info(f"Trying {url}")

        article_url, text = fetch_article(url)

        if not text:
            save_line(DEAD_LINKS_PATH, url)
            continue

        try:

            post = call_ai(cfg, text)

            send_telegram(cfg, post, article_url)

            save_line(USED_LINKS_PATH, url)

            log.info("Post sent")

            return

        except Exception as e:

            log.error(e)

    log.info("All sources failed")


if __name__ == "__main__":

    main()

