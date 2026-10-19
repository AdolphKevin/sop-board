"""页面和就地更新响应共用看板所在机器的本地时间写法。"""

from datetime import datetime


def local_time(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    offset = value.strftime("%z")
    return value.strftime("%Y-%m-%d %H:%M:%S") + " " + offset[:3] + ":" + offset[3:5]

