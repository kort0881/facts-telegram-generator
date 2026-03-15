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
LOG_FILE_PATH = "facts_generator.log"

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

# DRY-RUN: True = показывать пост в консоли, НЕ отправлять в Telegram
DRY_RUN = True

# Фиксированная шапка канала
CHANNEL_HEADER = "Что ты не знал"

# -------------------------
# LOGGING
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE_PATH, encoding="utf-8"),
    ],
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
    p = urlparse(url)
    return p.path in ("", "/") and not p.query and not p.fragment


# -------------------------
# ARTICLE FETCHER
# -------------------------
def fetch_article(url: str, used_urls: Set[str], dead_urls: Set[str]) -> Tuple:
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
PROMPT = """... твой большой промпт, без изменений ..."""


# -------------------------
# BANNED PHRASES / TITLE / STOP_WORDS / NORMALISATION / SIMILARITY
# -------------------------
# ⬆️ Оставь все эти блоки ровно как в последней версии, где мы уже
# добавили "меняет наше понимание" и новые soft-CTA.
# Я их здесь не дублирую, чтобы не заливать простыню ещё раз.


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
    parts = text.strip().split("\n\n", 2)
    if len(parts) < 2:
        return False
    first_block = parts[1]

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
    return bool(re.search(
        r"(теперь ты можешь|теперь любой может|был ли у тебя опыт|было ли у тебя|бывало ли у тебя)",
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
# SMART CLIP
# -------------------------
def smart_clip_post(post: str, limit: int = 900, min_cut: int = 400) -> str:
    """
    Обрезает пост до limit символов и откатывается к концу последнего
    предложения (., !, ?) чтобы не оборвать на полуслове.
    min_cut — минимальная длина, после которой имеет смысл искать точку.
    """
    if len(post) <= limit:
        return post

    raw = post[:limit]

    # ищем последний знак конца предложения
    last_dot = raw.rfind(".")
    last_q = raw.rfind("?")
    last_exc = raw.rfind("!")

    cut_pos = max(last_dot, last_q, last_exc)

    # если нашли знак и он не в начале, и текст до него не слишком короткий
    if cut_pos != -1 and cut_pos >= min_cut:
        clipped = raw[:cut_pos + 1].rstrip()
        log.info(f"Smart clipped post at position {cut_pos + 1}")
        return clipped

    log.info(f"Hard clipped post at {limit} chars (no good sentence end found)")
    return raw.rstrip()


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
# TELEGRAM / DRY-RUN
# -------------------------
def send_telegram(cfg: Dict, text: str, url: str) -> None:
    full_post = f"{CHANNEL_HEADER}\n\n{text}"
    msg = f"{full_post}\n\nИсточник: {url}"
    if len(msg) > TELEGRAM_LIMIT:
        msg = msg[:TELEGRAM_LIMIT]

    if globals().get("DRY_RUN", False):
        print("\n" + "=" * 80)
        print("DRY RUN — сообщение НЕ отправлено в Telegram")
        print("=" * 80)
        print(msg)
        print("=" * 80 + "\n")
        log.info("DRY RUN: post printed to console instead of sending to Telegram")
    else:
        resp = requests.post(
            f"https://api.telegram.org/bot{cfg['tg_token']}/sendMessage",
            json={"chat_id": cfg["tg_chat"], "text": msg},
        )
        resp.raise_for_status()
        log.info("Post sent to Telegram")

    save_post(text)
    topic_words = extract_topic_words(text)
    save_topic(topic_words)
    log.info(f"Saved topic keywords: {topic_words}")


# -------------------------
# SOURCE PICKER / HELPERS
# -------------------------
# (оставь как в твоём последнем файле — pick_sources, mark_used и т.д.)


# -------------------------
# MAIN
# -------------------------
def main() -> None:
    log.info("Starting generator (DRY_RUN=%s)", DRY_RUN)
    cfg = load_config()

    missing = [k for k in ("ai_url", "ai_model", "ai_key", "tg_token", "tg_chat") if not cfg.get(k)]
    if missing and not DRY_RUN:
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
                log.info(f"Post too long ({len(post)} chars), smart clipping to 900")
                post = smart_clip_post(post, limit=900, min_cut=400)

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

            # --- Send or DRY-RUN ---
            send_telegram(cfg, post, article_url)
            mark_used(url, article_url, used_urls)

            recent_domains.append(domain)
            if len(recent_domains) > MAX_RECENT_DOMAINS:
                recent_domains.pop(0)

            log.info("Post processed successfully")
            return

        except Exception as e:
            log.error(f"Failed on {article_url}: {e}")
            continue

    log.warning("All sources exhausted or failed")


if __name__ == "__main__":
    main()

