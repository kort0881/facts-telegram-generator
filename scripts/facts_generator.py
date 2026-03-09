import os
import sys
import json
import time
import datetime
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

import requests
import yaml
from bs4 import BeautifulSoup
import html

# ==================== Константы ====================
CONFIG_PATH = "config.yaml"
TELEGRAM_MAX_LENGTH = 4096  # Лимит сообщения в Telegram
MAX_RETRIES = 3
RETRY_DELAY = 2  # секунды

# ==================== Логирование ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8", mode="a")
    ]
)
logger = logging.getLogger(__name__)


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
        cfg["ai"].get("model", "llama-3.3-70b-versatile")
    ).strip()

    cfg["ai"]["url"] = cfg["ai"].get(
        "url",
        "https://api.groq.com/openai/v1/chat/completions"
    ).strip()

    # Telegram section
    cfg.setdefault("telegram", {})
    cfg["telegram"]["bot_token"] = os.environ.get("TG_BOT_TOKEN", "").strip()
    cfg["telegram"]["chat_id"] = os.environ.get("TG_CHAT_ID", "").strip()
    
    # Валидация обязательных полей
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
    
    logger.info("Configuration loaded successfully")
    return cfg


# ==================== Утилиты ====================
def trim_for_telegram(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> str:
    """Trim text to fit Telegram message limit, trying to break at sentence/word boundary."""
    if len(text) <= max_length:
        return text
    
    # Пробуем обрезать на последнем предложении
    truncated = text[:max_length - 3]
    last_period = truncated.rfind('.')
    last_newline = truncated.rfind('\n')
    last_space = truncated.rfind(' ')
    
    cut_pos = max(last_period, last_newline, last_space)
    if cut_pos > max_length * 0.7:  # Если нашли адекватную точку обрезки
        return text[:cut_pos + 1].strip() + "..."
    
    # Иначе просто обрезаем
    return truncated.strip() + "..."


def http_post_with_retries(
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout: int = 20,
    max_retries: int = MAX_RETRIES,
    retry_delay: int = RETRY_DELAY
) -> Optional[requests.Response]:
    """HTTP POST с повторными попытками и обработкой rate limits."""
    for attempt in range(max_retries):
        try:
            logger.debug(f"POST {url} (attempt {attempt + 1}/{max_retries})")
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            
            # Обработка rate limit (429)
            if resp.status_code == 429:
                retry_after = retry_delay
                try:
                    retry_after = int(resp.headers.get("Retry-After", retry_after))
                except (ValueError, TypeError):
                    pass
                logger.warning(f"Rate limited (429), waiting {retry_after}s before retry")
                time.sleep(retry_after)
                continue
            
            # Другие ошибки 4xx/5xx — не повторяем, возвращаем ответ для обработки
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
            wait_time = retry_delay * (2 ** attempt)  # exponential backoff
            logger.info(f"Retrying in {wait_time}s...")
            time.sleep(wait_time)
    
    logger.error(f"Failed after {max_retries} attempts")
    return None


def escape_html_for_telegram(text: str) -> str:
    """
    Безопасно экранирует текст для Telegram с parse_mode='HTML'.
    Экранирует только &, <, > — теги форматирования обрабатываются отдельно.
    """
    # Telegram HTML mode: экранируем &, <, >
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


# ==================== Telegram ====================
def send_to_telegram(text: str, url: str, cfg: Dict[str, Any]) -> bool:
    """
    Send post to Telegram channel.
    Returns True on success, False on failure.
    """
    tg_cfg = cfg.get("telegram", {}) or {}
    bot_token = tg_cfg.get("bot_token", "").strip()
    chat_id = tg_cfg.get("chat_id", "").strip()

    if not bot_token or not chat_id:
        logger.warning("Telegram config incomplete (bot_token or chat_id empty), skipping send")
        return False

    base_text = text.strip()
    message = f"{base_text}\n\n🔗 Источник: {url}"
    message = trim_for_telegram(message)
    
    # Экранируем для HTML-режима Telegram
    safe_message = escape_html_for_telegram(message)

    # ✅ Исправлен URL: убраны лишние пробелы
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
        logger.info(f"✅ Sent to Telegram channel {chat_id}")
        return True
    except requests.exceptions.HTTPError as e:
        error_body = resp.text[:500] if resp else "no body"
        logger.error(f"❌ HTTP Error sending to Telegram: {e}, response: {error_body}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error sending to Telegram: {e}")
        return False


# ==================== Точка входа (пример) ====================
if __name__ == "__main__":
    logger.info("🚀 Starting bot...")
    
    try:
        config = load_config()
        logger.info(f"Using model: {config['ai']['model']}")
        
        # Пример отправки
        test_text = "Тестовое сообщение от бота!\nЭто проверка работы <b>HTML</b> форматирования & спецсимволов."
        test_url = "https://example.com/article"
        
        success = send_to_telegram(test_text, test_url, config)
        if success:
            logger.info("✅ Test message sent successfully")
        else:
            logger.error("❌ Failed to send test message")
            
    except KeyboardInterrupt:
        logger.info("👋 Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"💥 Unhandled exception: {e}")
        sys.exit(1)
