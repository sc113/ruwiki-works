import json
from datetime import datetime, timezone

from sqlalchemy.dialects.mysql.pymysql import dialect

from toolforge_app.storage import epoch_time


def test_toolsdb_timestamps_work_with_datetime_json_and_elapsed_time():
    mysql = dialect()
    column_type = epoch_time.dialect_impl(mysql)
    process = column_type.result_processor(mysql, 5)  # PyMySQL DOUBLE
    timestamp = 1728000000.125
    value = process(timestamp) if process else timestamp

    assert isinstance(value, float)
    assert datetime.fromtimestamp(value, timezone.utc).microsecond == 125000
    assert timestamp + 0.5 - value == 0.5
    assert json.loads(json.dumps({"at": value}))["at"] == timestamp
