"""演示商城：Web 页面与 JSON API，SQLite 存储；退款规则来自 lib 仓库的 refundlib。

python3 server.py --port 8100 --db shop.sqlite3 --lib <lib 仓库目录> [--stock-url <库存服务地址>]

下单（POST /api/purchases）先经库存服务扣减库存，成功后才生成订单；没有给出库存服务地址或它无法应答时，
返回 503 与降级提示“库存服务暂不可用，请稍后再试”，不生成订单（库存演示项目的 SOP stock.md 规则 R2）。
"""
import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

HERE = Path(__file__).resolve().parent
SESSIONS = {}
STOCK_UNAVAILABLE = '库存服务暂不可用，请稍后再试'


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, shop TEXT NOT NULL DEFAULT 'demo',
                          paid REAL NOT NULL, delivered_days_ago INTEGER NOT NULL);
                        CREATE TABLE IF NOT EXISTS refunds(id INTEGER PRIMARY KEY, order_id TEXT NOT NULL UNIQUE, amount REAL NOT NULL,
                          status TEXT NOT NULL DEFAULT '已退款');
                        CREATE TABLE IF NOT EXISTS order_items(order_id TEXT PRIMARY KEY, sku TEXT NOT NULL, qty INTEGER NOT NULL);''')
    return db


class StockUnavailable(Exception):
    """库存服务没有地址、连不上、超时或返回 5xx。"""


class StockShortage(Exception):
    pass


class StockClient:
    """库存服务客户端。测试可以换成替身：只要提供 quantity(sku) 与 deduct(sku, qty)，不可用时抛 StockUnavailable。"""

    def __init__(self, url, timeout=3):
        self.url, self.timeout = (url or '').rstrip('/'), timeout
        self.opener = build_opener(ProxyHandler({}))

    def call(self, method, path, body=None):
        if not self.url:
            raise StockUnavailable('没有库存服务地址')
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.url + path, data=data, method=method, headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b'{}')
        except HTTPError as error:
            if error.code >= 500:
                raise StockUnavailable(f'库存服务返回 {error.code}') from error
            return error.code, json.loads(error.read() or b'{}')
        except (URLError, OSError, ValueError) as error:
            raise StockUnavailable(str(error)) from error

    def quantity(self, sku):
        return self.call('GET', '/api/stock/' + quote(sku))[1]['quantity']

    def deduct(self, sku, qty):
        status, body = self.call('POST', f'/api/stock/{quote(sku)}/deduct', {'qty': qty})
        if status == 409:
            raise StockShortage(body.get('error') or '库存不足')
        if status != 200:
            raise StockUnavailable(f'库存服务返回 {status}')
        return body['quantity']


def purchase(db, data, stock):
    """下单：先扣减库存，成功后才写订单。返回 (状态码, 响应正文)。"""
    order_id, sku = str(data.get('order_id') or ''), str(data.get('sku') or '')
    try:
        qty, paid = int(data.get('qty', 1)), float(data.get('paid', 0))
    except (TypeError, ValueError):
        return 422, {'error': '数量与金额须为数字'}
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', order_id) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', sku) or qty <= 0 or paid <= 0:
        return 422, {'error': '须给出订单号、商品、正数数量与实付金额'}
    if db.execute('SELECT 1 FROM orders WHERE id=?', (order_id,)).fetchone():
        return 409, {'error': '订单已存在'}
    try:
        left = stock.deduct(sku, qty)
    except StockUnavailable:
        return 503, {'error': STOCK_UNAVAILABLE}
    except StockShortage as error:
        return 409, {'error': str(error)}
    db.execute('INSERT INTO orders(id, paid, delivered_days_ago) VALUES (?,?,0)', (order_id, paid))
    db.execute('INSERT INTO order_items(order_id, sku, qty) VALUES (?,?,?)', (order_id, sku, qty))
    db.commit()
    return 201, {'order_id': order_id, 'sku': sku, 'qty': qty, 'paid': paid, 'stock_left': left}


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
        if path == '/api/orders':
            order = parse_qs(urlsplit(self.path).query).get('id', [''])[-1]
            return self.send(200, [dict(r) for r in db.execute(
                'SELECT o.id, o.paid, i.sku, i.qty FROM orders o LEFT JOIN order_items i ON i.order_id=o.id WHERE o.id=?', (order,))])
        match = re.fullmatch(r'/api/stock/([A-Za-z0-9_-]+)', path)
        if match:
            try:
                return self.send(200, {'sku': match[1], 'quantity': self.server.stock.quantity(match[1])})
            except StockUnavailable:
                return self.send(503, {'error': STOCK_UNAVAILABLE})
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
        if path == '/api/purchases':
            status, body = purchase(db, data, self.server.stock)
            return self.send(status, body)
        return self.send(404, {'error': 'not found'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--lib', required=True)
    parser.add_argument('--stock-url', default='', help='库存服务地址；不给时下单返回降级提示')
    args = parser.parse_args()
    sys.path.insert(0, args.lib)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.db_path = args.db
    server.stock = StockClient(args.stock_url)
    connect(args.db).close()
    print(f'listening on {args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
