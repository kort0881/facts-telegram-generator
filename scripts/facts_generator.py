#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Facts Telegram Generator
Auto-generates Telegram fact posts via AI (Groq/OpenAI-compatible) + GitHub Actions
"""

import datetime
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
import yaml
from bs4 import BeautifulSoup

# ---------- Constants ----------

CONFIG_PATH = "config.yaml"
LINKS_PATH = "links.txt"
USED_LINKS_PATH = "used_links.txt"
DEAD_LINKS_PATH = "dead_links.txt"  # 4xx / permanently broken URLs

MAX_FETCH_ATTEMPTS = 3  # How many URLs to try before giving up on a topic

TELEGRAM_MAX_LENGTH = 4096
MAX_ARTICLE_CHARS = 6_000
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3
HTTP_BACKOFF = 3

TOPICS = [
    {"id": "habits",  "name": "Привычки и поведение"},
    {"id": "memory",  "name": "Память и мышление"},
    {"id": "history", "name": "История и культура"},
    {"id": "laws",    "name": "Странные законы и социальные нормы"},
]

# ---------- Logging ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("facts_generator")


# ---------- Config ----------

@dataclass
class Config:
    """Holds runtime configuration, keeping secrets out of the file."""
    ai_url: str
    ai_model: str
    ai_api_key: str
    ai_max_tokens: int
    tg_bot_token: str
    tg_chat_id: str
    output_dir: str
    current_topic_index: int

    # Fields that should never be persisted
    _SECRET_FIELDS = {"ai_api_key", "tg_bot_token", "tg_chat_id"}

    def to_yaml_dict(self) -> Dict[str, Any]:
        """Return only non-secret, non-runtime fields suitable for saving."""
        return {
            "ai": {
                "url": self.ai_url,
                "model": self.ai_model,
                "max_tokens": self.ai_max_tokens,
            },
            "output": {"directory": self.output_dir},
            "current_topic_index": self.current_topic_index,
        }


def load_config() -> Config:
    """Load config from YAML and inject secrets from environment variables."""
    if not os.path.exists(CONFIG_PATH):
        logger.error(f"{CONFIG_PATH} not found.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}

    ai_raw = raw.get("ai", {})
    tg_raw = raw.get("telegram", {})

    api_key   = os.environ.get("GROQ_API_KEY", "").strip()
    bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id   = os.environ.get("TG_CHAT_ID", "").strip()

    missing = []
    if not api_key:   missing.append("GROQ_API_KEY")
    if not bot_token: missing.append("TG_BOT_TOKEN")
    if not chat_id:   missing.append("TG_CHAT_ID")

    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    cfg = Config(
        ai_url=ai_raw.get("url", "https://api.groq.com/openai/v1/chat/completions").strip(),
        ai_model=os.environ.get("GROQ_MODEL", ai_raw.get("model", "llama-3.3-70b-versatile")).strip(),
        ai_api_key=api_key,
        ai_max_tokens=ai_raw.get("max_tokens", 900),
        tg_bot_token=bot_token,
        tg_chat_id=chat_id,
        output_dir=raw.get("output", {}).get("directory", "output/posts"),
        current_topic_index=int(raw.get("current_topic_index", 0)),
    )
    logger.info(f"Configuration loaded. model={cfg.ai_model}")
    return cfg


def save_config(cfg: Config) -> None:
    """Persist only non-secret config fields back to YAML."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg.to_yaml_dict(), f, allow_unicode=True, default_flow_style=False)


# ---------- Utils ----------

def trim_to_telegram_limit(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> str:
    """
    Trim text to Telegram's character limit, cutting on a natural boundary
    (sentence end → newline → word) when possible.
    """
    if len(text) <= max_length:
        return text

    truncated = text[: max_length - 3]
    cut = max(truncated.rfind("."), truncated.rfind("\n"), truncated.rfind(" "))

    if cut > max_length * 0.7:
        return text[: cut + 1].strip() + "..."
    return truncated.strip() + "..."


def http_get_with_retries(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = HTTP_TIMEOUT,
    max_retries: int = HTTP_RETRIES,
    backoff: int = HTTP_BACKOFF,
) -> Optional[requests.Response]:
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=headers or {}, timeout=timeout)
            if resp.status_code >= 500:
                logger.warning(f"Server error {resp.status_code} on {url} (attempt {attempt})")
                if attempt < max_retries:
                    time.sleep(backoff * attempt)
                    continue
            return resp
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.warning(f"Network error on {url} (attempt {attempt}): {e}")
            if attempt < max_retries:
                time.sleep(backoff * attempt)
        except Exception as e:
            logger.error(f"Unexpected error fetching {url}: {e}")
            return None
    return None


def http_post_with_retries(
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout: int = 20,
    max_retries: int = HTTP_RETRIES,
    retry_delay: int = HTTP_BACKOFF,
) -> Optional[requests.Response]:
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", retry_delay))
                logger.warning(f"Rate limited (429), retrying in {wait}s")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout (attempt {attempt + 1}/{max_retries})")
        except requests.exceptions.ConnectionError:
            logger.warning(f"Connection error (attempt {attempt + 1}/{max_retries})")
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            return None

        if attempt < max_retries - 1:
            time.sleep(retry_delay * (2 ** attempt))

    logger.error(f"All {max_retries} attempts failed for {url}")
    return None


# ---------- Content fetching ----------

class FetchError(Exception):
    """Raised when a URL is permanently broken (4xx) and should be blacklisted."""


def fetch_article_text(url: str) -> str:
    """
    Fetch URL and return plain-text content of all <p> tags.

    Raises FetchError for permanent client errors (4xx) so the caller
    can blacklist the URL and avoid retrying it in future runs.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FactsBot/1.0)"}
    resp = http_get_with_retries(url, headers=headers)

    if resp is None:
        logger.error(f"No response from {url}")
        return ""

    if 400 <= resp.status_code < 500:
        raise FetchError(f"HTTP {resp.status_code} (permanent) for {url}")

    try:
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"HTTP error for {url}: {e}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    text = "\n\n".join(paragraphs).strip()
    return text[:MAX_ARTICLE_CHARS]


# ---------- Prompt ----------

PROMPT_TEMPLATE = """\
Ты редактор русскоязычного Telegram-канала "Любопытные факты".

СЕГОДНЯШНЯЯ ТЕМА: {topic_name}

Тебе дают исходный текст (статья, заметка, новость).

Сделай один яркий пост для Telegram-канала в таком формате:

1) Первая строка — цепляющий заголовок с 1–2 эмодзи. Без слова «Заголовок:».
2) Далее — 1–3 коротких предложения с основным фактом. Не пиши слово «Факт:», просто текст.
3) Пустая строка.
4) Блок «Почему это важно» — 2–4 предложения простым разговорным языком.
5) Пустая строка.
6) Блок «Как применить» — 1–2 предложения с практичным выводом.
7) Отдельной строкой — вопрос читателю и 2–4 хэштега по теме.

ТРЕБОВАНИЯ:
- Пиши по-русски, живым, современным стилем.
- Не копируй дословно текст источника, переформулируй.
- Без политики, войн, конфликтов, цензуры, VPN и всего около этого.
- Используй эмодзи умеренно: 3–7 на весь пост, по смыслу.
- Не используй слова «Заголовок», «Факт», «Почему это важно», «Что взять себе» в тексте.
- Только обычный текст, без какого-либо форматирования.

Исходный текст:
{article_text}
"""


def build_prompt(article_text: str, topic_name: str) -> str:
    return PROMPT_TEMPLATE.format(topic_name=topic_name, article_text=article_text)


# ---------- AI API ----------

def call_ai_api(cfg: Config, prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {cfg.ai_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg.ai_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": cfg.ai_max_tokens,
    }

    logger.info(f"Calling AI API: {cfg.ai_url}")
    resp = http_post_with_retries(cfg.ai_url, payload, headers, timeout=60)

    if resp is None:
        logger.error("AI API returned no response")
        return ""

    try:
        data = resp.json()
    except json.JSONDecodeError:
        logger.error(f"AI API returned invalid JSON: {resp.text[:500]}")
        return ""

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        logger.error(f"Unexpected AI API response: {json.dumps(data)[:500]}")
        return ""


# ---------- Telegram ----------

def send_to_telegram(text: str, source_url: str, cfg: Config) -> bool:
    """Send a plain-text post to a Telegram channel."""
    message = trim_to_telegram_limit(f"{text.strip()}\n\n🔗 Источник: {source_url}")

    api_url = f"https://api.telegram.org/bot{cfg.tg_bot_token}/sendMessage"
    payload = {
        "chat_id": cfg.tg_chat_id,
        "text": message,
        "disable_web_page_preview": True,
        "disable_notification": False,
    }

    logger.info(f"Sending to Telegram ({len(message)} chars)")
    resp = http_post_with_retries(api_url, payload, headers={}, timeout=20)

    if resp is None:
        logger.error("Telegram sendMessage failed: no response")
        return False

    try:
        resp.raise_for_status()
        logger.info(f"Message sent to {cfg.tg_chat_id}")
        return True
    except requests.exceptions.HTTPError as e:
        logger.error(f"Telegram HTTP error: {e} — {resp.text[:500]}")
        return False


# ---------- Persistence ----------

def save_post(text: str, source_url: str, topic_name: str, output_dir: str) -> str:
    """Save generated post as a Markdown file."""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(output_dir, f"{timestamp}.md")

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {topic_name}\n\n{text.strip()}\n\n🔗 Источник: {source_url}\n")

    logger.info(f"Post saved: {path}")
    return path


def load_links(path: str = LINKS_PATH) -> Dict[str, List[str]]:
    """Parse links.txt grouped by [topic_id] sections."""
    if not os.path.exists(path):
        logger.error(f"{path} not found")
        sys.exit(1)

    links_by_topic: Dict[str, List[str]] = {t["id"]: [] for t in TOPICS}
    current_topic: Optional[str] = None

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                topic_id = line[1:-1].lower()
                current_topic = topic_id if topic_id in links_by_topic else None
                if current_topic:
                    logger.info(f"Found topic section: {current_topic}")
                continue
            if current_topic and line.startswith("http"):
                links_by_topic[current_topic].append(line)

    return links_by_topic


def load_used_links(path: str = USED_LINKS_PATH) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def load_dead_links(path: str = DEAD_LINKS_PATH) -> set:
    return load_used_links(path)  # same format


def save_used_link(url: str, path: str = USED_LINKS_PATH) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(url + "\n")


def save_dead_link(url: str, path: str = DEAD_LINKS_PATH) -> None:
    logger.info(f"Blacklisting dead URL: {url}")
    with open(path, "a", encoding="utf-8") as f:
        f.write(url + "\n")


# ---------- Topic rotation ----------

def rotate_topic(cfg: Config) -> None:
    """Advance topic index and persist (secrets excluded)."""
    old_name = TOPICS[cfg.current_topic_index]["name"]
    cfg.current_topic_index = (cfg.current_topic_index + 1) % len(TOPICS)
    save_config(cfg)
    logger.info(f"Topic rotated: {old_name} → {TOPICS[cfg.current_topic_index]['name']}")


def pick_unused_urls(cfg: Config, links_by_topic: Dict[str, List[str]], n: int = MAX_FETCH_ATTEMPTS) -> List[str]:
    """Return up to n random unused, non-dead URLs for the current topic."""
    topic = TOPICS[cfg.current_topic_index]
    all_links = links_by_topic.get(topic["id"], [])

    if not all_links:
        logger.warning(f"No links configured for topic '{topic['id']}'")
        return []

    excluded = load_used_links() | load_dead_links()
    available = [u for u in all_links if u not in excluded]

    if not available:
        logger.warning(f"All links for topic '{topic['id']}' are used or dead")
        return []

    random.shuffle(available)
    return available[:n]


# ---------- Main ----------

def generate_post(url: str, topic_name: str, cfg: Config) -> bool:
    """
    Fetch, generate, save and send a single post.

    Returns True on success.
    Raises FetchError if the URL is permanently broken (caller should blacklist it).
    """
    article_text = fetch_article_text(url)  # may raise FetchError
    if not article_text or len(article_text) < 100:
        logger.warning(f"Skipping {url}: source text too short or empty")
        return False

    prompt = build_prompt(article_text, topic_name)
    ai_text = call_ai_api(cfg, prompt)

    if not ai_text or len(ai_text) < 100:
        logger.warning(f"Skipping {url}: AI response too short")
        return False

    save_post(ai_text, url, topic_name, cfg.output_dir)
    send_to_telegram(ai_text, url, cfg)
    save_used_link(url)
    return True


def main() -> None:
    logger.info("Starting Facts Generator...")

    cfg = load_config()
    links_by_topic = load_links()
    topic = TOPICS[cfg.current_topic_index]

    logger.info(f"Current topic: {topic['name']} (index {cfg.current_topic_index})")

    candidates = pick_unused_urls(cfg, links_by_topic)
    if not candidates:
        logger.warning("No available URLs — skipping this run.")
        rotate_topic(cfg)
        return

    success = False
    for url in candidates:
        logger.info(f"Trying: {url}")
        try:
            success = generate_post(url, topic["name"], cfg)
        except FetchError as e:
            logger.error(f"Dead link detected: {e}")
            save_dead_link(url)
            continue  # try next candidate

        if success:
            break
        # transient failure (empty body, short AI reply) — try next
        logger.warning("Trying next candidate URL...")

    logger.info("Post generated successfully." if success else "All candidates failed.")
    rotate_topic(cfg)
    logger.info("Done.")


if __name__ == "__main__":
    main()
