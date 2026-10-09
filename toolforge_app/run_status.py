"""Execution status is independent of the backlog requiring manual attention."""
import json


STATUS_LABELS = {"success": "Завершён", "failed": "Ошибка",
                 "running": "В работе", "interrupted": "Прерван",
                 "paused": "Приостановлен", "stopped": "Остановлен"}


def remaining_problems(summary, report):
    value = summary.get('report_problems', report.get('problems', summary.get('problems', 0)))
    return value if type(value) is int else 0


def completion_status(status, summary, report):
    if status not in {'success', 'failed', 'issues'}:
        return status
    errors = summary.get('errors')
    if errors:
        return 'failed'
    # An older failed run without an error count is not evidence of success.
    if status == 'failed' and errors is None:
        return status
    if status == 'issues' or status == 'failed' and remaining_problems(summary, report):
        return 'success'
    return status


def completion_message(status, summary, report):
    if status == 'failed':
        return 'Обработка завершена с ошибками'
    return 'Обработка завершена' if status == 'success' else 'Обработка прервана'


def normalize_run(row):
    """Apply the same classification to older runs without rewriting their data."""
    row = dict(row)
    summary, report = json.loads(row['summary']), json.loads(row['report'])
    row['status'] = completion_status(row['status'], summary, report)
    if row['status'] == 'success':
        events = json.loads(row['events'])
        for event in events:
            if event['code'] == 'finished':
                event['message'] = completion_message('success', summary, report)
                event.pop('tone', None)
        row['events'] = json.dumps(events, ensure_ascii=False)
    return row


def status_expression(runs):
    """SQLite and MariaDB filters must also classify legacy run rows."""
    from sqlalchemy import case, func
    summary, report = runs.c.summary, runs.c.report
    errors = func.json_extract(summary, '$.errors')
    problems = func.coalesce(func.json_extract(summary, '$.report_problems'),
                             func.json_extract(report, '$.problems'),
                             func.json_extract(summary, '$.problems'), 0)
    completed = runs.c.status.in_(['success', 'failed', 'issues'])
    manual_only = ((runs.c.status == 'issues') | ((runs.c.status == 'failed')
                   & (errors == 0) & (problems > 0)))
    return case((completed & (errors > 0), 'failed'), (manual_only, 'success'), else_=runs.c.status)
