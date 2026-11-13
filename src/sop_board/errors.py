"""接口的结构化失败响应。"""


class APIError(Exception):
    def __init__(self, status, code, problems, **details):
        self.status, self.code, self.problems = status, code, problems
        self.details = details
        super().__init__(code)


def reject(status, code, field, problem):
    raise APIError(status, code, [dict(field=field, problem=problem)])
