"""按检出运行 adapter.env：端口偏移、跨项目端口租约、就绪等待与进程组清理（守护进程侧）。"""
from . import daemon


def require_stopped(context, checkout):
    with context.store.connection() as db:
        row = db.execute('SELECT * FROM environments WHERE checkout=?', (checkout.id,)).fetchone()
    if row and row['state'] in ('starting', 'ready') and row['pid'] and daemon.alive(row['pid']):
        raise ValueError(f'检出 {checkout.name} 的环境仍在运行；先运行 sop-board env down --checkout {checkout.name}')
