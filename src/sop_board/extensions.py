"""守护进程的组成：各功能模块在这里挂到 App 上，并登记启动恢复与关闭钩子。"""

RECOVER = []
CLOSE = []


def attach(app):
    """按依赖顺序挂载功能模块；模块在导入时用 server.route 注册路由。"""
    for name in MODULES:
        module = __import__('sop_board.' + name, fromlist=['attach'])
        hook = getattr(module, 'attach', None)
        if hook:
            hook(app)


MODULES = ['browsers', 'gate', 'launcher', 'web', 'guards', 'settings']
