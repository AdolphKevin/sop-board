"""别的机器上的证据（已定决策 21）：证据文件留在产出它的机器上；带 --lan 的看板把机器标识、显示名、内网地址与一个
对等令牌登记到业务记录库，别的看板与 report --zip 按登记的地址向产出机器的看板读取，核对 sha256 后使用，不在本机保存。
那台看板没开、没加 --lan 或地址已失效时读不到，页面与报告写明“证据在 <机器显示名> 的机器上”。

对等读取用 /peer/ 接口：须带业务记录库里登记的对等令牌（能读业务记录库的人才拿得到），返回原始字节（内网遮蔽会改变
内容、使 sha256 对不上）；中转给本看板内网页面时照常按本看板的规则遮蔽。
"""
import hashlib
import hmac
import json
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

HEADER = 'X-Sop-Board-Peer'
_cache, _guard = {}, threading.Lock()
TTL = 10


def opener():
    return build_opener(ProxyHandler({}))


def registered(db, machine):
    """业务记录库里登记的地址与令牌：(地址列表, 令牌)。"""
    row = db.one('SELECT addresses, peer_token FROM machines WHERE id=?', (machine,))
    if row is None or not row['addresses'] or not row['peer_token']:
        return [], None
    try:
        addresses = json.loads(row['addresses'])
    except ValueError:
        addresses = []
    return [a for a in addresses if isinstance(a, str)], row['peer_token']


def _hello(address, token, machine, timeout):
    try:
        request = Request(address + '/peer/hello', headers={HEADER: token})
        with opener().open(request, timeout=timeout) as response:
            return json.loads(response.read() or b'{}').get('machine') == machine
    except (URLError, OSError, ValueError):
        return False


def reachable(db, machine, *, timeout=1.5):
    """产出机器的看板此刻是否可达：返回可用的 (地址, 令牌) 或 None；结果缓存 10 秒，页面轮询不会反复探测。"""
    addresses, token = registered(db, machine)
    key = (machine, tuple(addresses), token)
    with _guard:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < TTL:
            return hit[1]
    found = None
    if token:
        for address in addresses:
            if _hello(address, token, machine, timeout):
                found = (address, token)
                break
    with _guard:
        _cache[key] = (time.monotonic(), found)
    return found


def fetch(db, sha, machines, *, timeout=30):
    """从产出机器的看板读取证据并核对 sha256；都读不到时返回 None。"""
    for machine in machines:
        target = reachable(db, machine)
        if not target:
            continue
        address, token = target
        try:
            request = Request(f'{address}/peer/evidence/{sha}', headers={HEADER: token})
            with opener().open(request, timeout=timeout) as response:
                raw = response.read()
        except (URLError, OSError):
            with _guard:
                _cache.pop((machine, tuple(registered(db, machine)[0]), token), None)
            continue
        if hashlib.sha256(raw).hexdigest() == sha:
            return raw
    return None


def producers(db, product, sha):
    """证据索引里登记的产出机器。"""
    return [row['machine'] for row in db.execute('SELECT machine FROM evidence WHERE product=? AND sha256=?', (product, sha))]


def allowed(app, request):
    supplied = request.headers.get(HEADER, '')
    return bool(app.peer_token) and hmac.compare_digest(supplied, app.peer_token)


def register(app):
    """看板启动（以及之后业务记录库恢复时）登记本机：带 --lan 时写内网地址与对等令牌，不带 --lan 时撤下本端口的登记。"""
    store = app.store
    machine = store.machine
    stamp = __import__('sop_board.store', fromlist=['now']).now()
    with store.records_tx() as db:
        if app.lan and app.addresses:
            db.execute('INSERT INTO machines(id, name, addresses, peer_token, updated_at) VALUES (?,?,?,?,?) '
                       'ON CONFLICT(id) DO UPDATE SET name=excluded.name, addresses=excluded.addresses, '
                       'peer_token=excluded.peer_token, updated_at=excluded.updated_at',
                       (machine['id'], machine['name'], json.dumps(app.addresses), app.peer_token, stamp))
        else:
            db.execute('INSERT INTO machines(id, name, addresses, peer_token, updated_at) VALUES (?,?,NULL,NULL,?) '
                       'ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at',
                       (machine['id'], machine['name'], stamp))
            unregister(app, db, stamp)
    app.registered = True
    app.machine_name_sync_pending = False


def unregister(app, db, stamp):
    """撤下本看板端口的登记（同一台机器上别的看板登记的地址不动）。"""
    addresses, _ = registered(db, app.store.machine['id'])
    if addresses and all(a.rsplit(':', 1)[-1] == str(app.port) for a in addresses):
        db.execute('UPDATE machines SET addresses=NULL, peer_token=NULL, updated_at=? WHERE id=?', (stamp, app.store.machine['id']))


def ensure_registered(app):
    if getattr(app, 'registered', False):
        return
    try:
        register(app)
    except Exception:  # noqa: BLE001 - 业务记录库不可用或未绑定时，下次请求再登记
        pass


def withdraw(app):
    """看板退出时撤下自己的登记（尽力而为）。"""
    if not app.lan:
        return
    try:
        with app.store.records_tx() as db:
            unregister(app, db, __import__('sop_board.store', fromlist=['now']).now())
    except Exception:  # noqa: BLE001
        pass
