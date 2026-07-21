# jira-mcp

MCP-сервер для доступа к **Jira Server / Data Center** через
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

PAT создаётся в Jira: **Profile → Personal Access Tokens**.

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
| `get_user_worklog`     | Часы одного пользователя за период (Tempo Timesheets)           |
| `get_users_worklog`    | Часы по списку пользователей за период, с разбивкой по каждому  |
| `get_team_worklog`     | Часы всех участников команды Tempo (`teamId`) за период — отчёт «Logged Time» |
| `get_all_teams`        | Список всех команд Tempo (id, название, лид)                     |
| `find_team`            | Найти команды Tempo по названию (подстрока) → получить `teamId`  |
| `get_team_members`     | Все участники команды Tempo (ключ, имя, роль, период членства)   |

## Структура

```
src/jira_mcp/
├── config.py        # настройки из окружения (pydantic-settings)
├── client.py        # фабрика Jira-клиента (кеш)
├── server.py        # FastMCP + main()
└── tools/
    ├── issues.py    # инструменты по задачам
    └── tempo.py     # заглушка под Tempo Planning API
```

## Расширение под Tempo Planning

Заготовка — `src/jira_mcp/tools/tempo.py`. Tempo использует собственный базовый URL
и API-токен, поэтому интеграция получит отдельный HTTP-клиент и свои настройки.

## Разработка

```bash
uv run ruff check
uv run pytest
```
