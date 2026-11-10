# websearch-mcp

Локальный MCP-сервер веб-поиска. Без ключей, без платных API, без Docker.
Работает на Windows, macOS и Linux.

Ротация бесплатных движков (DuckDuckGo → Mojeek → Brave → публичные SearxNG),
чтение страниц в markdown с сохранением блоков кода, обход документации в
пределах одного домена.

## Установка

Нужны Python 3.12+ и [uv](https://docs.astral.sh/uv/).

```bash
cd <путь-до-проекта>
uv sync
```

`lxml` даёт ускорение парсинга страниц, но не обязателен: если на вашей
платформе он не установился, сервер сам переключится на чисто питоновский
`html.parser`.

## Регистрация в Claude Code

Подставьте свой путь к проекту. Примеры для трёх платформ:

**Windows** (PowerShell):

```powershell
claude mcp add websearch -- uv run --project C:\path\to\websearch-mcp python -m websearch_mcp.server
```

**macOS / Linux**:

```bash
claude mcp add websearch -- uv run --project /path/to/websearch-mcp python -m websearch_mcp.server
```

Если в каталоге проекта уже есть `.mcp.json`, можно просто скопировать
`.mcp.json.example` в `.mcp.json` и вписать свой путь — тогда регистрация
не нужна.

## Инструменты MCP

| Инструмент | Что делает |
|---|---|
| `web_search(query, max_results)` | Поиск по вебу: заголовок, URL, сниппет |
| `fetch_page(url)` | Страница чистым markdown |
| `crawl_docs(start_url, depth, max_pages)` | Обход документации в пределах домена |
| `search_and_read(query, max_pages)` | Поиск + чтение топов одним вызовом |
| `cache_stats()` / `cache_clear()` | Статистика и очистка кэша |

## Команды CLI

```bash
websearch search "httpx timeout config"
websearch fetch  https://example.com/docs
websearch crawl  https://example.com/docs --depth 2
websearch read   "httpx timeout config" --pages 3
websearch doctor          # какие движки живы прямо сейчас
websearch cache stats
websearch cache prune     # удалить только протухшее
websearch cache clear
```

## Ограничения нагрузки

Сервер рассчитан на локальный запуск и не должен ни висеть, ни расти в памяти:

| Ограничение | Значение | Зачем |
|---|---|---|
| Бюджет на один поиск | 30 с | цепочка из 7 хостов с backoff могла крутиться ~6 минут |
| Размер скачиваемой страницы | 8 MB | BeautifulSoup раздувает HTML в памяти ~20× |
| Суммарный объём `crawl_docs` | 300 тыс. знаков | иначе ответ не влезает в контекст клиента |
| Одна страница в markdown | 60 тыс. знаков | то же |

Превысившая лимит страница пропускается с пояснением, а не роняет вызов.

## Поведение при блокировке

Сервер определяет блокировку по содержимому ответа (HTTP 202, слова `challenge` /
`unusual traffic`, ноль распарсенных результатов), а не по коду ответа. При
блокировке — переход на следующий движок с экспоненциальной паузой и постоянным
набором cookie для каждого движка.

Если все движки отказали или кончился бюджет времени, возвращается **честная
ошибка** с причиной по каждому движку, а не выдуманные результаты.

## Кэш

Дисковый, TTL: поиск — 6 часов, страницы — 24 часа. Повторный запрос идёт из
кэша, в сеть не выходит. Протухшие записи вычищаются автоматически раз в
32 записи; вручную — `websearch cache prune`.

Расположение по платформам:

| ОС | Путь |
|---|---|
| Windows | `%LOCALAPPDATA%\websearch-mcp\cache` |
| macOS | `~/Library/Caches/websearch-mcp` |
| Linux | `$XDG_CACHE_HOME/websearch-mcp` или `~/.cache/websearch-mcp` |

Точный путь показывает `websearch cache stats`.

## Тесты

```bash
uv run pytest
```
