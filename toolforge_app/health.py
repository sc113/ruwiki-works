"""Shared service state and durable incident notifications from trusted evidence."""
import time

from .processors import TASKS
from .schedules import get_schedule, next_daily_time


def component_health(settings, store, component, now=None):
    now = time.time() if now is None else now
    at = store.get_state(component + '_heartbeat', 0)
    return dict(name='Исполнитель' if component == 'executor' else 'Монитор', at=at,
                online=bool(at and now - at < 300))


def system_status(settings, store, now=None):
    now = time.time() if now is None else now
    components = {key: component_health(settings, store, key, now) for key in ('executor', 'monitor')}
    conditions, stale = {}, set()
    tasks = [task for task in TASKS if task.enabled]
    controls = {task.slug: store.control(task.slug) for task in tasks}
    stopped = all(value['mode'] == 'stopped' for value in controls.values())
    paused = all(value['mode'] != 'active' for value in controls.values())
    running = []

    def alert(key, title, message, target='/console'):
        conditions[key] = dict(title=title, message=message, target=target)

    if not stopped:
        if not any(c['at'] for c in components.values()):
            alert('service:not-started', 'Фоновая обработка не запущена',
                  'Сайт доступен, но исполнитель и монитор ещё не сообщили о запуске.')
        else:
            for key, component in components.items():
                if not component['online']:
                    alert('service:' + key, component['name'] + ' недоступен',
                          'Нет свежего сигнала от процесса. Проверьте фоновое задание Toolforge.')
    for task in tasks:
        history = store.list_runs(1, processor=task.slug, exclude_import=True)
        latest = history[0] if history else None
        control = controls[task.slug]
        target = '/processors/' + task.processor + '?task=' + task.slug + '&view=problems#reports'
        if latest and latest['status'] == 'running':
            running.append(task.title)
        if latest and latest['status'] in {'failed', 'interrupted'} and not (
                control.get('action') == 'restart' and latest['started_at'] < control['at']):
            alert('run:' + task.slug, task.title + ': ' + ('ошибка' if latest['status'] == 'failed' else 'проход прерван'),
                  'Проверьте результат последнего запуска и журнал.', '/runs/' + latest['id'])
        prefix = '' if task.slug == 'obkat' else task.slug + ':'
        for suffix, label in [('worker_error', 'Ошибка обработчика'), ('monitor_error', 'Ошибка обновления данных')]:
            if store.get_state(prefix + suffix) and not (suffix == 'worker_error' and latest and latest['status'] in {'failed', 'interrupted'}):
                alert(suffix + ':' + task.slug, label + ': ' + task.title,
                      'Подробности доступны в отчёте задачи и полном журнале.', target)
        if control['mode'] == 'stopped':
            continue
        if task.slug == 'obkat':
            checked = store.get_state('last_poll', 0)
            max_age = get_schedule(settings, store, task.slug)['search_minutes'] * 60 + 300
        else:
            checked = store.get_state(task.slug + ':inventory', {}).get('checked_at', 0)
            max_age = get_schedule(settings, store, task.slug)['search_minutes'] * 60 + 3600
        started = store.get_state('monitor_started_at', 0)
        if (checked and now - checked > max_age) or (not checked and started and now - started > max_age):
            stale.add(task.slug)
            alert('stale:' + task.slug, 'Данные устарели: ' + task.title,
                  'Счётчики или страницы не были обновлены в ожидаемый срок.', target)

    # A ready task waiting behind a running action has not missed its turn.
    if not running:
        for task in tasks:
            if 'daily' not in task.schedules or controls[task.slug]['mode'] != 'active':
                continue
            baseline = max(store.get_state(task.slug + ':schedule_started_at', 0),
                           controls[task.slug].get('at') or 0,
                           store.get_state(task.slug + ':schedule_updated', {}).get('at', 0))
            if not baseline:
                continue
            deadline = next_daily_time(get_schedule(settings, store, task.slug)['run_time'], now) - 86400
            recent = store.list_runs(1, processor=task.slug, exclude_import=True, started_from=deadline)
            if deadline >= baseline and now - deadline > 900 and not recent:
                alert('missed:' + task.slug, 'Пропущен запуск: ' + task.title,
                      'После времени ежедневного запуска прошло больше 15 минут; обработка не началась.', '/console?task=' + task.slug)

    executor, monitor = components['executor'], components['monitor']
    if stopped:
        status, label = 'stopped', 'Бот остановлен'
    elif paused:
        status, label = 'paused', 'Обработка приостановлена'
    elif not executor['online']:
        status, label = 'offline', 'Нет связи с обработчиком' if executor['at'] else 'Обработка не запущена'
    elif running:
        status, label = 'running', 'В работе'
    elif not monitor['online']:
        status, label = 'warning', 'Работает · наблюдение недоступно'
    elif conditions:
        status, label = 'warning', 'Работает · требуется внимание'
    else:
        status, label = 'success', 'Бот работает'
    store.sync_notifications(conditions, now)
    return dict(status=status, label=label, running=running, components=components,
                active_alerts=len(conditions), unread=store.unread_notifications(), stale=stale, now=now)
