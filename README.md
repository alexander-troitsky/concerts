# Концертный бот

Раз в неделю ищет концерты выбранных исполнителей в зонах Барселона / Санкт-Петербург / Москва и присылает в Telegram только новые анонсы.

## Запуск на Railway

1. **Токен.** В Telegram: @BotFather → `/newbot` → скопировать токен.
2. **GitHub.** Создать приватный репозиторий и загрузить туда файлы: `bot.py`, `requirements.txt`, `Procfile`, `.python-version`, `.gitignore`.
3. **Railway.** New Project → Deploy from GitHub repo → выбрать репозиторий.
4. **Переменные** (вкладка Variables):
   - `BOT_TOKEN` — токен от BotFather
   - `DATA_DIR` = `/data`
   - по желанию: `TM_API_KEY` (Ticketmaster), `CHECK_WEEKDAY` (1 = пн … 7 = вс, по умолчанию 1), `CHECK_HOUR` (по умолчанию 10, время Мадрида)
5. **Том для базы.** В сервисе: правый клик → Attach Volume, mount path `/data`. Без тома список исполнителей и история сбросятся при каждом редеплое.
6. Дождаться деплоя, написать боту `/start`. **Первый, кто напишет `/start`, становится владельцем**, остальных бот игнорирует.
7. Выполнить `/test`: видно, какие источники читаются. Потом `/check`: придёт всё, что уже объявлено.

## Команды
`/list`, `/add Имя; вариант; вариант`, `/remove Имя`, `/sources`, `/addsource venue|artist|list URL [Город|Имя]`, `/delsource N`, `/test`, `/check`

## Ticketmaster (необязательно)
Бесплатный ключ: developer.ticketmaster.com → зарегистрироваться → My Apps → Consumer Key. Положить в `TM_API_KEY`.
