# Запуск ruwiki works через Toolforge Build Service

При развёртывании нужно проверить контейнер, ToolsDB и Wikimedia OAuth. Команды ниже выполняются после создания аккаунта. Репозиторий: https://github.com/sc113/ruwiki-works. `ruwiki-works` как имя инструмента и `s12345` как пользователь базы — примеры: заменить после регистрации Toolforge.

## 1. Публичный репозиторий и заявка

Опубликовать проект как публичный `ruwiki-works` с лицензией MIT. Включить код, тесты, документацию, `requirements.txt`, `Procfile`, `.python-version` и `.env.example`. `.env`, локальная база, `var/`, дампы, BotPassword и файлы сессий в репозиторий не входят.

Создать [Wikimedia developer account](https://toolsadmin.wikimedia.org/register/), затем [подать заявку на членство Toolforge](https://toolsadmin.wikimedia.org/tools/membership/apply). Готовый английский текст — в [TOOLFORGE.md](TOOLFORGE.md). Добавить в него ссылку на репозиторий. После одобрения выйти из Toolsadmin и войти снова, создать tool account и добавить публичный SSH-ключ. [Официальный Quickstart](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Quickstart).

Подключиться из Windows:

```powershell
ssh SHELL_USERNAME@login.toolforge.org
```

Дальнейшие команды — в Linux-терминале Toolforge:

```bash
become ruwiki-works
git clone https://github.com/sc113/ruwiki-works.git "$HOME/ruwiki-works-src"
```

Копия на NFS нужна для документации и манифеста Jobs. Исполняемый контейнер будет собран из публичного репозитория.

## 2. ToolsDB и окружение

Запустить `sql tools`, посмотреть имя пользователя и создать базу с его префиксом:

```sql
SELECT CURRENT_USER();
CREATE DATABASE s12345__ruwiki_works CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
```

Файл `replica.my.cnf` создаёт Toolforge; проект читает его по абсолютному пути. Не копировать его в GitHub. [ToolsDB](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Database).

Задать параметры окружения; имя учётной записи admin ввести через интерактивный запрос:

```bash
toolforge envvars create TOOLFORGE_DATABASE_URL 'mysql+pymysql://@tools.db.svc.wikimedia.cloud/s12345__ruwiki_works?read_default_file=/data/project/ruwiki-works/replica.my.cnf&charset=utf8mb4'
toolforge envvars create TOOLFORGE_PUBLIC_URL 'https://ruwiki-works.toolforge.org'
toolforge envvars create TOOLFORGE_ADMIN_USERNAME
toolforge envvars create TOOLFORGE_TIMEZONE 'Europe/Moscow'
toolforge envvars create TOOLFORGE_WIKI_WRITE 'false'
```

Сгенерировать случайный секрет сессий и ввести его через скрытый запрос:

```bash
toolforge envvars create TOOLFORGE_SECRET_KEY
```

Значение должно сохраняться между перезапусками. Пароли и ключи тоже вводить через интерактивный запрос, без значений в командной строке. CLI может показать введённое значение в результате: такой вывод не публиковать. При передаче значения через stdin не добавлять завершающий перевод строки. [Envvars](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Envvars).

## 3. Сборка и создание таблиц

```bash
toolforge build start https://github.com/sc113/ruwiki-works.git
toolforge build show
toolforge build logs
toolforge jobs images
```

Продолжать после успешной сборки. Проверить фактическое имя образа, обычно `tool-ruwiki-works/tool-ruwiki-works:latest`:

```bash
toolforge jobs run ruwiki-init --image tool-ruwiki-works/tool-ruwiki-works:latest --command setup-db --mount all --wait
```

`setup-db` задан в `Procfile`; Jobs автоматически добавляет `launcher`. `--mount all` нужен для доступа к `replica.my.cnf`. Отдельное виртуальное окружение на NFS этому варианту не требуется. [Build Service](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Build_Service).

## 4. Сайт, OAuth и фоновые задания

```bash
toolforge webservice --mount all --health-check-path /healthz buildservice start
toolforge webservice buildservice logs -f
```

Проверить сайт и `/healthz`. Зарегистрировать на Meta [OAuth 1.0a consumer для входа](https://meta.wikimedia.org/wiki/Special:OAuthConsumerRegistration/propose) с callback `https://ruwiki-works.toolforge.org/oauth/callback`. Отметить «Allow consumer to specify a callback in requests and use callback URL above as a required prefix». Запрашивается только подтверждение личности; править от имени посетителя сайт не должен. Consumer входа и BotPassword исполнителя — разные учётные данные. [Правила OAuth](https://meta.wikimedia.org/wiki/OAuth_app_guidelines).

```bash
toolforge envvars create TOOLFORGE_OAUTH_KEY
toolforge envvars create TOOLFORGE_OAUTH_SECRET
toolforge webservice restart
```

Открыть `/login` напрямую и проверить вход через Wikimedia OAuth для настроенной учётной записи admin. Кнопка входа в публичном интерфейсе отсутствует; другим учётным записям авторизация недоступна. Публичный пользователь видит отчёты и сокращённые журналы.

После входа открыть `/admin`: на странице «Подключения» видны состояние ToolsDB, сигналы монитора и исполнителя, режим записи, проверки BotPassword и OAuth. Отсюда можно открыть страницы регистрации Wikimedia, проверить доступ и заменить учётные данные. Начальный OAuth и разрешённая учётная запись admin задаются на сервере, чтобы первый вход был доступен.

Введённые через страницу данные сохраняются в отдельной таблице ToolsDB с шифрованием Fernet и имеют приоритет над окружением. Ключ выводится из `TOOLFORGE_SECRET_KEY`: при восстановлении базы нужен тот же секрет сервера. Пароли не возвращаются в HTML, журнал или cookie. Неверная замена не сохраняется. Сайт использует новые данные при следующем входе, исполнитель — при следующей авторизации; перезапуск не требуется. Проверки подключений никогда не включают запись.

После смены секрета сервера нужно повторно задать учётные данные. Если вход недоступен, команда `python -m toolforge_app clear-connection oauth`, выполненная в окружении приложения, убирает сохранённый через сайт OAuth и возвращает настройки окружения. Аналогично `clear-connection bot` возвращает серверный BotPassword. Режим записи эти команды не меняют.

В `deploy/jobs.buildservice.yaml.example` заменить `TOOL_NAME` на выбранное имя; сохранить как `jobs.yaml` в каталоге инструмента:

```bash
toolforge jobs load jobs.yaml
toolforge jobs list
toolforge jobs logs ruwiki-executor -f
```

Манифест запускает два непрерывных задания: исполнитель всех задач и независимый монитор. Не нужны отдельные cron-задания на каждую обработку: часы и интервалы берутся из базы. Исполнитель обрабатывает задачи последовательно, включая назначенные на одинаковое время. Health-проверки используют команды `health-executor` и `health-monitor` из `Procfile`; их работу проверить после запуска Jobs.

Время по умолчанию: поиск ОБКАТ каждые 5 минут, обработка через 15 минут без правок, форматирование в последний день месяца в 23:30 МСК. Шаблоны о проблемах — 03:00; переводы и разделы — 04:00. Поиск остальных статей — каждые 6 часов. Расписание редактируется в панели admin.

Сначала просмотреть работу в режиме проверки: очередь, счётчики, предлагаемые изменения, полные и публичные журналы. Для проверок остановки и ручного запуска нужен настоящий вход, локальный предпросмотр их не выполняет.

## 5. Включение правок

После проверки пробных результатов настроить BotPassword существующей согласованной учётной записи бота. Нужны редактирование существующих страниц и разрешения, позволяющие использовать её флаг бота. Детали сохранения и ошибок — в [API_SAFETY.md](API_SAFETY.md).

```bash
toolforge envvars create TOOLFORGE_BOT_USERNAME
toolforge envvars create TOOLFORGE_BOT_LOGIN
toolforge envvars create TOOLFORGE_BOT_PASSWORD
toolforge envvars create TOOLFORGE_USER_AGENT 'ruwiki-works/1.0 (https://github.com/sc113/ruwiki-works)'
```

Логин BotPassword имеет вид `BOT_ACCOUNT@PASSWORD_NAME`. После осознанного включения записи изменить `TOOLFORGE_WIKI_WRITE` на `true` через `toolforge envvars create TOOLFORGE_WIKI_WRITE true`, затем перезапустить сайт и задания:

```bash
toolforge webservice restart
toolforge jobs restart ruwiki-executor
toolforge jobs restart ruwiki-monitor
```

Для задач статей проверить «Автосохранение» в параметрах. Выполнить один ручной запуск и сверить реальные изменения, флаги bot/minor и описания правок в Википедии. До этой проверки полное соответствие окружению Toolforge не подтверждено.

## Обновление

После публикации изменений выполнить новую сборку, дождаться успеха и перезапустить webservice и оба Jobs. Очередь и отчёты сохраняются в ToolsDB. Перед изменениями схемы делать резервную копию базы; `init-db` создаёт отсутствующие таблицы, но не заменяет миграции уже существующих колонок.
