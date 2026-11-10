"""数据配方 seed-order：向本检出的演示 SQLite 写入一笔测试订单（只准备订单，不准备退款）。

看板在数据目录运行本脚本：参数经大写环境变量传入（ORDER_ID、PAID、DAYS），检出名在 SOP_CHECKOUT。
DEMO_SEED_TOKEN 模拟写库凭据；本脚本把它写进输出，用来核对看板留痕会遮蔽凭据取值（真实配方不应输出凭据）。
"""
import json
import os
from pathlib import Path
import sqlite3
import sys

token = os.environ.get('DEMO_SEED_TOKEN')
if not token:
    sys.exit('缺少环境变量 DEMO_SEED_TOKEN')
if not os.environ.get('ORDER_ID'):
    sys.exit('缺少参数 order_id')
path = Path.cwd() / 'env' / os.environ['SOP_CHECKOUT'] / 'shop.sqlite3'
if not path.is_file():
    sys.exit(f'演示 SQLite 不存在：{path}；先让看板为本检出拉起环境（sop-board env up）')
order = dict(id=os.environ['ORDER_ID'], paid=float(os.environ.get('PAID') or 99), delivered_days_ago=int(os.environ.get('DAYS') or 3))
db = sqlite3.connect(path)
db.execute('INSERT OR REPLACE INTO orders(id, paid, delivered_days_ago) VALUES (:id, :paid, :delivered_days_ago)', order)
db.commit()
print(f'写库凭据 {token}')
print(json.dumps(dict(order, db=str(path)), ensure_ascii=False))
