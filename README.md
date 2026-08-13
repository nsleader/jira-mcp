# jira-mcp

MCP-сервер для доступа к **Jira Server / Data Center** (и, опционально, к
**Confluence Server / DC**) через
[`atlassian-python-api`](https://github.com/atlassian-api/atlassian-python-api).
Аутентификация — Personal Access Token (Bearer). Транспорт — stdio.

## Требования

- Python ≥ 3.11 (в разработке используется 3.13)
- [`uv`](https://docs.astral.sh/uv/)

## Установка

Сервер ставится и запускается прямо из репозитория через `uvx` — клонировать
исходники и указывать путь до них не нужно. `uvx` скачает пакет, соберёт его в
изолированном окружении и запустит точку входа:

```bash
uvx --from git+https://github.com/nsleader/jira-mcp.git jira-mcp
```

Чтобы зафиксировать версию, можно указать тег или коммит:

```bash
uvx --from git+https://github.com/nsleader/jira-mcp.git@v0.1.0 jira-mcp
```

## Конфигурация

Креды передаются как **переменные окружения в конфигурации подключения MCP-сервера**
(блок `env`), а не через `.env` файл.

| Переменная            | Обязательна | По умолчанию | Описание                                  |
|-----------------------|-------------|--------------|-------------------------------------------|
| `JIRA_URL`            | да          | —            | Базовый URL Jira, напр. `https://jira.example.com` |
| `JIRA_PERSONAL_TOKEN` | да          | —            | Personal Access Token (Bearer)            |
| `JIRA_SSL_VERIFY`     | нет         | `true`       | Проверка TLS-сертификата                  |
| `JIRA_TIMEOUT`        | нет         | `75`         | Таймаут запроса, сек                      |
| `CONFLUENCE_URL`      | для Confluence | —         | Базовый URL Confluence, напр. `https://wiki.example.com` |
| `CONFLUENCE_PERSONAL_TOKEN` | нет   | `JIRA_PERSONAL_TOKEN` | Отдельный PAT Confluence            |
| `CONFLUENCE_SSL_VERIFY` | нет       | как у Jira   | Проверка TLS-сертификата                  |
| `CONFLUENCE_TIMEOUT`  | нет         | как у Jira   | Таймаут запроса, сек                      |

PAT создаётся в Jira: **Profile → Personal Access Tokens**; в Confluence — там же
в своём профиле. Confluence — отдельное приложение со своим адресом и, как
правило, своим токеном, поэтому `CONFLUENCE_URL` из `JIRA_URL` не выводится.
Без него сервер работает как обычно, а confluence-инструменты возвращают
понятную ошибку о ненастроенном подключении.

## Запуск

Сервер общается по stdio и запускается MCP-клиентом. Для локальной проверки можно
задать переменные вручную:

```bash
JIRA_URL=https://jira.example.com JIRA_PERSONAL_TOKEN=xxxx \
  uvx --from git+https://github.com/nsleader/jira-mcp.git jira-mcp
```

## Подключение к Claude Code

```bash
claude mcp add jira \
  -e JIRA_URL=https://jira.example.com \
  -e JIRA_PERSONAL_TOKEN=your-personal-access-token \
  -- uvx --from git+https://github.com/nsleader/jira-mcp.git jira-mcp
```

Чтобы включить инструменты Confluence, добавьте туда же
`-e CONFLUENCE_URL=https://wiki.example.com` и (если токен отдельный)
`-e CONFLUENCE_PERSONAL_TOKEN=your-confluence-token`.

## Подключение к Claude Desktop

В `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jira": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/nsleader/jira-mcp.git", "jira-mcp"],
      "env": {
        "JIRA_URL": "https://jira.example.com",
        "JIRA_PERSONAL_TOKEN": "your-personal-access-token"
      }
    }
  }
}
```

## Доступные инструменты

| Инструмент             | Описание                                                         |
|------------------------|------------------------------------------------------------------|
| `get_issue`            | Получить задачу по ключу (`PROJ-123`)                            |
| `search_issues`        | Поиск задач по JQL                                               |
| `add_comment`          | Добавить комментарий к задаче                                    |
| `get_issue_types`      | Типы задач инстанса или конкретного проекта (что можно передать в `create_issue`) |
| `create_issue`         | Создать эпик / задачу / подзадачу — **вызывается строго по явной просьбе пользователя** |
| `add_worklog`          | Залогировать время на задачу — **вызывается строго по явной просьбе пользователя** |
| `get_all_projects`     | Список всех проектов, видимых пользователю                        |
| `get_project`          | Один проект по ключу или id                                      |
| `get_project_roles`    | Роли проекта и их состав (пользователи и группы) — где искать реального РП |
| `get_user_worklog`     | Часы одного пользователя за период (Tempo Timesheets)           |
| `get_users_worklog`    | Часы по списку пользователей за период, с разбивкой по каждому  |
| `get_team_worklog`     | Часы всех участников команды Tempo (`teamId`) за период — отчёт «Logged Time» |
| `get_all_teams`        | Список всех команд Tempo (id, название, лид)                     |
| `find_team`            | Найти команды Tempo по названию (подстрока) → получить `teamId`  |
| `get_team_members`     | Все участники команды Tempo (ключ, имя, роль, период членства)   |
| `get_user_plan`        | Планы Tempo Planner: на какие проекты и на сколько часов в день запланирован пользователь |
| `get_users_plan`       | Планы Tempo Planner по списку пользователей за один запрос, с разбивкой по каждому |
| `search_confluence`    | Поиск страниц Confluence: текст, пространство, метка (CQL под капотом) |
| `get_confluence_page`  | Прочитать страницу по id / ссылке / `space` + `title` — тело в Markdown |
| `get_confluence_page_children` | Дочерние страницы — обход дерева пространства вниз          |
| `get_confluence_spaces` | Список пространств Confluence (ключи для поиска)                 |

## Структура

```
src/jira_mcp/
├── config.py        # настройки из окружения (pydantic-settings)
├── client.py        # фабрики Jira- и Confluence-клиентов (кеш)
├── server.py        # FastMCP + main()
└── tools/
    ├── issues.py    # инструменты по задачам + create_issue
    ├── projects.py  # проекты и роли проекта (get_project_roles)
    ├── worklogs.py  # отчёты по времени (Tempo Timesheets) + add_worklog
    ├── tempo.py     # Tempo Planner: планирование (get_user_plan)
    └── confluence.py # поиск и чтение статей Confluence
```

## Создание задач

`create_issue` создаёт issue через `rest/api/2/issue` от имени владельца токена.
Иерархия Jira Server/DC не лежит в обычных полях, поэтому сервер сам разбирается
с agile-полями Greenhopper (их id различаются от инстанса к инстансу и находятся
по типу схемы в `rest/api/2/field`):

- `issue_type="Epic"` → в обязательное поле **Epic Name** пишется `epic_name`,
  а если он не задан — `summary`
- обычный тип + `parent="PROJ-1"` → ключ эпика пишется в **Epic Link**
- подзадача (`subtask: true`) + `parent="PROJ-2"` → поле `parent`; без `parent`
  вызов отклоняется до похода в Jira

Тип задачи резолвится по имени (регистронезависимо) или id заранее: если такого
типа нет, ошибка перечисляет доступные, а не возвращает голый HTTP 400. Имена
типов на инстансах переименовывают и локализуют — при сомнениях сначала
`get_issue_types(project_key)`.

Остальные поля — `description`, `assignee` (логин Server/DC), `priority`,
`labels`, `components`, `due_date`; всё, что не покрыто сигнатурой (story points,
обязательные кастомные поля), передаётся через `extra_fields`, например
`{"customfield_10004": 3}`.

Как и `add_worklog`, инструмент пишущий и в описании помечен как вызываемый
**строго по просьбе пользователя**: удалить созданную задачу сервер не умеет.

## Логирование времени

`add_worklog` пишет worklog через core-API Jira
(`rest/api/2/issue/{key}/worklog`) от имени владельца токена; Tempo на Server/DC
читает те же worklog'и, поэтому списанное время видно в отчётах выше.

- `time_spent` — синтаксис Jira: `2h`, `90m`, `2h 30m`, `1d 4h`
- `started` — `YYYY-MM-DD` (привязывается к 09:00 локального времени, чтобы
  worklog не уехал на предыдущий день в восточных таймзонах), `YYYY-MM-DDTHH:MM`
  или метка времени со смещением; по умолчанию — сейчас

В описании инструмента явно указано, что он вызывается **строго по просьбе
пользователя**: это единственный инструмент, пишущий время, и отменить запись
из сервера нельзя.

## Tempo Planner

`get_user_plan` читает раздел планирования через `rest/tempo-planning/1/allocation`
и рабочий календарь пользователя (`rest/tempo-core/1/user/schedule`), поэтому
разбивка по дням не учитывает выходные и праздники — так же, как сам Tempo.
На Server/DC это плагин Jira: работает тот же `JIRA_URL` и PAT, отдельный
Tempo-токен не нужен.

## Confluence

Четыре инструмента только на чтение: найти статью и прочитать её.

**Поиск.** `search_confluence` собирает CQL из обычных аргументов —
`query` (полнотекстовый `text ~`), `space`, `label`, `content_type` — и по
умолчанию сортирует по релевантности; `newest_first=true` переключает на
«что менялось недавно». Для запросов, которые сигнатурой не выразить
(`creator = jsmith AND lastmodified > -7d`), есть параметр `cql` — он идёт на
сервер как есть. Маркеры подсветки совпадений (`@@@hl@@@`), которые Server
подмешивает в заголовок и excerpt, вычищаются. В ответе `total` — общее число
совпадений, а не размер выданной страницы результатов.

**Чтение.** `get_confluence_page` принимает id, любую ссылку на страницу
(`?pageId=123`, `/pages/123/Title`, `/display/SPACE/Title`) либо пару
`space` + `title`. Тело берётся из отрендеренного `body.view` (макросы уже
раскрыты) и конвертируется в Markdown: заголовки, списки, таблицы и блоки кода
сохраняются, разметка макросов — нет; относительные ссылки достраиваются до
абсолютных. Другие варианты — `format="text"`, `"html"` и `"storage"`
(исходный XHTML, нужен только чтобы где-то отредактировать страницу).

Длинные статьи режутся по `max_chars` (по умолчанию 20000), чтобы не забивать
контекст: в ответе есть `content_chars`, `truncated` и `next_offset` —
следующий кусок читается тем же инструментом с `offset`.

Рядом с текстом возвращается контекст статьи: пространство, версия, кто и когда
менял, метки и хлебные крошки `ancestors`. Вниз по дереву —
`get_confluence_page_children`; ключи пространств — `get_confluence_spaces`
(персональные `~user` скрыты, если не попросить `include_personal`).

## Разработка

```bash
uv run ruff check
uv run pytest
```
