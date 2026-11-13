// 断言、只读数据查询及其计算来源；仅解析源码，绝不运行候选。
import path from 'node:path';
import fs from 'node:fs';
import {graph, canonical, walk, nameOf, assetSummary, sha, supportFiles, assetFiles, taskAssets} from './spec-assets.mjs';

const [mode, specArg, rootArg, libraryArg, importsArg] = process.argv.slice(2);
const spec = path.resolve(specArg), root = path.resolve(rootArg);
const library = libraryArg === 'library';
// 首次沉淀：复用库的 importRoot 定位方式，只比较 spec 与项目辅助代码。
const promotion = libraryArg === 'promotion';
const importRoot = importsArg || (promotion ? path.dirname(spec) : root);
const extra = libraryArg === 'runtime' ? [...['playwright.config.mjs', 'global-setup.mjs', 'progress-reporter.mjs'].map(name => path.join(root, name)).filter(file => fs.existsSync(file)), ...taskAssets(root)] : library ? assetFiles(importsArg || root) : [];
const sql = /\b(?:SELECT\b[\s\S]*\bFROM\b|WITH\s+\w+\s+AS\s*\(|INSERT\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM)/i;

function analyze() {
  const dataAssets = (promotion ? [] : library ? assetFiles(importRoot) : taskAssets(root))
    .filter(file => !/\.(mjs|cjs|js)(\.off)?$/.test(file) && !file.split(path.sep).join('/').includes('/support/'));
  const bundle = graph(spec, root, {library: library || promotion, importRoot, extra: dataAssets, runtimeFixtures: promotion});
  const items = [], ids = new Set();
  const shim = rec => /^\s*(?:\/\/[^\n]*\n)*export \* from ['"]\.\/library\/[^'"]+['"];\s*$/.test(rec.source);
  function logical(file) {
    const rec = bundle.files.get(file);
    if (rec && shim(rec)) return logical(rec.imports[0].target);
    if (promotion) return file === spec ? '(spec)' : path.relative(importRoot, file).split(path.sep).join('/');
    return file === spec ? '(spec)' : path.relative(library ? (importsArg || root) : root, file).replace(/^scenarios\/(?:library\/)?/, '').replace(/^library\//, '');
  }
  function programOf(rec) {
    const value = canonical(rec.ast.program);
    walk(value, node => {
      const literal = node.source || (node.type === 'CallExpression' && node.callee.type === 'Import' ? node.arguments[0] : null);
      const imported = literal && rec.imports.find(i => i.source === literal.value);
      if (imported?.target) literal.value = '@local:' + logical(imported.target);
    });
    return value;
  }
  function executionShape(value) {
    if (Array.isArray(value)) return value.map(executionShape).filter(v => v !== null);
    if (!value || typeof value !== 'object') return value;
    if (value.type === 'ExpressionStatement') {
      let expression = value.expression;
      if (expression?.type === 'AwaitExpression') expression = expression.argument;
      if (expression?.type === 'CallExpression' && expression.callee.type === 'MemberExpression' && /^to[A-Z]/.test(expression.callee.property.name || '')) {
        let cursor = expression.callee.object;
        while (cursor.type === 'MemberExpression') cursor = cursor.object;
        if (cursor.type === 'CallExpression' && /^expect(?:\.(soft|poll))?$/.test(nameOf(cursor.callee))) return null;
      }
    }
    const out = {};
    for (const [key, child] of Object.entries(canonical(value))) out[key] = executionShape(child);
    return out;
  }
  const execution = [...bundle.files].filter(([file]) => !shim(bundle.files.get(file)))
    .map(([file, rec]) => ({file: promotion ? logical(file) : file === spec ? '(spec)' : path.relative(library ? (importsArg || root) : root, file).replace(/^scenarios\/(?:library\/)?/, '').replace(/^library\//, ''),
      sha256: sha(JSON.stringify(rec.ast ? executionShape(programOf(rec)) : sha(rec.bytes)))}));
  for (const [file, rec] of bundle.files) {
    if (!rec.ast || shim(rec)) continue;
    const relative = promotion ? logical(file) : file === spec ? '(spec)' : path.relative(library ? (importsArg || root) : root, file).replace(/^scenarios\/(?:library\/)?/, '').replace(/^library\//, '');
    let ordinal = 0;
    const bindings = new Map();
    walk(rec.ast.program, (node, parents) => {
      let id, value;
      if (node.type === 'VariableDeclarator' && node.id.type === 'Identifier') { id = node.id.name; value = node.init; }
      if (node.type === 'AssignmentExpression' && node.left.type === 'Identifier') { id = node.left.name; value = node.right; }
      if (node.type === 'FunctionDeclaration' && node.id) { id = node.id.name; value = node; }
      if (id && value) {
        const variable = parents.at(-1)?.type === 'VariableDeclaration' && parents.at(-1).kind === 'var';
        const owner = parents.filter(p => /Function|Method/.test(p.type) || p.type === 'Program'
          || !variable && ['BlockStatement', 'ForStatement', 'ForOfStatement', 'CatchClause'].includes(p.type));
        if (!bindings.has(id)) bindings.set(id, []);
        bindings.get(id).push({node: value, owner});
      }
    });
    function sources(node, parents, seen = new Set()) {
      const out = {};
      walk(node, (n, ps) => {
        if (n.type !== 'Identifier') return;
        const p = ps.at(-1);
        if (p?.type === 'MemberExpression' && p.property === n && !p.computed) return;
        if (p?.type === 'ObjectProperty' && p.key === n && !p.computed && !p.shorthand) return;
        if (seen.has(n.name)) return;
        const defs = (bindings.get(n.name) || []).filter(d => d.owner.every(o => parents.includes(o)));
        if (!defs.length) return;
        const next = new Set([...seen, n.name]);
        out[n.name] = defs.map(d => ({value: canonical(d.node), sources: sources(d.node, [...parents, d.node], next)}));
      });
      return out;
    }
    walk(rec.ast.program, (node, parents) => {
      let kind, subject, matcher, args, negated = false;
      const modifiers = [];
      if (node.type === 'CallExpression' && node.callee.type === 'MemberExpression' && /^to[A-Z]/.test(node.callee.property.name || '')) {
        let cursor = node.callee.object;
        while (cursor.type === 'MemberExpression') { modifiers.push(cursor.property.name); if (cursor.property.name === 'not') negated = !negated; cursor = cursor.object; }
        if (cursor.type === 'CallExpression' && /^expect(?:\.(?:soft|poll))?$/.test(nameOf(cursor.callee))) {
          kind = nameOf(cursor.callee); subject = cursor; matcher = node.callee.property.name; args = node.arguments;
        }
      }
      const call = node.type === 'CallExpression' ? nameOf(node.callee) : '';
      if (!kind && /(?:^|\.)(?:attachSql|query|queryRows|queryOne|executeQuery|readQuery)$/.test(call)) {
        kind = 'data'; subject = node; matcher = call; args = node.arguments;
      }
      if (!kind && ['StringLiteral', 'TemplateLiteral'].includes(node.type)) {
        const value = node.type === 'StringLiteral' ? node.value : node.quasis.map(q => q.value.cooked).join('$' + '{}');
        if (sql.test(value)) { kind = 'sql'; subject = node; matcher = 'SQL'; args = []; }
      }
      if (!kind) return;
      ordinal++;
      const statement = [...parents].reverse().find(p => p.type === 'ExpressionStatement' || p.type === 'VariableDeclaration') || node;
      const before = rec.source.slice(0, statement.start);
      const marker = before.match(/\/\/\s*sop-assert:\s*([A-Za-z0-9_.:-]+)\s*$|\/\*\s*sop-assert:\s*([A-Za-z0-9_.:-]+)\s*\*\/\s*$/);
      const assertion_id = marker ? (marker[1] || marker[2]) + (kind === 'sql' ? ':sql' : '') : 'auto:' + relative + ':' + kind + ':' + ordinal;
      if (ids.has(assertion_id)) throw new Error('重复断言标识：' + assertion_id);
      ids.add(assertion_id);
      const steps = parents.filter(p => p.type === 'CallExpression' && /^(?:step|test.step)$/.test(nameOf(p.callee)))
        .map(p => canonical(p.arguments[nameOf(p.callee) === 'step' ? 1 : 0]));
      const conditions = parents.filter(p => ['IfStatement', 'ConditionalExpression', 'LogicalExpression', 'ForStatement', 'ForOfStatement', 'WhileStatement', 'SwitchCase', 'CatchClause'].includes(p.type))
        .map(p => ({type: p.type, test: canonical(p.test || p.left || p.param), right: canonical(p.right), branch: p.consequent && node.start >= p.consequent.start && node.end <= p.consequent.end ? 'then' : 'else'}));
      const functions = parents.filter(p => /Function|Method/.test(p.type)).map(p => ({type: p.type, name: p.id?.name || p.key?.name || null,
        key: canonical(p.key), async: p.async, params: canonical(p.params)}));
      const preceding = parents.filter(p => Array.isArray(p.body) || p.type === 'SwitchCase').map(p => {
        const statements = (p.type === 'SwitchCase' ? p.consequent : p.body).filter(statement => statement.end <= node.start);
        const shape = executionShape(programOf({...rec, ast: {program: statements}}));
        return {scope: p.type, sha256: sha(JSON.stringify(shape))};
      });
      const awaited = [...parents].reverse().find(p => p.type === 'AwaitExpression' || /Function|Method/.test(p.type))?.type === 'AwaitExpression';
      const data = {kind, matcher, negated, modifiers: modifiers.reverse(), subject: canonical(subject), args: canonical(args), sources: sources(node, parents),
        steps, conditions, functions, preceding, preceding_assertions: items.filter(item => item.file === relative).map(item => item.assertion_id), execution, awaited};
      const nodes = [];
      function texts(value, pointer = '') {
        if (!value || typeof value !== 'object') return;
        if (value.type === 'StringLiteral') nodes.push({node_path: pointer + '/value', value: value.value});
        if (value.type === 'RegExpLiteral') nodes.push({node_path: pointer + '/pattern', value: value.pattern, regex: true});
        if (value.type === 'TemplateElement') nodes.push({node_path: pointer + '/value/cooked', value: value.value?.cooked});
        for (const [k, v] of Object.entries(value)) if (v && typeof v === 'object') {
          if (Array.isArray(v)) v.forEach((c, i) => texts(c, pointer + '/' + k + '/' + i));
          else texts(v, pointer + '/' + k);
        }
      }
      texts(data);
      items.push({assertion_id, file: relative, line: node.loc.start.line, text: rec.source.slice(node.start, node.end),
        signature: data, signature_sha256: sha(JSON.stringify(data)), text_nodes: nodes});
    });
  }
  return {items, files: [...bundle.files].map(([file, rec]) => ({path: path.relative(root, file), sha256: sha(rec.bytes)}))};
}
try {
  let value;
  if (mode === 'analyze') value = analyze();
  else if (mode === 'assets') value = assetSummary(spec, root, {library, importRoot: importsArg || root, extra});
  else if (mode === 'portable') {
    const bundle = graph(spec, root, {library, importRoot: importsArg || root});
    const mapped = new Map(), shims = new Set();
    for (const [file, rec] of bundle.files) {
      const rel = path.relative(library ? (importsArg || root) : root, file).replace(/^scenarios\//, '');
      if (/^(?:fixtures|library)\.mjs$/.test(rel) && /^\s*(?:\/\/[^\n]*\n)*export \* from ['"]\.\/library\/[^'"]+['"];\s*$/.test(rec.source)) shims.add(file);
      else mapped.set(file, file === spec ? '(spec)' : rel.replace(/^library\//, ''));
    }
    function target(file) {
      if (shims.has(file)) return target(bundle.files.get(file).imports[0].target);
      return mapped.get(file);
    }
    const files = {}, binary = {}, origins = {};
    for (const [file, rel] of mapped) {
      let source = bundle.files.get(file).source;
      for (const imp of [...bundle.files.get(file).imports].reverse()) if (imp.target && file !== spec) {
        let next = path.posix.relative(path.posix.dirname(rel), target(imp.target)); if (!next.startsWith('.')) next = './' + next;
        source = source.slice(0, imp.start) + JSON.stringify(next) + source.slice(imp.end);
      }
      if (Object.hasOwn(files, rel) && files[rel] !== source) throw new Error('冻结依赖路径冲突：' + rel);
      if (!bundle.files.get(file).bytes.equals(Buffer.from(bundle.files.get(file).source, 'utf8')) && !bundle.files.get(file).ast)
        binary[rel] = bundle.files.get(file).bytes.toString('base64');
      else files[rel] = source;
      origins[rel] = path.relative(root, file).split(path.sep).join('/');
    }
    value = {files, binary, origins, packages: bundle.packages};
  } else throw new Error('未知操作');
  process.stdout.write(JSON.stringify(value));
} catch (error) { process.stderr.write(error.message + '\n'); process.exitCode = 1; }
