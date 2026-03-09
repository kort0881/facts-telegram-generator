# facts-telegram-generator

Auto-generator of Telegram fact posts via Groq + GitHub Actions.

## Описание

**Канал:** "Что ты не знал"

Автоматизированный бот для генерации и публикации интересных неочевидных фактов в Telegram-канал. Использует AI (Groq/OpenRouter/OpenAI-совместимые) для создания уникального контента с ротацией тем.

## Возможности

✅ **Автоматическая генерация контента** - AI создаёт оригинальные посты из источников  
✅ **Ротация тем** - Посты чередуются по темам (привычки, память, история, законы)  
✅ **Интеграция с Telegram** - Прямая публикация в канал via Bot API  
✅ **GitHub Actions** - Ежедневное расписание (06:00 UTC)  
✅ **Обработка ошибок** - Retry-логика для сетевых сбоев и лимитов API  
✅ **Управление конфигом** - Сохранение индекса темы для ротации  

## Структура проекта

```
facts-telegram-generator/
├── scripts/
│   └── facts_generator.py    # Основной скрипт генерации
├── .github/workflows/
│   └── facts.yml             # GitHub Actions workflow (ежедневно)
├── config.yaml.example       # Шаблон конфигурации
├── links.txt                 # Источники, сгруппированные по темам
├── requirements.txt          # Зависимости Python
└── README.md                 # Этот файл
```

## Установка и конфигурация

### 1. Клонирование репозитория

```bash
git clone https://github.com/kort0881/facts-telegram-generator.git
cd facts-telegram-generator
```

### 2. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 3. Конфигурация

Скопируйте и отредактируйте конфиг:

```bash
cp config.yaml.example config.yaml
```

**config.yaml** должен содержать:

```yaml
ai:
  url: "https://api.groq.com/openai/v1/chat/completions"
  api_key: "YOUR_GROQ_API_KEY"
  model: "mixtral-8x7b-32768"
  max_tokens: 900

telegram:
  bot_token: "YOUR_BOT_TOKEN"
  chat_id: "YOUR_CHANNEL_ID"

output:
  directory: "output/posts"

current_topic_index: 0  # Начальный индекс темы (обновляется автоматически)
```

### 4. Подготовка источников

Файл **links.txt** содержит URL источников, сгруппированные по темам:

```
[habits]
https://jamesclear.com/atomic-habits
https://www.scientificamerican.com/article/how-to-build-new-habits/
...

[memory]
https://www.nature.com/subjects/human-behaviour
...

[history]
https://www.smithsonianmag.com/...
...

[laws]
https://en.wikipedia.org/wiki/List_of_unusual_laws
...
```

## Использование

### Локальный запуск

```bash
python scripts/facts_generator.py
```

Скрипт:
1. Загружает конфиг и текущий индекс темы
2. Выбирает случайный URL из текущей темы
3. Извлекает текст из источника
4. Генерирует пост через AI
5. Публикует в Telegram
6. Сохраняет пост в `output/posts/`
7. Ротирует тему для следующего запуска

### Автоматическое расписание (GitHub Actions)

Workflow **`.github/workflows/facts.yml`** запускается каждый день в **06:00 UTC**.

Для использования:
1. Добавьте Secrets в репозитории:
   - `GROQ_API_KEY` (или ваш AI API ключ)
   - `TG_BOT_TOKEN`
   - `TG_CHAT_ID`
2. Workflow автоматически:
   - Генерирует пост
   - Коммитит обновлённый `config.yaml` с новым индексом темы
   - Пушит на `main` ветку

## Темы ротации

Система ротирует посты по темам в таком порядке:

1. **Привычки и поведение** - Психология, привычки, советы по продуктивности
2. **Память и мышление** - Когнитивные процессы, мозг, обучение
3. **История и культура** - Исторические события, культурные факты
4. **Странные законы и нормы** - Необычные законы, социальные явления

## Качество контента

Каждый генерируемый пост должен соответствовать **Правилу 3 источников**:

- ✅ Каждый факт опирается на минимум 2–3 независимых источника
- ✅ Приоритет источникам Tier S/A: Nature, Scientific American, Smithsonian, N+1 и т.д.
- ✅ Неочевидные факты, не поверхностные фактоиды
- ✅ Практические выводы и применение в жизни

### Формат поста

Структура генерируемого поста:

```
[Заголовок 10-12 слов]

[Факт: 1-3 конкретных предложения]

[Почему это важно: 2-4 предложения объяснения]

[Что взять себе: практический вывод]

[❓ Вопрос к читателю]

🔗 Источник: [URL]
```

### Ограничения контента

❌ Политика, войны, конфликты  
❌ VPN и цензура (в контексте обхода)  
❌ Спам и проверенные фейки  
❌ HTML-теги и markdown в текстах от AI  

✅ Психология, поведение, привычки  
✅ История, культура, быт  
✅ Природа, животные, науки  
✅ Экономика, социология, статистика  

## Обработка ошибок

Скрипт включает robust обработку:

- **HTTP Retry Logic** - Повторные попытки при 5xx ошибках и таймаутах
- **Backoff стратегия** - Экспоненциальная задержка между попытками
- **Telegram error handling** - Логирование ошибок отправки
- **AI response validation** - Проверка длины и валидности ответа

## API ключи и переменные

### Groq API
1. Зарегистрируйтесь на https://console.groq.com
2. Создайте API ключ в dashboard
3. Добавьте в `config.yaml` или GitHub Secrets

### Telegram Bot
1. Создайте бота: @BotFather → /newbot
2. Получите токен
3. Создайте приватный канал
4. Добавьте бота администратором канала
5. Получите Chat ID (можно через @userinfobot)

## Логирование

Логи показывают:
- ✓ Загрузку конфига и текущую тему
- ✓ Найденные источники для темы
- ✓ Процесс фетчинга контента
- ✓ Вызовы AI API
- ✓ Результаты публикации в Telegram
- ✓ Ротацию тем

Пример:
```
2024-01-15 06:00:10 - INFO - Starting Facts Generator...
2024-01-15 06:00:10 - INFO - Current topic: Привычки и поведение (index 0)
2024-01-15 06:00:10 - INFO - Found 8 links for topic 'habits'
2024-01-15 06:00:11 - INFO - Calling AI API: https://api.groq.com/openai/v1/chat/completions
2024-01-15 06:00:13 - INFO - Sent to Telegram channel -1001234567890
2024-01-15 06:00:13 - INFO - Topic rotated: Привычки и поведение → Память и мышление
```

## Развертывание на VPS

Для локального запуска на сервере вместо GitHub Actions:

```bash
# Установка
git clone https://github.com/kort0881/facts-telegram-generator.git
cd facts-telegram-generator
pip install -r requirements.txt
cp config.yaml.example config.yaml
# Отредактируйте config.yaml

# Запуск один раз в день через cron
crontab -e
# Добавьте строку:
0 6 * * * cd /path/to/facts-telegram-generator && python scripts/facts_generator.py >> logs/cron.log 2>&1
```

## Расширение

### Добавление новых тем

Отредактируйте `TOPICS` в `scripts/facts_generator.py`:

```python
TOPICS = [
    {"id": "habits", "name": "Привычки и поведение"},
    {"id": "memory", "name": "Память и мышление"},
    {"id": "history", "name": "История и культура"},
    {"id": "laws", "name": "Странные законы и социальные нормы"},
    {"id": "science", "name": "Научные открытия"},  # Новая тема
]
```

И добавьте URL источники в `links.txt`:

```
[science]
https://www.nature.com/...
https://www.scientificamerican.com/...
```

### Смена AI провайдера

Скрипт совместим с OpenAI-совместимыми API:
- Groq
- OpenRouter
- Mistral API
- Local LLM (ollama с OpenAI API эмуляцией)

Просто измените `ai.url`, `ai.api_key`, и `ai.model` в `config.yaml`.

## Лицензия

MIT

## Автор

kort0881

---

**Последнее обновление:** 2024-01-15  
**Статус:** Production-ready ✅
