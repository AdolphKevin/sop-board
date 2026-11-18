"""经本检出的地址核对演示商城，供 entries.*.probe 调用。"""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('entry', choices=('api', 'web'))
    entry = parser.parse_args().entry
    checkout = os.environ['SOP_CHECKOUT']
    directory = Path(os.environ['SOP_ENV_LOG_DIR'])
    address_var, route = ('APP_URL', '/api/orders?id=probe') if entry == 'api' else ('WEB_URL', '/')
    address = os.environ.get(address_var)
    print(f'{entry} · {checkout} · {address_var}={address or "未设置"}', flush=True)
    if (directory / f'{entry}.probe-fail').is_file():
        print('demo probe unavailable')
        return 1
    if (directory / f'{entry}.probe-slow').is_file():
        time.sleep(3)
    if not address:
        return 1
    try:
        with urlopen(address.rstrip('/') + route, timeout=5) as response:
            body = response.read()
            if entry == 'api':
                healthy = isinstance(json.loads(body), list)
            else:
                healthy = '<title>演示商城</title>' in body.decode('utf-8')
            if response.status != 200 or not healthy:
                raise ValueError(f'{route} 未返回预期的商城响应')
    except (OSError, ValueError) as error:
        print(error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
