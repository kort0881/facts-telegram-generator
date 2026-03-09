#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Facts Telegram Generator
Auto-generates Telegram fact posts via AI (Groq/OpenRouter) + GitHub Actions
"""

import os
import sys
import json
import datetime
import logging
import requests
import yaml
from pathlib import Path
from bs4 import BeautifulSoup

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

CONFIG_PATH = "config.yaml"

def load_config():
    """Load config from YAML file."""
    if not os.path.exists(CONFIG_PATH):
        logger.error(f"{CONFIG_PATH} not found. Copy config.yaml.example to config.yaml")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def fetch_text_from_url(url: str, timeout: int = 20) -> str:
    """Fetch and extract text from URL."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; FactsBot/1.0)"}
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = "\n\n".join(paragraphs)
        return text[:6000]  # Trim to 6000 chars for context window
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return ""

def build_prompt(article_text: str) -> str:
    """Build AI prompt for fact generation."""
    return f"""Ты редактор русскоязычного Telegram-канала "Любопытные факты".

Тебе дают исходный текст (статья, заметка, новость).

ЗАДАЧА:
1. Найди 1–2 малоизвестных или неочевидных факта.
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

Исходный текст:
{article_text}
"""

def call_ai_api(api_cfg: dict, prompt: str) -> str:
    """Call AI API to generate fact post."""
    try:
        url = api_cfg["url"]
        api_key = api_cfg["api_key"]
        model = api_cfg["model"]
        max_tokens = api_cfg.get("max_tokens", 900)
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        # Support for different API formats (Groq, OpenRouter, etc.)
        # Try OpenAI-compatible format
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        
        logger.info(f"Calling AI API: {url}")
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        
        # Extract text from response
        if "choices" in data and len(data["choices"]) > 0:
            if "message" in data["choices"][0]:
                return data["choices"][0]["message"]["content"].strip()
            elif "text" in data["choices"][0]:
                return data["choices"][0]["text"].strip()
        
        logger.error(f"Unexpected API response: {data}")
        return ""
    except Exception as e:
        logger.error(f"Error calling AI API: {e}")
        return ""

def save_post(text: str, url: str, cfg: dict):
    """Save generated post to file."""
    output_dir = cfg.get("output", {}).get("directory", "output/posts")
    os.makedirs(output_dir, exist_ok=True)
    
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{output_dir}/{now}.md"
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(text)
        f.write(f"\n\n🔗 Источник: {url}\n")
    
    logger.info(f"Saved post to {filename}")

def send_to_telegram(text: str, cfg: dict):
    """Send post to Telegram channel."""
    try:
        bot_token = cfg.get("telegram", {}).get("bot_token")
        chat_id = cfg.get("telegram", {}).get("chat_id")
        
        if not bot_token or not chat_id:
            logger.warning("Telegram config not set, skipping sending")
            return
        
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        
        resp = requests.post(url, json=payload, timeout=20)
        resp.raise_for_status()
        logger.info(f"Sent to Telegram channel {chat_id}")
    except Exception as e:
        logger.error(f"Error sending to Telegram: {e}")

def main():
    """Main entrypoint."""
    logger.info("Starting Facts Generator...")
    
    cfg = load_config()
    ai_cfg = cfg["ai"]
    
    # Read links from file
    if not os.path.exists("links.txt"):
        logger.error("links.txt not found")
        sys.exit(1)
    
    with open("links.txt", "r", encoding="utf-8") as f:
        links = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    
    logger.info(f"Found {len(links)} links to process")
    
    success_count = 0
    for url in links:
        logger.info(f"Processing: {url}")
        
        article_text = fetch_text_from_url(url)
        if not article_text or len(article_text) < 100:
            logger.warning(f"Skipping {url} - too short or empty")
            continue
        
        prompt = build_prompt(article_text)
        ai_answer = call_ai_api(ai_cfg, prompt)
        
        if not ai_answer or len(ai_answer) < 100:
            logger.warning(f"Skipping {url} - AI returned too short response")
            continue
        
        save_post(ai_answer, url, cfg)
                send_to_telegram(ai_answer, cfg)
        success_count += 1
    
    logger.info(f"Completed. Generated {success_count} posts.")

if __name__ == "__main__":
    main()
