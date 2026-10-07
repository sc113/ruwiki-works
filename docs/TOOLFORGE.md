# ruwiki works на Toolforge

Инструкция по регистрации и развёртыванию ruwiki works. Команды используют пример имени инструмента и базы; их нужно заменить значениями выбранного окружения. Интеграции ToolsDB, OAuth и записи бота проверяются при развёртывании.

Для проекта нужны один сайт, два непрерывных задания Toolforge Jobs и общая база ToolsDB. Общий исполнитель обрабатывает действия последовательно: ОБКАТ, даты, параметры RQ, разворачивание RQ, переводы через категории, переводы с СО, «Пустой раздел» → «Дополнить раздел» и обратная замена. Монитор ищет правки ОБКАТ каждые 5 минут и проверяет остальные списки каждые 6 часов. Три действия шаблонов ставятся в очередь ежедневно в 03:00 МСК, переводы и замены разделов — в 04:00 МСК. ОБКАТ запускается через 15 минут после последней правки и с форматированием в последний день месяца в 23:30. Расписание хранится в базе, отдельный cron не нужен. Все модули — часть одного инструмента.

## Регистрация и оформление

1. Открыть [регистрацию Toolsadmin](https://toolsadmin.wikimedia.org/register/) и выбрать **Login using Wikimedia account**. Создать связанный developer account: отдельные логин разработчика и UNIX-имя для SSH. Если developer account уже есть, использовать его и связать учётные записи в настройках. [Официальный порядок](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Quickstart).
2. Подать [заявку на членство Toolforge](https://toolsadmin.wikimedia.org/tools/membership/apply). В ней объяснить назначение проекта и перечислить ботозадачи. Репозиторий можно добавить, если он уже опубликован. Дождаться одобрения, затем выйти и снова войти в Toolsadmin.
3. В [списке инструментов Toolsadmin](https://toolsadmin.wikimedia.org/tools/) выбрать **Create new tool**. Предлагаемое техническое имя — `ruwiki-works`, отображаемое название — `ruwiki works`. Доступность имени ещё не проверена. При таком имени адрес сайта будет `https://ruwiki-works.toolforge.org/`. Техническое имя нельзя просто переименовать позже. [Tool accounts](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Tool_accounts).
4. На странице инструмента заполнить **Add toolinfo**: название, описание, контакт сопровождающего, адрес сайта и исходников. Для описания подойдёт: «Bot tasks for Russian Wikipedia with a web interface for monitoring, reports and admin controls». После запуска добавить ссылку на сайт на страницу своей бот-учётной записи.
5. Добавить публичный SSH-ключ в настройки developer account и подключиться с локального компьютера: `ssh SHELL_USERNAME@login.toolforge.org`. Использовать UNIX-имя из регистрации, затем `become TOOL_NAME`. Постоянные процессы запускаются через webservice и Jobs, а не непосредственно на сервере входа. [Правила Toolforge](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Rules).

Пример текста заявки на членство — это описание планов; его нужно сверить со своими реальными задачами:

> I would like to run bot tasks for Russian Wikipedia on Toolforge. The tasks process category discussions, add dates to maintenance templates, convert RQ parameters, add translation source metadata and update section templates. Jobs run serially on a schedule or after page changes. A web interface is used to monitor processing, control runs and display public reports and logs. Admin access uses Wikimedia OAuth. The code is licensed under MIT. Source: https://github.com/sc113/ruwiki-works.

### Нужен ли GitHub

GitHub необязателен. Требование Toolforge — публиковать код под лицензией, одобренной OSI. Для Build Service дополнительно нужен публичный Git-репозиторий; подойдут GitHub, GitLab Wikimedia или другой Git-хостинг. Репозиторий GitLab Wikimedia можно создать через страницу инструмента в Toolsadmin. [Правила](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Rules), [Build Service](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Build_Service).

Проект распространяется под лицензией MIT. В репозиторий включаются исходники проекта, тесты, документация, `Procfile`, `.python-version` и `.env.example`. `.env`, `var/`, локальная база, дампы, пароли, BotPassword и OAuth-секреты исключены через `.gitignore`. Взаимодействие с Википедией выполняется через MediaWiki API.

### Какие заявки относятся к Википедии

Членство Toolforge даёт доступ к хостингу. Оно не выдаёт боту разрешение на автоматические правки. Для новой бот-учётной записи или новых задач применяются [правила ботов рувики](https://ru.wikipedia.org/wiki/Википедия:Правила_применения_ботов) и [заявки на статус бота](https://ru.wikipedia.org/wiki/Википедия:Заявки_на_статус_бота). Если учётная запись и эти задачи уже согласованы, нужно сверить их с прежним разрешением; смена места запуска сама по себе не означает новую задачу.

Для входа на сайт отдельно регистрируется OAuth-приложение на Meta; этот порядок описан ниже. OAuth для входа и BotPassword для правок — разные механизмы.

## Способ развёртывания

Актуальный [Quickstart](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Quickstart) рекомендует новым инструментам **Build Service**: контейнер собирается из публичного репозитория и используется сайтом и Jobs. Для этого варианта подготовлены корневые `Procfile`, `.python-version` и `deploy/jobs.buildservice.yaml.example`. Основной порядок запуска — в [DEPLOY_BUILDSERVICE.md](DEPLOY_BUILDSERVICE.md). Реальная сборка, ToolsDB и OAuth будут проверены после создания tool account.

Альтернативный способ запуска — стандартный **Python webservice** с NFS и виртуальным окружением. Он также документирован Toolforge; `deploy/jobs.yaml.example` и `deploy/worker.sh` относятся именно к нему. Не нужно смешивать команды и пути этого варианта с контейнером Build Service. Выбор варианта не меняет логику сайта и очередь задач.

Официальные справки: [Python webservice](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Web/Python), [непрерывные задания](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Running_jobs), [ToolsDB](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Database), [Flask и Wikimedia OAuth](https://wikitech.wikimedia.org/wiki/Help:Toolforge/My_first_Flask_OAuth_tool), [защита записи от конфликтов](https://www.mediawiki.org/wiki/API:Edit). Версии доступных образов нужно проверить перед развёртыванием командой `toolforge jobs images`.

## Альтернативный вариант: стандартный Python webservice

Следующие разделы относятся только к NFS и стандартному образу Python. Для выбранного Build Service используйте отдельную инструкцию выше.

## 1. Аккаунт и исходники

Создать developer account и tool account; затем подключиться к `login.toolforge.org` и перейти в пользователя инструмента:

```bash
become TOOL_NAME
mkdir -p "$HOME/www/python/src"
```

Скопировать проект в `$HOME/www/python/src` или клонировать туда свой репозиторий. `.env`, `var/`, пароли и куки в репозиторий не включать.

## 2. База ToolsDB

Выполнить `sql tools`. Из `$HOME/replica.my.cnf` взять **только имя** пользователя ToolsDB вида `s12345`, чтобы выбрать имя базы с нужным префиксом. Создать:

```sql
CREATE DATABASE s12345__wiki_tools CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
```

Создать `.env` в каталоге проекта из `.env.example`, ограничить права: `chmod 600 .env`. Указать реальное имя инструмента и базы:

```dotenv
TOOLFORGE_DATABASE_URL=mysql+pymysql://@tools.db.svc.wikimedia.cloud/s12345__wiki_tools?read_default_file=/data/project/TOOL_NAME/replica.my.cnf&charset=utf8mb4
TOOLFORGE_PUBLIC_URL=https://TOOL_NAME.toolforge.org
TOOLFORGE_SECRET_KEY=REPLACE_WITH_A_RANDOM_SECRET
TOOLFORGE_ADMIN_USERNAME=YOUR_WIKIMEDIA_USERNAME
TOOLFORGE_TIMEZONE=Europe/Moscow
TOOLFORGE_START_MONTH=2019-01
TOOLFORGE_WIKI_WRITE=false
```

Сгенерировать секрет командой `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` и сохранить его в `.env`. Внешний HTTPS-адрес без заданного секрета намеренно не запускается. Секрет должен быть одинаковым при перезапуске webservice.

Вариант через `toolforge envvars` тоже поддерживается: значения окружения имеют приоритет над `.env`.

## 3. Python-окружение и сайт

В Python-контейнере:

```bash
toolforge webservice python3.13 shell
python3 -m venv "$HOME/www/python/venv"
source "$HOME/www/python/venv/bin/activate"
cd "$HOME/www/python/src"
python -m pip install -r requirements.txt
python -m toolforge_app init-db
exit
toolforge webservice python3.13 start
```

Webservice загружает корневой `app.py`, объект `app`. Python 3.13 указан в проверенной документации, но перед запуском нужно сверить доступные образы. Сайт должен открыться по `https://TOOL_NAME.toolforge.org/`. `/healthz` проверяет доступность сайта и базы.

## 4. OAuth и доступ admin

Зарегистрировать **OAuth 1.0a consumer для входа** на Meta через [Special:OAuthConsumerRegistration/propose](https://meta.wikimedia.org/wiki/Special:OAuthConsumerRegistration/propose). Callback:

```text
https://TOOL_NAME.toolforge.org/oauth/callback
```

Потребуется подтверждение личности пользователя; доступ на редактирование от имени посетителя сайту не нужен. Это отдельный consumer, а не owner-only consumer бота. Полученные ключ и секрет сохранить как `TOOLFORGE_OAUTH_KEY` и `TOOLFORGE_OAUTH_SECRET`. Перезапустить сайт:

```bash
toolforge webservice restart
```

Настроить `TOOLFORGE_ADMIN_USERNAME` в приватной конфигурации и проверить вход через Wikimedia OAuth. Роль admin доступна только указанной учётной записи; при пустом значении управление отключено. На страницах задач появится панель ручного запуска, паузы, остановки и перезапуска. Другие пользователи могут входить, но управление им не доступно; все отчёты доступны и без входа. Реальный вход нужно проверить после регистрации consumer — локальные тесты подтверждают поведение кода, а не регистрацию в Wikimedia. Если входом будут пользоваться другие участники, регистрация приложения проходит рассмотрение OAuth-администраторами; запрашивать только подтверждение личности. [Правила OAuth-приложений](https://meta.wikimedia.org/wiki/OAuth_app_guidelines).

## 5. Фоновые задания

В `deploy/jobs.yaml.example` заменить `TOOL_NAME`. Скопировать в `jobs.yaml` и выполнить:

```bash
toolforge jobs load jobs.yaml
toolforge jobs list
toolforge jobs logs ruwiki-executor -f
```

В примере два задания: `ruwiki-executor` (`worker --task all`) и `ruwiki-monitor` (`worker --task monitor`). Скрипт `deploy/worker.sh` читает зависимости из `$HOME/www/python/venv`. Для этого варианта нужен образ `python3.13` и доступ к NFS (`mount: all`, также являющийся обычным значением по умолчанию для стандартных образов). Первый запуск ОБКАТ получает актуальные страницы сам; монитор загружает счётчики категорий и списки шаблонов. Настройки, отметки проверенных статей и история хранятся в общей базе ToolsDB.

Проверить, что на сайте появился статус подключённого обработчика и завершённая первичная синхронизация. Затем просмотреть предлагаемые изменения, TXT-отчёт и таблицу. В режиме проверки задания также регулярно исполняются, но записей в Википедию не будет.

## 6. Подключение записи бота

Создать BotPassword нужной учётной записи бота с правами чтения и редактирования существующих страниц. Сохранить в `.env`:

```dotenv
TOOLFORGE_BOT_USERNAME=BOT_ACCOUNT_NAME
TOOLFORGE_BOT_LOGIN=BOT_ACCOUNT_NAME@BOT_PASSWORD_NAME
TOOLFORGE_BOT_PASSWORD=BOT_PASSWORD_VALUE
TOOLFORGE_WIKI_WRITE=true
```

В `.env` также указать User-Agent с названием инструмента и страницей/контактом сопровождающего. ОБКАТ пишет только месячные страницы и «Текущие обсуждения»; даты и RQ — только существующие статьи пространства 0 из снимка своих отслеживаемых категорий. Отсутствующие страницы автоматически не создаются. BotPassword не связан с OAuth для входа на сайт. Для задач шаблонов дополнительно проверьте «Автосохранение правок» в настройках каждой задачи.

Перезапустить webservice и фоновое задание, чтобы оба процесса получили новую конфигурацию:

```bash
toolforge webservice restart
toolforge jobs restart ruwiki-executor
toolforge jobs restart ruwiki-monitor
```

Выполнить обычный ручной запуск с сайта: после работы в режиме проверки он нужен, чтобы применить уже подготовленные изменения. Проверить правки бота и их описания в Википедии, а также успешное обновление таблицы. Конкретные кнопки/права BotPassword и перезапуск jobs стоит сверить с актуальной справкой CLI на этапе развёртывания.

## Обновления и восстановление

После замены исходников обновить зависимости внутри Python-контейнера и перезапустить сайт и два задания. Очередь, настройки и история хранятся в ToolsDB, поэтому сохраняются. Схема первого выпуска создаётся через `init-db`; три задачи шаблонов и общая блокировка используют существующие таблицы и отдельные ключи без изменения схемы. Будущие изменения схемы потребуют отдельной миграции, а не удаления базы.

Резервное копирование ToolsDB можно делать обычным `mariadb-dump` с `--defaults-file=$HOME/replica.my.cnf` и выбранной базой. Дамп содержит в том числе временные OAuth-запросы, поэтому его следует хранить с ограниченным доступом. Публичные логи доступны через сайт; служебные исключения — через `toolforge jobs logs`.
