"""演示库存服务：查询与扣减库存，SQLite 存储。商城下单时调用它。

python3 server.py --port 28090 --db stock.sqlite3 [--crash]

启动时为演示商品 DEMO-1、DEMO-2 补足 100 件库存（已有的不改）。--crash 让服务启动后立即异常退出（演示开关 DEMO_STOCK_CRASH 用）。
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import sqlite3
import threading

SEED = {'DEMO-1': 100, 'DEMO-2': 100}
LOCK = threading.Lock()


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS stock(sku TEXT PRIMARY KEY, quantity INTEGER NOT NULL)')
    return db


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print('stock ' + format % args, flush=True)

    def send(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/':
            return self.send(200, {'service': 'stock', 'ok': True})
        match = re.fullmatch(r'/api/stock/([A-Za-z0-9_-]+)', self.path)
        if not match:
            return self.send(404, {'error': 'not found'})
        db = connect(self.server.db_path)
        row = db.execute('SELECT quantity FROM stock WHERE sku=?', (match[1],)).fetchone()
        return self.send(200, {'sku': match[1], 'quantity': row['quantity'] if row else 0})

    def do_POST(self):
        match = re.fullmatch(r'/api/stock/([A-Za-z0-9_-]+)/deduct', self.path)
        if not match:
            return self.send(404, {'error': 'not found'})
        try:
            qty = int(json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0') or 0)) or b'{}').get('qty', 1))
        except (ValueError, TypeError):
            return self.send(422, {'error': '数量须为正整数'})
        if qty <= 0:
            return self.send(422, {'error': '数量须为正整数'})
        with LOCK:
            db = connect(self.server.db_path)
            row = db.execute('SELECT quantity FROM stock WHERE sku=?', (match[1],)).fetchone()
            current = row['quantity'] if row else 0
            if current < qty:
                return self.send(409, {'error': '库存不足', 'sku': match[1], 'quantity': current})
            db.execute('UPDATE stock SET quantity=? WHERE sku=?', (current - qty, match[1]))
            db.commit()
        return self.send(200, {'sku': match[1], 'quantity': current - qty})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--crash', action='store_true')
    args = parser.parse_args()
    db = connect(args.db)
    for sku, quantity in SEED.items():
        db.execute('INSERT OR IGNORE INTO stock(sku, quantity) VALUES (?,?)', (sku, quantity))
    db.commit()
    if args.crash:
        raise RuntimeError('演示开关 DEMO_STOCK_CRASH=1：库存服务启动后立即异常退出')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.db_path = args.db
    print(f'stock listening on {args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
