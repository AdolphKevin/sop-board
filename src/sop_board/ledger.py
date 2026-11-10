"""待人处理、身份使用计数与前置数据配方。"""
import json
import re

from .entries import ENV_NAME

RECIPE_NAME = re.compile(r'[a-z0-9][a-z0-9-]*\Z')


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and '\0' not in value


def _texts(value, *, empty=False):
    return isinstance(value, list) and (empty or bool(value)) and all(_text(v) for v in value)


def validate_fixtures(value, problem):
    """fixtures：{environment, scope, credential_env?, adhoc?, recipes?, real_only?}。"""
    if not isinstance(value, dict):
        problem('fixtures', '须为含 environment、scope 的对象')
        return None
    for field in value.keys() - {'environment', 'scope', 'credential_env', 'adhoc', 'recipes', 'real_only'}:
        problem('fixtures.' + field, '格式外字段')
    if not _text(value.get('environment')):
        problem('fixtures.environment', '须为非空文字')
    if not _texts(value.get('scope')):
        problem('fixtures.scope', '须为非空文字数组')
    credentials = value.get('credential_env', [])
    if (not isinstance(credentials, list) or not all(isinstance(v, str) and ENV_NAME.fullmatch(v) for v in credentials)
            or len(set(credentials)) != len(credentials)):
        problem('fixtures.credential_env', '须为不重复的环境变量名数组')
        credentials = []
    if type(value.get('adhoc', False)) is not bool:
        problem('fixtures.adhoc', '须为布尔值')
    real_only = value.get('real_only', [])
    if not _texts(real_only, empty=True):
        problem('fixtures.real_only', '须为文字数组')
        real_only = []
    recipes, seen = value.get('recipes', []), set()
    if not isinstance(recipes, list):
        problem('fixtures.recipes', '须为数组')
        recipes = []
    for index, recipe in enumerate(recipes):
        prefix = f'fixtures.recipes[{index}]'
        if not isinstance(recipe, dict):
            problem(prefix, '配方须为对象')
            continue
        for field in recipe.keys() - {'name', 'title', 'params', 'creates', 'run', 'check'}:
            problem(f'{prefix}.{field}', '格式外字段')
        name = recipe.get('name')
        if not isinstance(name, str) or not RECIPE_NAME.fullmatch(name):
            problem(prefix + '.name', '须只含小写字母、数字与连字符，且以字母或数字开头')
        elif name in seen:
            problem(prefix + '.name', '配方名称不能重复')
        seen.add(name)
        if not _text(recipe.get('title')):
            problem(prefix + '.title', '须为非空文字')
        if not _texts(recipe.get('creates', []), empty=True):
            problem(prefix + '.creates', '须为文字数组')
        params = recipe.get('params', {})
        if (not isinstance(params, dict) or not all(ENV_NAME.fullmatch(k) and _text(v) for k, v in params.items())
                or len({k.upper() for k in params}) != len(params)):
            problem(prefix + '.params', '须为参数名到非空说明的对象；参数名须能转为互不重复的环境变量名')
        for field in ('run', 'check'):
            if (field == 'run' or field in recipe) and not _texts(recipe.get(field)):
                problem(f'{prefix}.{field}', '须为非空命令参数数组，不经过 shell')
    return dict(value, credential_env=credentials, adhoc=value.get('adhoc', False), recipes=recipes, real_only=real_only)


ATTENTION_KINDS = ('人机验证', 'OAuth 授权', '账号登录', '第三方后台设置', '权限放行', '邮箱验证',
                   '环境问题', '前置数据', '浏览器崩溃', '其他')


def open_attention(db, checkout_id, *, local):
    rows = db.execute('SELECT * FROM attention WHERE resolved_at IS NULL AND (checkout IS NULL OR checkout=?) ORDER BY id',
                      (checkout_id,))
    result = []
    for row in rows:
        item = dict(id=row['id'], kind=row['kind'], text=row['text'], scenarios=json.loads(row['scenarios']),
                    created_by=row['created_by'], created_at=row['created_at'])
        if local:
            item['work'] = row['work']
        result.append(item)
    return result
