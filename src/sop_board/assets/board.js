// Flow Board 工作台：左栏 文件→节点→场景 树与状态筛选，右栏场景详情、证据、复测标注与历史。
(() => {
  const config = JSON.parse(document.getElementById('board-config').textContent);
  const NAME_KEY = 'sop-board-name';
  const FILTERS = [['retest', '待复测'], ['fail', '失败'], ['blocked', '受阻'], ['distill', '待沉淀'],
    ['pass', '通过'], ['guarded', '守护通过'], ['not-run', '未执行'], ['draft', '草稿']];
  const state = {board: null, filter: null, search: '', selected: null};

  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  };

  const params = () => new URLSearchParams(location.search);
  const checkoutName = () => params().get('checkout') || '';
  const api = async (path, options = {}) => {
    const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
    const body = await response.json().catch(() => ({ok: false, problems: [{problem: `HTTP ${response.status}`}]}));
    if (!response.ok || body.ok === false) throw new Error((body.problems || []).map(p => p.problem).join('；') || `HTTP ${response.status}`);
    return body;
  };
  const query = (extra = {}) => {
    const search = new URLSearchParams({checkout: checkoutName(), ...extra});
    if (!checkoutName()) search.delete('checkout');
    return search.toString();
  };

  function fillCheckouts(checkouts, current) {
    const select = document.getElementById('checkout');
    if (!select) return;
    select.replaceChildren(...checkouts.map(c => el('option', {value: c.name, selected: c.name === current},
      c.name + (c.kind === 'main' ? '（登记路径）' : ''))));
    select.onchange = () => {
      const search = params();
      search.set('checkout', select.value);
      location.search = search.toString() + location.hash;
    };
  }

  // ---- 工作台 ----

  async function loadBoard() {
    state.board = await api('/api/board?' + query());
    fillCheckouts(state.board.checkouts, state.board.checkout);
    renderFilters();
    renderTree();
  }

  function matches(key, item) {
    if (state.filter === 'distill' ? !item.distill : state.filter && item.state !== state.filter) return false;
    const text = state.search.trim().toLowerCase();
    return !text || key.toLowerCase().includes(text) || item.title.toLowerCase().includes(text);
  }

  function renderFilters() {
    const counts = state.board.counts;
    const container = document.getElementById('filters');
    container.replaceChildren(
      el('button', {class: state.filter ? '' : 'active', onclick: () => { state.filter = null; renderFilters(); renderTree(); }},
        `全部 ${state.board.total}`),
      ...FILTERS.filter(([name]) => counts[name] || ['retest', 'fail', 'distill', 'pass', 'guarded', 'not-run', 'draft'].includes(name))
        .map(([name, label]) => el('button', {class: state.filter === name ? 'active' : '', 'data-state': name,
          onclick: () => { state.filter = state.filter === name ? null : name; renderFilters(); renderTree(); }},
          `${label} ${counts[name] || 0}`)));
  }

  function renderTree() {
    const tree = document.getElementById('tree');
    const nodes = [];
    for (const file of state.board.files) {
      if (file.invalid) {
        nodes.push(el('details', {class: 'file invalid', open: true}, el('summary', {}, `${file.name}（文件不合格）`),
          el('ul', {}, (file.problems || []).map(p => el('li', {}, `${p.line ? '第 ' + p.line + ' 行 ' : ''}${p.field}：${p.problem}`)))));
        continue;
      }
      const groups = file.nodes.map(node => {
        const rows = node.scenarios.map(id => `${file.name}#${id}`).filter(key => matches(key, state.board.items[key]));
        if (!rows.length) return null;
        return el('li', {class: 'node'}, el('div', {class: 'node-title'}, `${node.id} ${node.title}`),
          el('ul', {}, rows.map(key => {
            const item = state.board.items[key];
            return el('li', {class: 'scenario' + (state.selected === key ? ' selected' : ''), 'data-key': key,
              onclick: () => select(key)},
              el('span', {class: `dot ${item.state}`, title: item.label}), el('span', {class: 'sid'}, item.id),
              el('span', {class: 'stitle'}, item.title), item.distill ? el('span', {class: 'badge distill'}, '待沉淀') : null);
          })));
      }).filter(Boolean);
      if (!groups.length) continue;
      nodes.push(el('details', {class: 'file', open: true},
        el('summary', {}, file.name + (file.status === 'draft' ? '（草稿）' : '')), el('ul', {}, groups)));
    }
    tree.replaceChildren(...(nodes.length ? nodes : [el('p', {class: 'muted'}, '没有符合条件的场景')]));
  }

  function select(key) {
    state.selected = key;
    if (decodeURIComponent(location.hash.slice(1)) !== key) history.replaceState(null, '', '#' + encodeURIComponent(key).replace(/%23/g, '#'));
    renderTree();
    loadDetail(key);
  }

  function evidenceView(item) {
    if (item.type.startsWith('image/')) return el('a', {href: item.url, target: '_blank'}, el('img', {src: item.url, alt: item.name, loading: 'lazy'}));
    if (item.type.startsWith('video/')) return el('video', {src: item.url, controls: true, preload: 'metadata'});
    if (item.name.endsWith('.zip') && item.name.includes('trace')) {
      const absolute = new URL(item.url, location.href).href;
      return el('span', {}, el('a', {href: item.url}, `下载 ${item.name}`), ' · ',
        el('a', {href: 'https://trace.playwright.dev/?trace=' + encodeURIComponent(absolute), target: '_blank'}, '在 Trace Viewer 中打开'));
    }
    const pre = el('pre', {class: 'text-evidence'}, '…');
    fetch(item.url).then(r => r.text()).then(text => { pre.textContent = text.length > 6000 ? text.slice(0, 6000) + '\n…（已截断，下载查看全文）' : text; });
    return el('div', {}, el('a', {href: item.url, target: '_blank'}, item.name), pre);
  }

  function gallery(items) {
    if (!items?.length) return el('p', {class: 'muted'}, '没有证据');
    return el('div', {class: 'gallery'}, items.map(item => el('figure', {}, evidenceView(item), el('figcaption', {}, item.name))));
  }

  function itemsList(values, part, coverage) {
    return el('ol', {}, values.map((text, index) => {
      const tests = coverage?.[part]?.[index];
      return el('li', {}, text, tests ? el('span', {class: 'badge guard', title: '守护测试：' + tests.join('、')}, '已守护') : null);
    }));
  }

  function historyRow(entry) {
    const pieces = [el('strong', {}, entry.label), ` · ${entry.executed_at}`];
    if (entry.kind === 'manual') pieces.push(` · 复测人 ${entry.marked_by}`);
    else {
      pieces.push(` · ${entry.independence}`);
      if (entry.run) pieces.push(` · 运行 #${entry.run}`);
      if (entry.launch) pieces.push(` · 启动 #${entry.launch}`);
      if (entry.executor) pieces.push(` · 执行者 ${entry.executor}`);
    }
    if (!entry.current) pieces.push(el('span', {class: 'badge old'}, '旧内容'));
    return el('li', {}, el('div', {}, ...pieces),
      entry.finding ? el('div', {class: 'finding'}, entry.finding) : null,
      entry.kept && entry.note ? el('div', {class: 'note'}, '理由：' + entry.note) : null,
      entry.defect ? el('div', {}, '缺陷：', /^https?:/.test(entry.defect) ? el('a', {href: entry.defect, target: '_blank', rel: 'noopener'}, entry.defect) : entry.defect) : null,
      entry.product_changed?.length ? el('div', {class: 'muted'}, '执行期间产品变化：' + entry.product_changed.slice(0, 5).join('、')) : null,
      entry.evidence?.length ? el('details', {}, el('summary', {}, `证据 ${entry.evidence.length} 份`), gallery(entry.evidence)) : null);
  }

  function markForm(data) {
    const name = el('input', {name: 'by', placeholder: '复测人姓名', required: true, value: localStorage.getItem(NAME_KEY) || ''});
    const defect = el('input', {name: 'defect', placeholder: '缺陷链接或编号（不通过时必填）'});
    const note = el('input', {name: 'note', placeholder: '备注（可选）'});
    const message = el('span', {class: 'message'});
    const form = el('form', {class: 'mark', onsubmit: async event => {
      event.preventDefault();
      const verdict = form.querySelector('input[name=verdict]:checked')?.value;
      if (!verdict) { message.textContent = '请选择通过或不通过'; return; }
      if (verdict === 'fail' && !defect.value.trim()) { message.textContent = '不通过时必须填写缺陷链接'; return; }
      try {
        await api('/api/mark', {method: 'POST', body: JSON.stringify({checkout: data.checkout, scenario: data.key, verdict,
          by: name.value.trim(), defect: defect.value.trim() || null, note: note.value.trim() || null})});
        localStorage.setItem(NAME_KEY, name.value.trim());
        await loadBoard();
        await loadDetail(data.key);
      } catch (error) { message.textContent = '提交失败：' + error.message; }
    }},
      el('label', {}, el('input', {type: 'radio', name: 'verdict', value: 'pass'}), '通过'),
      el('label', {}, el('input', {type: 'radio', name: 'verdict', value: 'fail'}), '不通过'),
      defect, name, note, el('button', {type: 'submit'}, '提交复测结论'), message);
    return form;
  }

  function localButtons(data) {
    if (!config.local) return null;
    const message = el('span', {class: 'message'});
    const call = (path, body, done) => async () => {
      message.textContent = '已提交…';
      try { const result = await api(path, {method: 'POST', body: JSON.stringify(body)}); message.textContent = done(result); }
      catch (error) { message.textContent = '失败：' + error.message; }
    };
    return el('div', {class: 'actions'},
      el('button', {onclick: call('/api/launch', {checkout: data.checkout, scenario: data.key},
        r => `已启动无头会话 #${r.launch.id}，结果会回到这里；进度见运行记录`)}, 'AI 重跑此场景'),
      el('button', {onclick: call('/api/retest', {checkout: data.checkout, scenario: data.key},
        r => r.summary || '守护测试已运行')}, '运行守护'),
      el('button', {disabled: !data.state.distill, onclick: call('/api/distill', {checkout: data.checkout, scenario: data.key},
        r => `已启动沉淀会话 #${r.launch.id}`)}, '沉淀'),
      message);
  }

  async function loadDetail(key) {
    const pane = document.getElementById('detail');
    let data;
    try { data = await api('/api/scenario?' + query({key})); }
    catch (error) { pane.replaceChildren(el('p', {class: 'error'}, error.message)); return; }
    const scene = data.scene;
    const latest = data.latest;
    const current = data.history.filter(h => h.current);
    pane.replaceChildren(
      el('h2', {}, `${data.key} ${scene.title}`),
      el('div', {class: 'badges'}, el('span', {class: 'badge'}, scene.priority), el('span', {class: 'badge'}, '入口 ' + scene.verify),
        el('span', {class: 'badge'}, scene.polarity === 'positive' ? '正向' : '反向' + (scene.negative_type ? ' · ' + scene.negative_type : '')),
        el('span', {class: 'badge'}, '角色 ' + scene.actor), scene.draft ? el('span', {class: 'badge distill'}, '草稿') : null),
      el('p', {class: `state ${data.state.state}`}, el('strong', {}, data.state.label), data.state.reason ? '：' + data.state.reason : '',
        data.state.distill ? el('span', {class: 'badge distill'}, '待沉淀') : null),
      el('section', {}, el('h3', {}, '起点'), el('ul', {}, scene.initial_state.map(t => el('li', {}, t)))),
      el('section', {}, el('h3', {}, '步骤'), el('ol', {}, scene.steps.map(t => el('li', {}, t)))),
      el('section', {}, el('h3', {}, '预期'), itemsList(scene.expected, 'expected', data.coverage)),
      el('section', {}, el('h3', {}, '禁止'), itemsList(scene.forbidden_effects, 'forbidden_effects', data.coverage)),
      el('section', {}, el('h3', {}, '最新结果与证据'),
        latest ? el('div', {}, el('p', {}, el('strong', {}, latest.label), ` · ${latest.executed_at} · ${latest.actual || ''}`),
          latest.finding ? el('p', {class: 'finding'}, latest.finding) : null, gallery(latest.evidence))
          : el('p', {class: 'muted'}, '当前内容还没有执行结果')),
      scene.draft ? null : el('section', {}, el('h3', {}, '复测'), markForm(data)),
      localButtons(data),
      el('details', {class: 'history'}, el('summary', {}, `历史（${data.history.length} 条，当前内容 ${current.length} 条）`),
        el('ul', {}, data.history.map(historyRow)),
        data.guard_runs.length ? el('div', {}, el('h4', {}, '守护运行'), el('ul', {}, data.guard_runs.map(run =>
          el('li', {}, `${run.status === 'pass' ? '守护通过' : '守护未通过'} · ${run.ran_at} · ${run.operator}`, run.reason ? ' · ' + run.reason : '')))) : null));
  }

  async function startWorkbench() {
    document.getElementById('search').addEventListener('input', event => { state.search = event.target.value; renderTree(); });
    await loadBoard();
    const key = decodeURIComponent(location.hash.slice(1));
    if (key && state.board.items[key]) select(key);
    window.addEventListener('hashchange', () => {
      const next = decodeURIComponent(location.hash.slice(1));
      if (next && next !== state.selected && state.board.items[next]) select(next);
    });
    setInterval(() => loadBoard().catch(() => {}), 15000);
  }

  // ---- 运行记录 ----

  async function startRuns() {
    const data = await api('/api/runs?' + query());
    const board = await api('/api/board?' + query());
    fillCheckouts(board.checkouts, data.checkout);
    const launchRows = data.launches.map(item => el('tr', {},
      el('td', {}, '#' + item.id), el('td', {}, item.kind === 'run' ? '场景执行' : '回归沉淀'), el('td', {}, item.agent),
      el('td', {}, item.scope.join('、')), el('td', {}, item.status + (item.exit_code !== null && item.exit_code !== undefined ? `（退出码 ${item.exit_code}）` : '')),
      el('td', {}, item.started_at || ''), el('td', {}, item.requested_by),
      config.local ? el('td', {}, el('a', {href: `/api/launches/${item.id}/log`, target: '_blank'}, '日志'), item.session ? ` · ${item.session}` : '') : null));
    document.getElementById('launches').replaceChildren(el('table', {},
      el('thead', {}, el('tr', {}, ['编号', '类型', '代理', '场景', '状态', '开始', '发起人', config.local ? '本机' : null].filter(Boolean).map(t => el('th', {}, t)))),
      el('tbody', {}, launchRows.length ? launchRows : [el('tr', {}, el('td', {colspan: 8}, '没有启动记录'))])));
    const runRows = data.runs.map(item => el('tr', {},
      el('td', {}, '#' + item.id), el('td', {}, item.kind === 'run' ? (item.fix ? `修复 #${item.fix} 的复验` : '执行') : '沉淀'),
      el('td', {}, {prepared: '进行中', submitted: '已提交', abandoned: '已放弃'}[item.status] || item.status),
      el('td', {}, item.scope.join('、')), el('td', {}, `登记 ${item.recorded}` + (item.voided.length ? `，内容变化未登记 ${item.voided.length}` : '') + (item.not_executed.length ? `，未执行 ${item.not_executed.length}` : '')),
      el('td', {}, item.created_at), config.local ? el('td', {}, item.session || '') : null));
    document.getElementById('runs').replaceChildren(el('table', {},
      el('thead', {}, el('tr', {}, ['编号', '类型', '状态', '场景', '结果', '开始', config.local ? '会话' : null].filter(Boolean).map(t => el('th', {}, t)))),
      el('tbody', {}, runRows.length ? runRows : [el('tr', {}, el('td', {colspan: 7}, '没有运行'))])));
  }

  const start = {workbench: startWorkbench, runs: startRuns}[config.page];
  if (start) start().catch(error => document.querySelector('main').prepend(el('p', {class: 'error'}, error.message)));
})();
