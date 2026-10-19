"""为单独验证检查与合并的旧用例准备已登记版本，避免自动执行成为无关前置操作。"""
import yaml

from helpers import push_body, KEY


def prepared_board(factory, command, repo, env):
    board = factory(env=env)
    for path in sorted((repo / 'docs/sop').glob('*.yaml')):
        if path.name == 'entries.yaml':
            continue
        document = yaml.safe_load(path.read_text())
        from sop_board.repositories import product
        entry = repo / ('.tw/config.json' if (repo / '.tw/config.json').exists() else 'docs/sop/entries.yaml')
        body = push_body(document, path=str(path.relative_to(repo)), pushed_by='工作区', content=path.read_text(), **product(repo, path, entry))
        status, response, _ = board.request('PUT', f"/api/v1/projects/{KEY}/batches/{document['metadata']['batch']}/scenario-file", body)
        assert status == 200, response
    board.stop()
    board.command = command
    board.start()
    return board
