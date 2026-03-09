#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Facts Telegram Generator
Auto-generates Telegram fact posts via AI (Groq/OpenAI-compatible) + GitHub Actions
"""

import os
import sys
import json
import time
import datetime
import logging
from typing import Dict, Any, List, Optional

import requests
import yaml
from bs4 import BeautifulSoup
import html

# ---------- Constants ----------

CONFIG_PATH = "config.yaml"
LINKS_PATH = "links.txt"

MAX_TG_LEN = 3800
MAX_ARTICLE_CHARS = 6000

HTTP_TIMEOUT = 20
HTTP_RETRIES = 3
HTTP_BACKOFF = 3

TOPICS = [
    {"id": "habits", "name": "Привычки и поведение"},
    {"id": "memory", "name": "Память и мышление"},
    {"id": "history", "name": "История и культура"},
    {"id": "laws", "name": "Странные законы и социальные нормы"},
]

TELEGRAM_MAX_LENGTH = 4096

# ---------- Logging ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("facts_generator")


# ==================== Конфигурация ====================

def load_config() -> Dict[str, Any]:
    """Load config from YAML file and inject secrets from environment."""
    if not os.path.exists(CONFIG_PATH):
        logger.error(f"{CONFIG_PATH} not found. Create config.yaml with non-sensitive settings.")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # AI section
    cfg.setdefault("ai", {})
    cfg["ai"]["api_key"] = os.environ.get("GROQ_API_KEY", "").strip()
    if not cfg["ai"]["api_key"]:
        logger.error("CRITICAL: GROQ_API_KEY not set in environment variables!")
        sys.exit(1)

    cfg["ai"]["model"] = os.environ.get(
        "GROQ_MODEL",
        cfg["ai"].get("model", "llama-3.3-70b-versatile"),
    ).strip()

    cfg["ai"]["url"] = cfg["ai"].get(
        "url",
        "https://api.groq.com/openai/v1/chat/completions",
    ).strip()

    # Telegram section
    cfg.setdefault("telegram", {})
    cfg["telegram"]["bot_token"] = os.environ.get("TG_BOT_TOKEN", "").strip()
    cfg["telegram"]["chat_id"] = os.environ.get("TG_CHAT_ID", "").strip()

    missing = []
    if not cfg["ai"]["api_key"]:
        missing.append("ai.api_key (GROQ_API_KEY)")
    if not cfg["telegram"]["bot_token"]:
        missing.append("telegram.bot_token (TG_BOT_TOKEN)")
    if not cfg["telegram"]["chat_id"]:
        missing.append("telegram.chat_id (TG_CHAT_ID)")

    if missing:
        logger.error(f"Missing required config: {', '.join(missing)}")
        sys.exit(1)

    logger.info(f"Configuration loaded successfully, model={cfg['ai']['model']}")
    return cfg


# ---------- Utils ----------

def trim_for_telegram(text: str, max_len: int = MAX_TG_LEN) -> str:
    """Trim text to fit Telegram message limit."""
    if len(text) <= max_len:
        return text
    truncated = text[: max_len - 20].rstrip()
    return truncated + "…"


def trim_for_telegram_strict(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> str:
    """Trim text trying to cut on sentence/word boundary."""
    if len(text) <= max_length:
        return text

    truncated = text[:max_length - 3]
    last_period = truncated.rfind(".")
    last_newline = truncated.rfind("\n")
    last_space = truncated.rfind(" ")

    cut_pos = max(last_period, last_newline, last_space)
    if cut_pos > max_length * 0.7:
        return text[: cut_pos + 1].strip() + "..."

    return truncated.strip() + "..."


def escape_html_for_telegram(text: str) -> str:
    """Escape &, <, > for Telegram HTML mode."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def http_get_with_retries(
    url: str,
    timeout: int = HTTP_TIMEOUT,
    max_retries: int = HTTP_RETRIES,
    backoff: int = HTTP_BACKOFF,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[requests.Response]:
    """GET with retry logic."""
    if headers is None:
        headers = {}
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code >= 500:
                logger.warning(
                    f"Server error {resp.status_code} on {url}, attempt {attempt}/{max_retries}"
                )
                if attempt < max_retries:
                    time.sleep(backoff * attempt)
                    continue
            return resp
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.warning(f"Network error on {url}, attempt {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                time.sleep(backoff * attempt)
                continue
            return None
        except Exception as e:
            logger.error(f"Unexpected error on {url}: {e}")
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
    """POST with retries and rate-limit handling."""
    for attempt in range(max_retries):
        try:
            logger.debug(f"POST {url} (attempt {attempt + 1}/{max_retries})")
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)

            if resp.status_code == 429:
                retry_after = retry_delay
                try:
                    retry_after = int(resp.headers.get("Retry-After", retry_after))
                except (ValueError, TypeError):
                    pass
                logger.warning(f"Rate limited (429), waiting {retry_after}s before retry")
                time.sleep(retry_after)
                continue

            resp.raise_for_status()
            return resp

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout on attempt {attempt + 1}/{max_retries}")
        except requests.exceptions.ConnectionError:
            logger.warning(f"Connection error on attempt {attempt + 1}/{max_retries}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            return None

        if attempt < max_retries - 1:
            wait_time = retry_delay * (2 ** attempt)
            logger.info(f"Retrying in {wait_time}s...")
            time.sleep(wait_time)

    logger.error(f"Failed after {max_retries} attempts")
    return None


# ---------- Content fetching ----------

def fetch_text_from_url(url: str, timeout: int = HTTP_TIMEOUT) -> str:
    """Fetch and extract text from URL."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FactsBot/1.0)"}
    resp = http_get_with_retries(url, timeout=timeout, headers=headers)
    if resp is None:
        logger.error(f"Failed to fetch {url}: no response")
        return ""

    try:
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"HTTP error fetching {url}: {e}, body={resp.text[:300]}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    text = "\n\n".join(paragraphs).strip()
    return text[:MAX_ARTICLE_CHARS]


# ---------- Prompt ----------

def build_prompt(article_text: str, topic_name: str) -> str:
    return f"""Ты редактор русскоязычного Telegram-канала "Любопытные факты".

СЕГОДНЯШНЯЯ ТЕМА: {topic_name}

Тебе дают исходный текст (статья, заметка, новость).

ЗАДАЧА:
1. Найди 1–2 малоизвестных или неочевидных факта по теме "{topic_name}".
2. Сформируй один связный пост в формате:

Заголовок: цепляющий, до 10–12 слов.
Факт: 1–3 предложения, конкретная ситуация/наблюдение/результат.
Почему это важно: 2–4 предложения с объяснением без воды.
Что взять себе: 1–2 предложения, практический вывод или мысль.
Хук в конце: один короткий вопрос читателю.

ТРЕБОВАНИЯ:
- Пиши по-русски.
- Не копируй дословно текст источника, переформулируй.
- Без политики, цензуры, VPN, конфликтов, войн.
- Темы ок: психология, поведение, привычки, история, культура, быт, природа.
- Не используй HTML-теги, только обычный текст (без <b>, <i> и т.п.).
- Свой тон, ирония, примеры из жизни — сделай факт живым и запоминающимся.

Исходный текст:
{article_text}
"""


# ---------- AI API ----------

def call_ai_api(api_cfg: Dict[str, Any], prompt: str) -> str:
    """Call AI API (OpenAI-compatible chat completions)."""
    try:
        url = api_cfg["url"]
        api_key = api_cfg["api_key"]
        model = api_cfg["model"]
    except KeyError as e:
        logger.error(f"AI config is missing key: {e}")
        return ""

    max_tokens = api_cfg.get("max_tokens", 900)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }

    logger.info(f"Calling AI API: {url}")
    resp = http_post_with_retries(url, payload, headers, timeout=60)
    if resp is None:
        logger.error("AI API request failed: no response")
        return ""

    try:
        data = resp.json()
    except json.JSONDecodeError:
        logger.error(f"AI API returned invalid JSON: {resp.text[:500]}")
        return ""

    if isinstance(data, dict) and "choices" in data and data["choices"]:
        choice = data["choices"][0]
        if isinstance(choice, dict):
            if "message" in choice and isinstance(choice["message"], dict):
                content = choice["message"].get("content", "")
                if isinstance(content, str):
                    return content.strip()
            if "text" in choice and isinstance(choice["text"], str):
                return choice["text"].strip()

    logger.error(f"Unexpected AI API response structure: {json.dumps(data)[:500]}")
    return ""


# ---------- Telegram ----------

def send_to_telegram(text: str, url: str, cfg: Dict[str, Any]) -> bool:
    """Send post to Telegram channel."""
    tg_cfg = cfg.get("telegram", {}) or {}
    bot_token = tg_cfg.get("bot_token", "").strip()
    chat_id = tg_cfg.get("chat_id", "").strip()

    if not bot_token or not chat_id:
        logger.warning("Telegram config incomplete (bot_token or chat_id empty), skipping send")
        return False

    base_text = text.strip()
    message = f"{base_text}\n\n🔗 Источник: {url}"
    message = trim_for_telegram_strict(message)
    safe_message = escape_html_for_telegram(message)

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": safe_message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": False,
    }

    logger.info(f"Sending message to Telegram chat {chat_id} (length: {len(message)})")
    resp = http_post_with_retries(api_url, payload, headers={}, timeout=20)
    if resp is None:
        logger.error("Telegram sendMessage failed: no response after retries")
        return False

    try:
        resp.raise_for_status()
        logger.info(f"Sent to Telegram channel {chat_id}")
        return True
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP Error sending to Telegram: {e}, response: {resp.text[:500]}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending to Telegram: {e}")
        return False


# ---------- Persistence ----------

def save_post(text: str, url: str, topic_name: str, cfg: Dict[str, Any]) -> str:
    output_dir = cfg.get("output", {}).get("directory", "output/posts")
    os.makedirs(output_dir, exist_ok=True)

    now = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{output_dir}/{now}.md"

    message = trim_for_telegram(text.strip())
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# {topic_name}\n\n")
        f.write(message)
        f.write(f"\n\n🔗 Источник: {url}\n")

    logger.info(f"Saved post to {filename}")
    return filename


# ---------- Links ----------

def load_links(path: str = LINKS_PATH) -> Dict[str, List[str]]:
    if not os.path.exists(path):
        logger.error(f"{path} not found")
        sys.exit(1)

    links_by_topic = {topic["id"]: [] for topic in TOPICS}
    current_topic = None

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("[") and line.endswith("]"):
                topic_id = line[1:-1].lower()
                if topic_id in links_by_topic:
                    current_topic = topic_id
                    logger.info(f"Found topic section: {topic_id}")
                continue

            if current_topic and line.startswith("http"):
                links_by_topic[current_topic].append(line)

    return links_by_topic


# ---------- Topic rotation ----------

def get_current_topic_index(cfg: Dict[str, Any]) -> int:
    return cfg.get("current_topic_index", 0)


def save_config(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, default_flow_style=False)


def set_next_topic_index(cfg: Dict[str, Any]) -> None:
    current = get_current_topic_index(cfg)
    next_index = (current + 1) % len(TOPICS)
    cfg["current_topic_index"] = next_index
    save_config(cfg)
    logger.info(f"Topic rotated: {TOPICS[current]['name']} → {TOPICS[next_index]['name']}")


# ---------- Main ----------

def main() -> None:
    logger.info("Starting Facts Generator...")

    cfg = load_config()
    ai_cfg = cfg["ai"]

    topic_index = get_current_topic_index(cfg)
    current_topic = TOPICS[topic_index]
    topic_id = current_topic["id"]
    topic_name = current_topic["name"]

    logger.info(f"Current topic: {topic_name} (index {topic_index})")

    links_by_topic = load_links()
    links = links_by_topic.get(topic_id, [])

    if not links:
        logger.warning(f"No links found for topic '{topic_id}'. Skipping.")
        set_next_topic_index(cfg)
        return

    logger.info(f"Found {len(links)} links for topic '{topic_id}'")

    success_count = 0
    skipped_short_source = 0
    skipped_ai_short = 0

    url = links[0]
    logger.info(f"Processing: {url}")

    article_text = fetch_text_from_url(url)
    if not article_text or len(article_text) < 100:
        logger.warning(f"Skipping {url} - too short or empty source text")
        skipped_short_source += 1
    else:
        prompt = build_prompt(article_text, topic_name)
        ai_answer = call_ai_api(ai_cfg, prompt)

        if not ai_answer or len(ai_answer) < 100:
            logger.warning(f"Skipping {url} - AI returned too short response")
            skipped_ai_short += 1
        else:
            save_post(ai_answer, url, topic_name, cfg)
            send_to_telegram(ai_answer, url, cfg)
            success_count += 1

    set_next_topic_index(cfg)

    logger.info(
        f"Completed. Generated {success_count} posts. "
        f"Skipped (short source)={skipped_short_source}, (short AI)={skipped_ai_short}"
    )


if __name__ == "__main__":
    main()

