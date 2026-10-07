web: gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 2 --timeout 60 --access-logfile - --error-logfile - app:app
executor: python -u -m toolforge_app worker --task all
monitor: python -u -m toolforge_app worker --task monitor
setup-db: python -u -m toolforge_app init-db
health-executor: python -m toolforge_app check-health executor
health-monitor: python -m toolforge_app check-health monitor
