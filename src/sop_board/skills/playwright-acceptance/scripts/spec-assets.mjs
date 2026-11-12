// 静态读取 spec 及其依赖的语法树，供 spec-ast.mjs 的保真检查使用；不加载产品模块、不运行 spec。
import fs from 'node:fs';
import path from 'node:path';
import {createRequire, builtinModules} from 'node:module';
import {createHash, randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';

// 基线（sop-board specs check 与沉淀的保真检查）：SOP_SPEC_BASELINE_REPO 指向仓库时，分析目录是 git HEAD 的临时副本，
// 起初只有 spec 本身；跟随本地导入、读取任务资产时，缺的文件按需从 HEAD 取出写进分析目录。只取分析目录之内的路径，不改仓库。
const baselineRepo = process.env.SOP_SPEC_BASELINE_REPO;
function headEntries(relative, recursive) {
  let raw;
  try { raw = execFileSync('git', ['-C', baselineRepo, 'ls-tree', '-z', ...(recursive ? ['-r'] : []), 'HEAD', '--', relative], {maxBuffer: 256 << 20}); }
  catch { return []; }
  return raw.toString('utf8').split('\0').filter(Boolean).map(line => {
    const tab = line.indexOf('\t');
    const [mode, type, object] = line.slice(0, tab).split(' ');
    return {mode, type, object, path: line.slice(tab + 1)};
  });
}
export function fromHead(root, file, {recursive = false} = {}) {
  if (!baselineRepo) return;
  const relative = path.relative(root, file);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative) || (!recursive && fs.existsSync(file))) return;
  for (const entry of headEntries(relative.split(path.sep).join('/'), recursive)) {
    const target = path.join(root, ...entry.path.split('/'));
    if (entry.type !== 'blob' || fs.existsSync(target)) continue;
    const bytes = execFileSync('git', ['-C', baselineRepo, 'cat-file', 'blob', entry.object], {maxBuffer: 256 << 20});
    fs.mkdirSync(path.dirname(target), {recursive: true});
    // HEAD 里的符号链接照样还原成符号链接，由下面的检查同样拒绝。
    if (entry.mode === '120000') fs.symlinkSync(bytes.toString('utf8'), target); else fs.writeFileSync(target, bytes);
  }
}

export const sha = value => createHash('sha256').update(value).digest('hex');
const ignored = new Set(['loc', 'start', 'end', 'extra', 'tokens', 'comments', 'leadingComments', 'trailingComments', 'innerComments']);
export function canonical(node) {
  if (Array.isArray(node)) return node.map(canonical);
  if (!node || typeof node !== 'object') return node;
  if (node.type === 'StringLiteral') return {type: 'StringLiteral', value: node.value};
  const out = {};
  for (const key of Object.keys(node).sort()) if (!ignored.has(key)) out[key] = canonical(node[key]);
  return out;
}
export function walk(node, visit, parents = []) {
  if (!node || typeof node !== 'object') return;
  if (node.type && visit(node, parents) === false) return;
  const next = node.type ? [...parents, node] : parents;
  for (const [key, value] of Object.entries(node)) if (!ignored.has(key)) {
    if (Array.isArray(value)) for (const child of value) walk(child, visit, next);
    else if (value && typeof value === 'object') walk(value, visit, next);
  }
}
export function parser(roots) {
  for (const root of [process.env.PW_NODE_ROOT, ...roots, process.cwd()].filter(Boolean)) {
    try {
      const req = createRequire(path.resolve(root, 'package.json'));
      const packageFile = req.resolve('playwright/package.json');
      const bundle = req(path.join(path.dirname(packageFile), 'lib/transform/babelBundle.js'));
      if (bundle.babelParse) return (source, file) => bundle.babelParse(source, file, true);
    } catch { /* 继续查找当前工作区的依赖，不使用其他工作区的固定路径。 */ }
  }
  throw new Error('找不到当前工作区的 Playwright 语法解析器；在任务或数据目录安装 @playwright/test，或用 PW_NODE_ROOT 指向其项目目录');
}
export function nameOf(node) {
  if (node?.type === 'Identifier') return node.name;
  if (node?.type === 'MemberExpression' && !node.computed) return nameOf(node.object) + '.' + node.property.name;
  return '';
}
export function importsOf(ast, {strict = true} = {}) {
  const imports = [];
  walk(ast.program, node => {
    if (['ImportDeclaration', 'ExportAllDeclaration', 'ExportNamedDeclaration'].includes(node.type) && node.source) {
      imports.push({source: node.source.value, start: node.source.start, end: node.source.end});
    }
    if (node.type === 'CallExpression' && (node.callee.type === 'Import' || nameOf(node.callee) === 'require')) {
      if (node.arguments[0]?.type !== 'StringLiteral') {
        if (strict) throw new Error('执行依赖必须使用静态文字路径，不能使用计算的 import/require');
        imports.push({unresolved: 'computed-import', line: node.loc?.start.line}); return;
      }
      const value = node.arguments[0]; imports.push({source: value.value, start: value.start, end: value.end});
    }
  });
  return imports;
}
const packageCache = new Map();
function packageDigest(packageFile, visiting = new Set()) {
  const root = fs.realpathSync(path.dirname(packageFile));
  if (packageCache.has(root)) return packageCache.get(root);
  if (visiting.has(root)) return 'cycle:' + JSON.parse(fs.readFileSync(packageFile, 'utf8')).name;
  const files = {}, deps = {}, next = new Set([...visiting, root]);
  function scan(directory) {
    for (const item of fs.readdirSync(directory, {withFileTypes: true}).sort((a, b) => a.name.localeCompare(b.name))) {
      if (['node_modules', '.git', '.cache'].includes(item.name)) continue;
      const file = path.join(directory, item.name);
      if (item.isDirectory()) scan(file);
      else files[path.relative(root, file)] = sha(fs.readFileSync(file));
    }
  }
  scan(root);
  const pkg = JSON.parse(fs.readFileSync(packageFile, 'utf8')), req = createRequire(packageFile);
  for (const name of Object.keys(pkg.dependencies || {}).sort()) {
    try { deps[name] = packageDigest(req.resolve(name + '/package.json'), next); }
    catch { deps[name] = 'unavailable'; }
  }
  const value = sha(JSON.stringify({files, deps}));
  packageCache.set(root, value); return value;
}
export function supportFiles(directory) {
  const files = [];
  for (const root of ['scenarios/support', 'scenarios/library/support'].map(name => path.join(directory, name))) {
    function scan(folder) {
      if (!fs.existsSync(folder)) return;
      for (const item of fs.readdirSync(folder, {withFileTypes: true})) {
        const file = path.join(folder, item.name);
        if (item.isDirectory()) scan(file); else files.push(file);
      }
    }
    scan(root);
  }
  return files;
}
export function assetFiles(directory) {
  const files = [];
  function scan(folder) {
    if (!fs.existsSync(folder)) return;
    for (const item of fs.readdirSync(folder, {withFileTypes: true})) {
      if (['node_modules', '.git'].includes(item.name)) continue;
      const file = path.join(folder, item.name);
      if (item.isDirectory()) scan(file); else files.push(file);
    }
  }
  scan(directory); return files;
}
export function taskAssets(directory) {
  return [...new Set(['library', 'pages', 'support', 'data'].flatMap(name => {
    fromHead(directory, path.join(directory, 'scenarios', name), {recursive: true});
    return assetFiles(path.join(directory, 'scenarios', name));
  }))];
}
export function graph(spec, root, {library = false, importRoot = root, extra = [], canonicalSpec = true} = {}) {
  root = path.resolve(root); spec = path.resolve(spec);
  const parse = parser([path.dirname(spec), root]);
  const files = new Map(), packages = {}, unresolved = [];
  function load(file, isSpec = false) {
    file = path.resolve(file);
    if (files.has(file)) return;
    if (canonicalSpec && !file.startsWith(root + path.sep)) throw new Error('执行依赖越出冻结目录：' + file);
    fromHead(root, file);
    let cursor = root;
    for (const part of canonicalSpec ? path.relative(root, file).split(path.sep) : []) {
      cursor = path.join(cursor, part);
      if (fs.lstatSync(cursor).isSymbolicLink()) throw new Error('执行资产不能包含符号链接：' + cursor);
    }
    const bytes = fs.readFileSync(file), source = bytes.toString('utf8');
    const ast = /\.(?:mjs|cjs|js)$/.test(file) ? parse(source, file) : null;
    const imports = ast ? importsOf(ast, {strict: canonicalSpec}) : [];
    files.set(file, {file, bytes, source, ast, imports});
    for (const imp of imports) {
      if (imp.unresolved) { unresolved.push({file: path.relative(root, file), kind: imp.unresolved, line: imp.line}); continue; }
      if (canonicalSpec && isSpec && !['./fixtures.mjs', './library.mjs'].includes(imp.source))
        throw new Error('规范脚本只能经 ./fixtures.mjs 和 ./library.mjs 导入：' + imp.source);
      if (canonicalSpec && (path.isAbsolute(imp.source) || imp.source.startsWith('file:'))) throw new Error('执行资产不允许绝对路径加载：' + imp.source);
      if (!canonicalSpec && /^(?:data|https?):/.test(imp.source)) {
        unresolved.push({file: path.relative(root, file), kind: 'virtual-module', specifier_sha256: sha(imp.source)}); continue;
      }
      if (!imp.source.startsWith('.') && !path.isAbsolute(imp.source) && !imp.source.startsWith('file:')) {
        if (imp.source.startsWith('node:') || builtinModules.includes(imp.source)) continue;
        const packageName = imp.source.startsWith('@') ? imp.source.split('/').slice(0, 2).join('/') : imp.source.split('/')[0];
        let packageFile;
        for (const base of [file, path.join(process.env.PW_NODE_ROOT || root, 'package.json')]) {
          try { packageFile = createRequire(base).resolve(packageName + '/package.json'); break; } catch {}
        }
        if (!packageFile) {
          if (canonicalSpec) throw new Error('未安装执行依赖：' + packageName);
          unresolved.push({file: path.relative(root, file), kind: 'unresolved-package', name: packageName}); continue;
        }
        packages[packageName] = packageDigest(packageFile);
        continue;
      }
      imp.target = imp.source.startsWith('file:') ? fileURLToPath(imp.source) : path.resolve(library && isSpec ? importRoot : path.dirname(file), imp.source);
      load(imp.target);
    }
  }
  load(spec, true);
  for (const file of extra) if (fs.existsSync(file)) load(file);
  return {files, packages, unresolved};
}
export function assetSummary(spec, root, options) {
  const result = graph(spec, root, options), files = {};
  for (const [file, row] of [...result.files].sort()) files[path.relative(root, file).split(path.sep).join('/')] = sha(row.bytes);
  const summary = {spec: path.relative(root, spec).split(path.sep).join('/'), files, packages: result.packages};
  if (result.unresolved.length) summary.unresolved = result.unresolved;
  return {...summary, candidate_sha256: sha(JSON.stringify(summary))};
}
