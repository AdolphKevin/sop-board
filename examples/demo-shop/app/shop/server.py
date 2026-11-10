"""演示商城：Web 页面与 JSON API，SQLite 存储；退款规则来自 lib 仓库的 refundlib。

python3 server.py --port 8100 --db shop.sqlite3 --lib <lib 仓库目录>
"""
import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
SESSIONS = {}


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, shop TEXT NOT NULL DEFAULT 'demo',
                          paid REAL NOT NULL, delivered_days_ago INTEGER NOT NULL);
                        CREATE TABLE IF NOT EXISTS refunds(id INTEGER PRIMARY KEY, order_id TEXT NOT NULL UNIQUE, amount REAL NOT NULL,
                          status TEXT NOT NULL DEFAULT '已退款');''')
    return db


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, kind='application/json; charset=utf-8', headers=()):
        raw = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0') or 0))
        if self.headers.get_content_type() == 'application/json':
            return json.loads(raw or b'{}')
        return {k: v[-1] for k, v in parse_qs(raw.decode()).items()}

    def user(self):
        cookie = SimpleCookie(self.headers.get('Cookie', ''))
        token = cookie['shop_session'].value if 'shop_session' in cookie else None
        return SESSIONS.get(token)

    def do_GET(self):
        path = urlsplit(self.path).path
        db = connect(self.server.db_path)
        if path == '/':
            return self.send(200, (HERE / 'templates' / 'home.html').read_text(encoding='utf-8'), 'text/html; charset=utf-8')
        if path == '/login':
            return self.send(200, '<!doctype html><html><head><meta charset="utf-8"><title>商家登录</title></head><body>'
                             '<form method="post" action="/login"><label>账号 <input name="user"></label>'
                             '<label>密码 <input name="password" type="password"></label><button>登录</button></form></body></html>',
                             'text/html; charset=utf-8')
        if path == '/orders':
            if not self.user():
                return self.send(302, '', headers=[('Location', '/login')])
            rows = db.execute('SELECT o.id, o.paid, r.status FROM orders o LEFT JOIN refunds r ON r.order_id=o.id WHERE o.shop=? ORDER BY o.id',
                              (self.user(),)).fetchall()
            items = ''.join(f'<tr><td>{html.escape(r["id"])}</td><td>{r["paid"]:.2f}</td><td>{html.escape(r["status"] or "未退款")}</td></tr>' for r in rows)
            return self.send(200, f'<!doctype html><html><head><meta charset="utf-8"><title>订单</title></head><body><h1>订单</h1>'
                             f'<table><thead><tr><th>订单</th><th>实付</th><th>退款状态</th></tr></thead><tbody>{items}</tbody></table></body></html>',
                             'text/html; charset=utf-8')
        if path == '/api/refunds':
            order = parse_qs(urlsplit(self.path).query).get('order_id', [''])[-1]
            return self.send(200, [dict(r) for r in db.execute('SELECT * FROM refunds WHERE order_id=?', (order,))])
        return self.send(404, {'error': 'not found'})

    def do_POST(self):
        path = urlsplit(self.path).path
        db = connect(self.server.db_path)
        data = self.body()
        if path == '/login':
            if data.get('user') == 'merchant' and data.get('password') == os.environ.get('DEMO_MERCHANT_PASSWORD', 'merchant-pass'):
                token = secrets.token_hex(16)
                SESSIONS[token] = 'demo'
                return self.send(302, '', headers=[('Location', '/orders'), ('Set-Cookie', f'shop_session={token}; HttpOnly; Path=/')])
            return self.send(401, '<p>账号或密码错误</p>', 'text/html; charset=utf-8')
        if path == '/api/orders':
            db.execute('INSERT OR REPLACE INTO orders(id, paid, delivered_days_ago) VALUES (?,?,?)',
                       (data['id'], float(data['paid']), int(data['delivered_days_ago'])))
            db.commit()
            return self.send(201, {'id': data['id']})
        if path == '/api/refunds':
            import refundlib
            order = db.execute('SELECT * FROM orders WHERE id=?', (data.get('order_id'),)).fetchone()
            if order is None:
                return self.send(404, {'error': '订单不存在'})
            if not refundlib.can_apply(order['delivered_days_ago']):
                return self.send(422, {'error': '已超过退款申请期限'})
            if db.execute('SELECT 1 FROM refunds WHERE order_id=?', (order['id'],)).fetchone():
                return self.send(409, {'error': '该订单已退款'})
            try:
                amount = refundlib.refund_amount(order['paid'], data.get('amount'))
            except ValueError as error:
                return self.send(422, {'error': str(error)})
            db.execute('INSERT INTO refunds(order_id, amount) VALUES (?,?)', (order['id'], amount))
            db.commit()
            return self.send(201, {'order_id': order['id'], 'amount': amount, 'status': '已退款'})
        return self.send(404, {'error': 'not found'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--lib', required=True)
    args = parser.parse_args()
    sys.path.insert(0, args.lib)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.db_path = args.db
    connect(args.db).close()
    print(f'listening on {args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
