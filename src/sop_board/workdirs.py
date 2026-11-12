"""工作输出目录：原子创建未占用的位置，保留数据库重建前的所有输出。"""


def create(parent, name):
    """优先使用 name；已有文件、目录或符号链接时顺延后缀，只返回本次新建的目录。"""
    parent.mkdir(parents=True, exist_ok=True)
    sequence = 1
    while True:
        directory = parent / (str(name) if sequence == 1 else f'{name}-{sequence}')
        try:
            directory.mkdir()
        except FileExistsError:
            sequence += 1
        else:
            return directory
