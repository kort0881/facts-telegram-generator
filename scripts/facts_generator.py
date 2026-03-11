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
- Updated prompt with concrete fact, practical вывод and examples
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
Ты пишешь пост для Telegram-канала «Что ты не знал».

Формат поста (строго соблюдай структуру и пустые строки):

1 строка — короткий цепляющий заголовок + 1 эмодзи.

1 блок (2–3 предложения) — один конкретный факт из текста:
- цифра, пример, эксперимент, находка, цитата или яркий случай;
- без общих фраз уровня «это важно для нашего благополучия».

пустая строка

2 блок (2–3 предложения) — простое объяснение:
- почему это важно, что это меняет в понимании темы;
- можно сравнить «как было раньше» и «что показывают новые данные».

пустая строка

3 блок (1–2 предложения) — практический вывод:
- в формате мини-инструкции: начни делать X / обрати внимание на Y / перестань делать Z;
- никаких общих абстракций, только то, что читатель может применить в жизни или в мышлении.

пустая строка

Финал — вопрос читателю:
- вопрос должен цеплять личный опыт или позицию («как ты к этому относишься?», «было ли у тебя так?», «что бы ты выбрал?»);
- избегай общих вопросов вроде «что вы думаете по этому поводу?».

Последняя строка — 3–4 хэштега на русском:
- сначала тема (#психология, #история, #наука, #политика), потом уточнения;
- не дублируй одно и то же слово в разных формах.

Стиль:
- Пиши коротко, живо, разговорным русским, без канцелярита.
- В каждом посте должен быть ОДИН главный факт и ОДНА понятная мысль, вокруг которой построен текст.
- Допустимая длина всего поста (без источника) — максимум 900 символов.
- Не используй шаблонные фразы: «это важно для нашего благополучия», «мы можем глубже понять эпоху», «это поднимает важные вопросы» и подобные общие формулировки.

Вот несколько примеров правильных постов. Следуй их стилю и структуре, но не копируй текст:

Пример 1 (палеонтология):
Что ты не знал: Рапторы-«команды охотников» 🦖
Utahraptor был почти с современного медведя ростом и охотился не в одиночку, а стаей: часть хищников отвлекала добычу спереди, пока другие нападали с флангов. Такая координация делала их опаснее многих более крупных динозавров.

Учёные считают, что именно командная работа и базовый «интеллект стаи» позволяли им брать жертв, которые в одиночку были бы им не по зубам. Размер и сила не всегда решают, когда против тебя хорошо слаженная группа.

Вывод простой: даже в доисторическом мире выигрывали не самые большие, а те, кто умел действовать синхронно.

Представь, ты один в лесу и слышишь шорохи сразу с трёх сторон — как бы ты себя повёл?
#история #доистория #динозавры #наука

Пример 2 (искусство):
Что ты не знал: Потайной художник королевы 🎨
Историки нашли неизвестные миниатюры Николаса Хиллиарда — любимого художника Елизаветы I, спрятанные в частных коллекциях. Новые работы показывают менее парадный образ двора: больше личных деталей, эмоций и небольших визуальных намёков.

Такие находки меняют наше представление о «замороженной» эпохе: за официальными портретами вдруг появляется живая, неидеальная реальность. История становится не набором дат, а чужой личной жизнью, в которую нас тихо впускают.

Вывод: каждый новый артефакт способен перевернуть привычную картинку прошлого, даже если кажется мелочью.

Тебе интереснее смотреть на идеально вылизанные парадные портреты или на честные, «закулисные» наброски?
#искусство #история #ЕлизаветаI #живопись

Пример 3 (религия и суд):
Что ты не знал: Когда вера приходит в суд ⚖️
В США при обсуждении кандидатов в высший суд всё чаще спрашивают, как их личная религия влияет на решения по абортам, ЛГБТ и свободе слова. Некоторые судьи прямо писали статьи о том, как совместить религиозные убеждения и требования закона.

От их ответов зависит, останутся ли у миллионов людей уже принятые права или их тихо пересмотрят. Для общества это не абстрактный спор, а очень конкретные последствия: кто имеет право на что в реальной жизни.

Практический вывод: когда ты смотришь новости о назначениях судей или чиновников, важно обращать внимание не только на партийность, но и на их ценности и тексты, которые они писали раньше.

Как ты думаешь, судья может быть глубоко верующим и при этом действительно отделять свои убеждения от решений?
#религия #право #суд #общество

Пример 4 (психология и дружба):
Что ты не знал: Друзья против старения 💚
Исследования показывают, что у людей с близкими друзьями ниже уровень хронического стресса и медленнее «изнашиваются» клетки — это видно даже по длине теломер в анализах. Обычные разговоры и чувство, что тебя поддержат, снижают воспаление не хуже некоторых таблеток.

Получается, что вечерний созвон или встреча с другом — это не «просто поболтать», а реальная инвестиция в здоровье и психику. Социальная изоляция, наоборот, связана с более высоким риском депрессии и ранних болезней.

Вывод: забота о себе — это не только спорт и питание, но и регулярные живые контакты с людьми, которые тебе не безразличны.

Когда ты в последний раз первым написал другу просто так, без повода?
#психология #дружба #здоровье #наука

Твоя задача: по исходному тексту ниже придумать НОВЫЙ пост в том же стиле и структуре, с ОДНИМ главным фактом, понятным объяснением и практическим выводом.

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

