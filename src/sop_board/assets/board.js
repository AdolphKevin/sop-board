// Flow Board 页面脚本。
// - 顶栏署名：姓名在页面上填一次，存 localStorage，所有页面与场景共用，使用者主动修改才变。
// - 工作台：左栏 文件→节点→场景 树（展开状态按项目记在 localStorage；点击场景只切换右栏，不重建树、不动滚动位置），
//   右栏四要素、证据、复测（通过 / 不通过 / 阻塞 / 需求变更，缺陷链接与姓名都可不填）与历史。
// - 业务 SOP：左栏 文档→章节→问题 树，右栏显示所选一节；待确认问题按“答复”小节标出已答复 / 待答复。
(() => {
  const config = JSON.parse(document.getElementById('board-config').textContent);
  const NAME_KEY = 'sop-board-name';
  const TREE_KEY = `sop-board-tree:${config.project}:${config.page}`;

  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  };
  const $ = selector => document.querySelector(selector);

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
  const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body)});

  function fillCheckouts(checkouts, current) {
    const select = $('#checkout');
    if (!select) return;
    select.replaceChildren(...checkouts.map(c => el('option', {value: c.name, selected: c.name === current},
      c.name + (c.kind === 'main' ? '（主检出）' : ''))));
    select.onchange = () => {
      const search = params();
      search.set('checkout', select.value);
      location.search = search.toString() + location.hash;
    };
  }

  // ---- 署名 ----

  const storedName = () => (localStorage.getItem(NAME_KEY) || '').trim();
  const displayName = () => storedName() || (config.local && config.operator ? config.operator : '');

  function renderSigner() {
    const host = $('#signer');
    if (!host) return;
    const name = displayName();
    host.replaceChildren(el('button', {class: 'signer-button', type: 'button', title: '修改署名', onclick: openSigner},
      el('span', {class: 'signer-label'}, '署名'), el('strong', {}, name || '未填写'),
      !storedName() && name ? el('span', {class: 'muted'}, '（本机操作者）') : null));
    for (const line of document.querySelectorAll('[data-signer]')) fillSignerLine(line);
  }

  function fillSignerLine(line) {
    const name = displayName();
    line.replaceChildren(...[name ? '署名 ' : '未署名', name ? el('strong', {}, name) : null,
      el('button', {type: 'button', class: 'link', onclick: openSigner}, name ? '修改' : '填写姓名')].filter(Boolean));
  }

  function openSigner() {
    const host = $('#signer');
    if (!host || host.querySelector('.signer-editor')) return;
    const input = el('input', {type: 'text', maxlength: 64, value: storedName(), placeholder: '你的姓名', 'aria-label': '署名'});
    const save = () => {
      const value = input.value.trim();
      if (value) localStorage.setItem(NAME_KEY, value); else localStorage.removeItem(NAME_KEY);
      renderSigner();
    };
    const editor = el('form', {class: 'signer-editor', onsubmit: event => { event.preventDefault(); save(); }},
      el('label', {}, '姓名填一次，所有页面与场景都沿用'), input,
      el('div', {class: 'row-actions'}, el('button', {type: 'submit', class: 'primary'}, '保存'),
        el('button', {type: 'button', onclick: renderSigner}, '取消')));
    host.append(editor);
    input.focus();
    input.select();
  }

  // ---- 通用：可折叠树的展开状态 ----

  const openState = (() => {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(TREE_KEY) || '{}'); } catch { saved = {}; }
    return {
      get: (key, fallback) => (key in saved ? saved[key] : fallback),
      set: (key, value) => { saved[key] = value; localStorage.setItem(TREE_KEY, JSON.stringify(saved)); },
    };
  })();

  function group(key, fallback, filtering, summary, children, className) {
    const details = el('details', {class: className, open: filtering ? true : openState.get(key, fallback)},
      el('summary', {}, el('span', {class: 'chevron', 'aria-hidden': 'true'}), summary), children);
    details.addEventListener('toggle', () => { if (!details.dataset.filtering) openState.set(key, details.open); });
    if (filtering) details.dataset.filtering = '1';
    return details;
  }

  function keepScroll(container, render) {
    const top = container.scrollTop;
    render();
    container.scrollTop = top;
  }

  // 折叠的 details 内容在新版 Chrome 中仍有 offsetParent，用 checkVisibility 判断是否真的可见。
  const shown = node => (node.checkVisibility ? node.checkVisibility() : node.offsetParent !== null);

  function moveSelection(tree, current, step) {
    const rows = [...tree.querySelectorAll('.selectable')].filter(shown);
    const index = rows.indexOf(current);
    const next = rows[index + step];
    if (next) { next.focus(); next.click(); next.scrollIntoView({block: 'nearest'}); }
  }

  const pill = (state, label) => el('span', {class: `pill ${state}`}, label);

  // ---- 工作台 ----

  const LABELS = {'retest': '待复测', 'fail': '失败', 'blocked': '受阻', 'changed': '需求变更', 'distill': '待沉淀', 'review': '待人工复核',
    'not-run': '未执行', 'pass': '通过', 'guarded': '守护通过', 'skip': '跳过', 'draft': '草稿'};
  const VERDICTS = [['pass', '通过'], ['fail', '不通过'], ['blocked', '阻塞'], ['changed', '需求变更']];
  const BADGES = ['review', 'distill'];
  const board = {data: null, signature: '', filter: null, search: '', selected: null};

  async function loadBoard() {
    const data = await api('/api/board?' + query());
    const signature = JSON.stringify([data.counts, data.files.map(f => [f.name, f.invalid, f.nodes]),
      Object.entries(data.items).map(([k, v]) => [k, v.state, v.distill, v.review, v.title]), data.stats]);
    board.data = data;
    if (signature === board.signature) return;
    const first = !board.signature;
    board.signature = signature;
    if (first) fillCheckouts(data.checkouts, data.checkout);
    renderFilters();
    renderTree();
    if (!first && !board.selected) renderOverview();
  }

  function matches(key, item) {
    const badge = BADGES.includes(board.filter);
    if (badge ? !item[board.filter] : board.filter && item.state !== board.filter) return false;
    const text = board.search.trim().toLowerCase();
    return !text || key.toLowerCase().includes(text) || item.title.toLowerCase().includes(text);
  }

  function renderFilters() {
    const counts = board.data.counts;
    const chip = (name, label, count) => el('button', {type: 'button', class: 'filter' + (board.filter === name ? ' active' : ''),
      onclick: () => { board.filter = board.filter === name ? null : name; renderFilters(); renderTree(); }},
      name ? el('span', {class: `dot ${name}`}) : null, label, el('span', {class: 'count'}, count));
    const always = ['retest', 'fail', 'review', 'not-run', 'pass'];
    $('#filters').replaceChildren(chip(null, '全部', board.data.total),
      ...config.states.concat(BADGES).filter(name => counts[name] || always.includes(name) || board.filter === name)
        .map(name => chip(name, LABELS[name] || name, counts[name] || 0)));
  }

  function scenarioRow(key, item) {
    return el('div', {class: 'row scenario selectable' + (board.selected === key ? ' selected' : ''), role: 'treeitem', tabindex: 0,
      'data-key': key, title: item.label + (item.reason ? '：' + item.reason : ''),
      onclick: () => select(key), onkeydown: event => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); moveSelection($('#tree'), event.currentTarget, event.key === 'ArrowDown' ? 1 : -1); }
      }},
      el('span', {class: `dot ${item.state}`}), el('span', {class: 'sid'}, item.id), el('span', {class: 'stitle'}, item.title),
      item.review ? el('span', {class: 'tag review', title: '待人工复核'}, '核') : null,
      item.distill ? el('span', {class: 'tag distill', title: '待沉淀'}, '沉') : null);
  }

  function renderTree() {
    const tree = $('#tree');
    const filtering = Boolean(board.filter || board.search.trim());
    keepScroll(tree, () => {
      const files = [];
      for (const file of board.data.files) {
        if (file.invalid) {
          if (filtering) continue;
          files.push(group('f:' + file.name, true, false,
            el('span', {class: 'group-label invalid'}, file.name, el('span', {class: 'tag fail'}, '不合格')),
            el('ul', {class: 'problems'}, (file.problems || []).map(p => el('li', {}, `${p.line ? '第 ' + p.line + ' 行 ' : ''}${p.field}：${p.problem}`))),
            'group file'));
          continue;
        }
        let fileCount = 0;
        // 场景少的文件缺省展开节点；大文件缺省只列节点，像目录一样逐个展开。
        const smallFile = file.nodes.reduce((sum, node) => sum + node.scenarios.length, 0) <= 40;
        const nodes = file.nodes.map(node => {
          const keys = node.scenarios.map(id => `${file.name}#${id}`).filter(key => board.data.items[key] && matches(key, board.data.items[key]));
          if (!keys.length) return null;
          fileCount += keys.length;
          return group(`n:${file.name}#${node.id}`, smallFile, filtering,
            el('span', {class: 'group-label'}, el('span', {class: 'nid'}, node.id), el('span', {class: 'ntitle'}, node.title),
              el('span', {class: 'count'}, keys.length)),
            el('div', {class: 'rows'}, keys.map(key => scenarioRow(key, board.data.items[key]))), 'group node');
        }).filter(Boolean);
        if (!nodes.length) continue;
        files.push(group('f:' + file.name, true, filtering,
          el('span', {class: 'group-label'}, el('span', {class: 'fname'}, file.name),
            file.status === 'draft' ? el('span', {class: 'tag draft'}, '草稿') : null, el('span', {class: 'count'}, fileCount)),
          el('div', {class: 'nodes'}, nodes), 'group file'));
      }
      tree.replaceChildren(...(files.length ? files : [el('p', {class: 'muted pad'}, '没有符合条件的场景')]));
    });
  }

  function reveal(key) {
    const item = board.data.items[key];
    if (!item) return;
    openState.set('f:' + item.file, true);
    openState.set(`n:${item.file}#${item.node}`, true);
    renderTree();
    $('#tree').querySelector(`[data-key="${CSS.escape(key)}"]`)?.scrollIntoView({block: 'center'});
  }

  function select(key, {fromHash = false} = {}) {
    const tree = $('#tree');
    tree.querySelector('.scenario.selected')?.classList.remove('selected');
    board.selected = key;
    const row = tree.querySelector(`[data-key="${CSS.escape(key)}"]`);
    row?.classList.add('selected');
    if (fromHash && !(row && shown(row))) reveal(key);
    if (decodeURIComponent(location.hash.slice(1)) !== key) history.replaceState(null, '', '#' + encodeURIComponent(key).replace(/%23/g, '#'));
    loadDetail(key);
  }

  function evidenceView(item) {
    if (item.type.startsWith('image/')) return el('a', {href: item.url, target: '_blank', class: 'thumb'}, el('img', {src: item.url, alt: item.name, loading: 'lazy'}));
    if (item.type.startsWith('video/')) return el('video', {src: item.url, controls: true, preload: 'metadata'});
    if (item.name.endsWith('.zip') && item.name.includes('trace')) {
      const absolute = new URL(item.url, location.href).href;
      return el('div', {class: 'file-evidence'}, el('a', {href: item.url}, '下载 trace'), el('a', {href: 'https://trace.playwright.dev/?trace=' + encodeURIComponent(absolute), target: '_blank'}, '在 Trace Viewer 中打开'));
    }
    const pre = el('pre', {class: 'text-evidence'}, '…');
    fetch(item.url).then(r => r.text()).then(text => { pre.textContent = text.length > 6000 ? text.slice(0, 6000) + '\n…（已截断，下载查看全文）' : text; });
    return el('div', {class: 'file-evidence'}, el('a', {href: item.url, target: '_blank'}, '打开原文'), pre);
  }

  function gallery(items) {
    if (!items?.length) return el('p', {class: 'muted'}, '没有证据');
    const media = items.filter(i => i.type.startsWith('image/') || i.type.startsWith('video/'));
    const others = items.filter(i => !media.includes(i));
    return el('div', {class: 'evidence'},
      media.length ? el('div', {class: 'gallery'}, media.map(item => el('figure', {}, evidenceView(item), el('figcaption', {}, item.name)))) : null,
      others.map(item => el('details', {class: 'evidence-file'}, el('summary', {}, item.name), evidenceView(item))));
  }

  function numbered(values, part, coverage, kind) {
    if (!values?.length) return el('p', {class: 'muted'}, '无');
    return el('ol', {class: `numbered ${kind}`}, values.map((text, index) => {
      const tests = coverage?.[part]?.[index];
      return el('li', {}, el('span', {class: 'text'}, text),
        tests ? el('span', {class: 'tag guard', title: '守护测试：' + tests.join('、')}, '已守护') : null);
    }));
  }

  const section = (title, ...content) => el('section', {class: 'block'}, el('h3', {}, title), ...content);

  function stateOf(entry) {
    if (entry.kind === 'manual') return {pass: 'pass', fail: 'fail', blocked: 'blocked', changed: 'changed'}[entry.status];
    if (entry.kept) return 'pass';
    return entry.status;
  }

  function historyItem(entry) {
    const who = entry.kind === 'manual' ? `复测人 ${entry.marked_by}` :
      [entry.independence, entry.run ? `运行 #${entry.run}` : null, entry.launch ? `启动 #${entry.launch}` : null,
        entry.executor ? `执行者 ${entry.executor}` : null].filter(Boolean).join(' · ');
    return el('li', {class: 'event'},
      el('div', {class: 'event-head'}, pill(stateOf(entry), entry.label), el('span', {class: 'muted'}, entry.executed_at),
        el('span', {class: 'muted'}, who), entry.current ? null : el('span', {class: 'tag old'}, '旧内容')),
      entry.finding ? el('p', {class: 'finding'}, entry.finding) : null,
      entry.note ? el('p', {class: 'note'}, (entry.kept ? '理由：' : '说明：') + entry.note) : null,
      entry.defect ? el('p', {}, '缺陷：', /^https?:/.test(entry.defect) ? el('a', {href: entry.defect, target: '_blank', rel: 'noopener'}, entry.defect) : entry.defect) : null,
      entry.product_changed?.length ? el('p', {class: 'muted'}, '执行期间产品变化：' + entry.product_changed.slice(0, 5).join('、')) : null,
      entry.evidence?.length ? el('details', {class: 'event-evidence'}, el('summary', {}, `证据 ${entry.evidence.length} 份`), gallery(entry.evidence)) : null);
  }

  function markCard(data) {
    let verdict = null;
    const defect = el('input', {type: 'text', name: 'defect', placeholder: '缺陷链接或编号'});
    const note = el('textarea', {name: 'note', rows: 3, placeholder: '例如：阻塞原因、需求怎样变了'});
    const defectField = el('label', {class: 'field', hidden: true}, el('span', {}, '缺陷链接', el('em', {}, '可选')), defect);
    const noteField = el('label', {class: 'field'}, el('span', {}, '说明', el('em', {}, '可选')), note);
    const message = el('p', {class: 'message'});
    const submit = el('button', {type: 'submit', class: 'primary block', disabled: true}, '提交复测结论');
    const choices = VERDICTS.map(([value, label]) => el('button', {type: 'button', class: `verdict ${value}`, role: 'radio', 'aria-checked': 'false',
      onclick: event => {
        verdict = value;
        for (const button of choices) button.setAttribute('aria-checked', String(button === event.currentTarget));
        defectField.hidden = !(value === 'fail' || value === 'blocked');
        submit.disabled = false;
        message.textContent = '';
      }}, label));
    const signer = el('p', {class: 'signed-as', 'data-signer': ''});
    fillSignerLine(signer);
    const form = el('form', {class: 'stack', onsubmit: async event => {
      event.preventDefault();
      if (!verdict) return;
      submit.disabled = true;
      message.textContent = '提交中…';
      try {
        await post('/api/mark', {checkout: data.checkout, scenario: data.key, verdict, by: storedName() || null,
          defect: defectField.hidden ? null : defect.value.trim() || null, note: note.value.trim() || null});
        await loadBoard();
        await loadDetail(data.key, {keep: true});
      } catch (error) { message.textContent = '提交失败：' + error.message; submit.disabled = false; }
    }}, el('div', {class: 'verdicts', role: 'radiogroup', 'aria-label': '复测结论'}, choices), defectField, noteField, signer, submit, message);
    return el('div', {class: 'card'}, el('h3', {}, '复测'), el('p', {class: 'muted small'}, '按左侧四要素操作后登记结论；缺陷链接与姓名都可不填。'), form);
  }

  function annotationCard(data) {
    const text = el('textarea', {name: 'text', rows: 4, placeholder: '例如：预期金额应为 99.00 元', required: true});
    const message = el('p', {class: 'message'});
    const signer = el('p', {class: 'signed-as', 'data-signer': ''});
    fillSignerLine(signer);
    const list = data.annotations?.length ? el('ul', {class: 'notes'}, data.annotations.map(a => el('li', {},
      el('div', {}, a.text), el('div', {class: 'muted small'}, `#${a.id} · ${a.author} · ${a.created_at} · ${a.status === 'open' ? '待处理' : a.status === 'adopted' ? '已采纳' : '未采纳'}`))))
      : el('p', {class: 'muted small'}, '还没有批注');
    return el('div', {class: 'card'}, el('h3', {}, '草稿批注'), list,
      el('form', {class: 'stack', onsubmit: async event => {
        event.preventDefault();
        if (!displayName()) { message.textContent = '请先填写姓名（右上角“署名”）'; openSigner(); return; }
        try {
          await post('/api/annotate', {checkout: data.checkout, file: data.scene.file, target: data.scene.id, text: text.value.trim(), by: displayName()});
          await loadDetail(data.key, {keep: true});
        } catch (error) { message.textContent = '提交失败：' + error.message; }
      }}, el('label', {class: 'field'}, el('span', {}, '批注内容'), text), signer, el('button', {type: 'submit', class: 'primary block'}, '提交批注'), message));
  }

  function localCard(data) {
    if (!config.local) return null;
    const message = el('p', {class: 'message'});
    const call = (path, body, done) => async event => {
      const button = event.currentTarget;
      button.disabled = true;
      message.textContent = '已提交…';
      try { message.textContent = done(await post(path, body)); }
      catch (error) { message.textContent = '失败：' + error.message; }
      finally { button.disabled = false; }
    };
    const action = (label, hint, handler, disabled) => el('button', {type: 'button', class: 'action', disabled, onclick: handler},
      el('strong', {}, label), el('span', {}, hint));
    return el('div', {class: 'card'}, el('h3', {}, '本机操作'),
      el('div', {class: 'actions'},
        action('AI 重跑此场景', '启动无头会话经真实入口执行', call('/api/launch', {checkout: data.checkout, scenario: data.key},
          r => `已启动无头会话 #${r.launch.id}，结果会回到这里；进度见运行记录`)),
        action('运行守护', '直接运行守护测试，不启动 AI', call('/api/retest', {checkout: data.checkout, scenario: data.key},
          r => r.summary || '守护测试已运行')),
        action('沉淀', data.state.distill ? '启动沉淀会话写守护测试' : '需要先有独立真实入口通过', call('/api/distill', {checkout: data.checkout, scenario: data.key},
          r => `已启动沉淀会话 #${r.launch.id}`), !data.state.distill)),
      message);
  }

  async function loadDetail(key, {keep = false} = {}) {
    const pane = $('#detail');
    let data;
    try { data = await api('/api/scenario?' + query({key})); }
    catch (error) { pane.replaceChildren(el('div', {class: 'empty'}, el('p', {class: 'finding'}, error.message))); return; }
    if (board.selected !== key) return;
    const scene = data.scene;
    const latest = data.latest;
    const current = data.history.filter(h => h.current);
    const top = pane.scrollTop;
    const main = el('div', {class: 'main'},
      el('div', {class: 'crumbs'}, scene.file, el('span', {}, '›'), `${scene.node} ${scene.node_title || ''}`),
      el('h1', {}, scene.title),
      el('div', {class: 'meta'}, el('span', {class: 'sid big'}, data.key),
        el('span', {class: 'chip'}, scene.priority), el('span', {class: 'chip'}, '入口 ' + scene.verify),
        el('span', {class: 'chip'}, scene.polarity === 'positive' ? '正向' : '反向' + (scene.negative_type ? ' · ' + scene.negative_type : '')),
        el('span', {class: 'chip'}, '角色 ' + scene.actor), scene.draft ? pill('draft', '草稿') : null),
      el('div', {class: `banner ${data.state.state}`}, pill(data.state.state, data.state.label),
        el('span', {class: 'reason'}, data.state.reason || ''), data.state.review ? el('span', {class: 'tag review'}, '待人工复核') : null,
        data.state.distill ? el('span', {class: 'tag distill'}, '待沉淀') : null),
      section('起点', scene.initial_state?.length ? el('ul', {class: 'plain'}, scene.initial_state.map(t => el('li', {}, t))) : el('p', {class: 'muted'}, '无')),
      section('步骤', numbered(scene.steps, null, null, 'steps')),
      section('预期', numbered(scene.expected, 'expected', data.coverage, 'expected')),
      section('禁止', numbered(scene.forbidden_effects, 'forbidden_effects', data.coverage, 'forbidden')),
      section('最新执行结果与证据', latest ? el('div', {},
        el('div', {class: 'event-head'}, pill(stateOf(latest), latest.label), el('span', {class: 'muted'}, latest.executed_at)),
        latest.actual ? el('p', {}, latest.actual) : null, latest.finding ? el('p', {class: 'finding'}, latest.finding) : null,
        gallery(latest.evidence)) : el('p', {class: 'muted'}, '当前内容还没有执行结果')),
      el('details', {class: 'block history'}, el('summary', {}, el('h3', {}, '历史'), el('span', {class: 'muted'}, `${data.history.length} 条，当前内容 ${current.length} 条`)),
        data.history.length ? el('ul', {class: 'timeline'}, data.history.map(historyItem)) : el('p', {class: 'muted'}, '没有记录'),
        data.guard_runs.length ? el('div', {}, el('h4', {}, '守护运行'), el('ul', {class: 'timeline'}, data.guard_runs.map(run =>
          el('li', {class: 'event'}, el('div', {class: 'event-head'}, pill(run.status === 'pass' ? 'guarded' : 'fail', run.status === 'pass' ? '守护通过' : '守护未通过'),
            el('span', {class: 'muted'}, run.ran_at), el('span', {class: 'muted'}, run.operator)), run.reason ? el('p', {}, run.reason) : null)))) : null));
    const side = el('aside', {class: 'side'}, scene.draft ? annotationCard(data) : markCard(data), localCard(data));
    pane.replaceChildren(el('div', {class: 'detail-grid'}, main, side));
    pane.scrollTop = keep ? top : 0;
  }

  // 百分比与报告一致：恰为 .5 时取偶数（同 Python 的 {:.0%}）。
  const percent = value => { const x = value * 100, r = Math.round(x); return r - x === 0.5 && r % 2 ? r - 1 : r; };
  const ratio = item => item.denominator ? `${item.numerator}/${item.denominator}（${percent(item.value)}%）` : '—';

  function renderOverview() {
    const pane = $('#detail');
    const {counts, total, items} = board.data;
    const tile = (state, label) => el('button', {type: 'button', class: `tile ${state}`,
      onclick: () => { board.filter = state; renderFilters(); renderTree(); }}, el('strong', {}, counts[state] || 0), el('span', {}, label));
    const urgent = Object.entries(items).filter(([, item]) => ['retest', 'fail', 'blocked', 'changed'].includes(item.state));
    pane.replaceChildren(el('div', {class: 'overview'},
      el('div', {}, el('h1', {}, `检出 ${board.data.checkout} · ${total} 条场景`),
        el('p', {class: 'muted'}, board.data.product.dirty ? '产品有未提交改动；结果按当前内容快照记录。' : '从左侧选择场景查看四要素、证据并复测。')),
      el('div', {class: 'tiles'}, [['retest', '待复测'], ['fail', '失败'], ['blocked', '受阻'], ['changed', '需求变更'],
        ['not-run', '未执行'], ['pass', '通过'], ['guarded', '守护通过'], ['review', '待人工复核'], ['distill', '待沉淀']].map(([s, l]) => tile(s, l))),
      el('p', {class: 'muted metrics'}, `Agent 独立通过的人工确认率 ${ratio(board.data.stats.agent_confirm_rate)} · 复核覆盖 ${ratio(board.data.stats.agent_review_coverage)}`),
      el('section', {class: 'block'}, el('h3', {}, `需要处理（${urgent.length}）`),
        urgent.length ? el('ul', {class: 'todo'}, urgent.map(([key, item]) => el('li', {},
          el('div', {class: 'row selectable', onclick: () => select(key, {fromHash: true})},
            el('span', {class: `dot ${item.state}`}), el('span', {class: 'sid'}, key), el('span', {class: 'stitle'}, item.title),
            el('span', {class: 'reason'}, item.label + (item.reason ? '：' + item.reason : ''))))))
          : el('p', {class: 'muted'}, '没有待复测、失败、受阻或需求变更的场景'))));
  }

  async function startWorkbench() {
    $('#search').addEventListener('input', event => { board.search = event.target.value; renderTree(); });
    await loadBoard();
    const key = decodeURIComponent(location.hash.slice(1));
    if (key && board.data.items[key]) select(key, {fromHash: true});
    else renderOverview();
    window.addEventListener('hashchange', () => {
      const next = decodeURIComponent(location.hash.slice(1));
      if (next && next !== board.selected && board.data.items[next]) select(next, {fromHash: true});
    });
    setInterval(() => loadBoard().catch(() => {}), 15000);
  }

  // ---- 业务 SOP ----

  const docs = {data: null, filter: null, search: '', selected: null};
  const ANSWER = {answered: ['pass', '已答复'], open: ['retest', '待答复']};

  function docOutline(doc) {
    // 把节组织成：概览（0、1 级）+ 2 级分组（含其下 3 级）+ 无上级的 3 级。
    const overview = [], items = [];
    let parent = null;
    doc.sections.forEach((section, index) => {
      if (section.level <= 1) { overview.push(index); parent = null; return; }
      if (section.level === 2) { parent = {index, children: []}; items.push(parent); return; }
      if (parent) parent.children.push(index); else items.push({index, children: []});
    });
    return {overview, items};
  }

  const sectionKey = (doc, index) => `${doc.name}::${doc.sections[index].anchor}`;
  const shortTitle = title => title.replace(/^标题[:：]\s*/, '');

  function docMatches(doc, index) {
    const section = doc.sections[index];
    if (docs.filter && section.answer !== docs.filter) return false;
    const text = docs.search.trim().toLowerCase();
    return !text || section.title.toLowerCase().includes(text);
  }

  function docRow(doc, index, label) {
    const section = doc.sections[index];
    const key = sectionKey(doc, index);
    const answer = ANSWER[section.answer];
    return el('div', {class: 'row selectable' + (docs.selected === key ? ' selected' : ''), role: 'treeitem', tabindex: 0, 'data-key': key,
      onclick: () => selectSection(key), onkeydown: event => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); moveSelection($('#tree'), event.currentTarget, event.key === 'ArrowDown' ? 1 : -1); }
      }},
      answer ? el('span', {class: `dot ${answer[0]}`, title: answer[1]}) : el('span', {class: 'dot hollow'}),
      el('span', {class: 'stitle'}, label || shortTitle(section.title)));
  }

  function renderDocFilters() {
    const all = docs.data.documents.flatMap(doc => doc.sections.filter(s => s.answer));
    const host = $('#filters');
    if (!all.length) { host.replaceChildren(); return; }
    const count = state => all.filter(s => s.answer === state).length;
    const chip = (state, label, n) => el('button', {type: 'button', class: 'filter' + (docs.filter === state ? ' active' : ''),
      onclick: () => { docs.filter = docs.filter === state ? null : state; renderDocFilters(); renderDocTree(); }},
      state ? el('span', {class: `dot ${ANSWER[state][0]}`}) : null, label, el('span', {class: 'count'}, n));
    host.replaceChildren(chip(null, '全部问题', all.length), chip('open', '待答复', count('open')), chip('answered', '已答复', count('answered')));
  }

  function renderDocTree() {
    const tree = $('#tree');
    const filtering = Boolean(docs.filter || docs.search.trim());
    keepScroll(tree, () => {
      const groups = [];
      for (const doc of docs.data.documents) {
        const {overview, items} = docOutline(doc);
        const rows = [];
        if (overview.length && !filtering) rows.push(docRow(doc, overview[0], '概览'));
        for (const item of items) {
          const children = item.children.filter(index => docMatches(doc, index));
          if (!item.children.length) {
            if (docMatches(doc, item.index)) rows.push(docRow(doc, item.index));
            continue;
          }
          if (!children.length && !(docMatches(doc, item.index) && !docs.filter)) continue;
          const section = doc.sections[item.index];
          const head = el('span', {class: 'group-label'}, el('span', {class: 'ntitle'}, shortTitle(section.title)), el('span', {class: 'count'}, children.length));
          const node = group(`s:${doc.name}::${section.anchor}`, false, filtering, head,
            el('div', {class: 'rows'}, children.map(index => docRow(doc, index))), 'group node');
          const summary = node.querySelector('summary');
          summary.classList.add('selectable');
          summary.dataset.key = sectionKey(doc, item.index);
          if (docs.selected === summary.dataset.key) summary.classList.add('selected');
          head.addEventListener('click', event => { event.preventDefault(); node.open = true; selectSection(summary.dataset.key); });
          rows.push(node);
        }
        if (!rows.length) continue;
        const questions = doc.sections.filter(s => s.answer);
        const open = questions.filter(s => s.answer === 'open').length;
        groups.push(group('d:' + doc.name, false, filtering,
          el('span', {class: 'group-label'}, el('span', {class: 'fname'}, doc.name.replace(/\.md$/, '')),
            questions.length ? el('span', {class: 'tag ' + (open ? 'retest' : 'pass')}, open ? `待答复 ${open}` : `已答复 ${questions.length}`) : null),
          el('div', {class: 'nodes'}, doc.problem ? el('p', {class: 'finding'}, doc.problem) : rows), 'group file'));
      }
      tree.replaceChildren(...(groups.length ? groups : [el('p', {class: 'muted pad'}, docs.data.documents.length ? '没有符合条件的章节' : '场景目录下没有业务 SOP（.md）')]));
    });
  }

  function findSection(key) {
    for (const doc of docs.data.documents) {
      const index = doc.sections.findIndex(s => `${doc.name}::${s.anchor}` === key);
      if (index >= 0) return {doc, index};
    }
    return null;
  }

  function sectionRange(doc, index) {
    const level = doc.sections[index].level;
    let end = index + 1;
    if (level <= 1) {
      while (end < doc.sections.length && doc.sections[end].level <= 1) end++;
    } else {
      while (end < doc.sections.length && doc.sections[end].level > level) end++;
    }
    return doc.sections.slice(index, end);
  }

  async function renderMermaid(container) {
    if (!window.mermaid) return;
    if (!renderMermaid.ready) {
      // 禁止图源改变安全设置、注入主题 CSS 或启用 HTML 标签。
      window.mermaid.initialize({startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true, flowchart: {htmlLabels: false},
        secure: ['secure', 'securityLevel', 'startOnLoad', 'suppressErrorRendering', 'flowchart', 'themeCSS', 'themeVariables', 'fontFamily']});
      renderMermaid.ready = true;
    }
    for (const node of container.querySelectorAll('pre.mermaid:not([data-processed])')) {
      const original = node.textContent;
      try { await window.mermaid.run({nodes: [node]}); }
      catch {
        node.textContent = original;
        node.after(el('p', {class: 'muted'}, '图示无法渲染，请核对 Mermaid 原文。'));
      }
    }
  }

  function selectSection(key, {fromHash = false} = {}) {
    const found = findSection(key);
    if (!found) return;
    const tree = $('#tree');
    tree.querySelectorAll('.selectable.selected').forEach(node => node.classList.remove('selected'));
    docs.selected = key;
    let row = tree.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (fromHash && !(row && shown(row))) {
      openState.set('d:' + found.doc.name, true);
      const outline = docOutline(found.doc);
      const parent = outline.items.find(item => item.children.includes(found.index));
      if (parent) openState.set(`s:${found.doc.name}::${found.doc.sections[parent.index].anchor}`, true);
      renderDocTree();
      row = tree.querySelector(`[data-key="${CSS.escape(key)}"]`);
      row?.scrollIntoView({block: 'center'});
    }
    row?.classList.add('selected');
    if (decodeURIComponent(location.hash.slice(1)) !== key) history.replaceState(null, '', '#' + encodeURIComponent(key));
    const {doc, index} = found;
    const sections = sectionRange(doc, index);
    const head = doc.sections[index];
    const parent = docOutline(doc).items.find(item => item.children.includes(index));
    const visible = [...tree.querySelectorAll('.selectable')].filter(shown);
    const position = visible.findIndex(node => node.dataset.key === key);
    const nav = (step, label) => {
      const target = visible[position + step];
      return el('button', {type: 'button', disabled: !target, onclick: () => { target.click(); target.scrollIntoView({block: 'nearest'}); }}, label);
    };
    const answer = ANSWER[head.answer];
    const pane = $('#detail');
    const body = el('div', {class: 'markdown'});
    body.innerHTML = sections.map(s => s.html).join('\n');
    pane.replaceChildren(el('div', {class: 'doc-view'},
      el('div', {class: 'crumbs'}, doc.name, parent ? [el('span', {}, '›'), shortTitle(doc.sections[parent.index].title)] : null),
      answer ? el('div', {class: `banner ${answer[0]}`}, pill(answer[0], answer[1])) : null,
      body,
      el('div', {class: 'pager'}, nav(-1, '← 上一节'), nav(1, '下一节 →'))));
    pane.scrollTop = 0;
    renderMermaid(body);
  }

  async function startSop() {
    docs.data = await api('/api/sop?' + query());
    fillCheckouts(docs.data.checkouts, docs.data.checkout);
    $('#search').addEventListener('input', event => { docs.search = event.target.value; renderDocTree(); });
    renderDocFilters();
    renderDocTree();
    const key = decodeURIComponent(location.hash.slice(1));
    if (key && findSection(key)) selectSection(key, {fromHash: true});
    else if (docs.data.documents[0]?.sections.length) {
      const first = docs.data.documents[0];
      selectSection(`${first.name}::${first.sections[0].anchor}`, {fromHash: true});
    }
    window.addEventListener('hashchange', () => {
      const next = decodeURIComponent(location.hash.slice(1));
      if (next && next !== docs.selected) selectSection(next, {fromHash: true});
    });
  }

  // ---- 运行记录 ----

  const table = (headers, rows, empty) => el('div', {class: 'table-wrap'}, el('table', {},
    el('thead', {}, el('tr', {}, headers.filter(Boolean).map(t => el('th', {}, t)))),
    el('tbody', {}, rows.length ? rows : [el('tr', {}, el('td', {colspan: headers.length, class: 'muted center'}, empty))])));

  async function startRuns() {
    const data = await api('/api/runs?' + query());
    const boardData = await api('/api/board?' + query());
    fillCheckouts(boardData.checkouts, data.checkout);
    const launchState = {running: ['retest', '运行中'], exited: ['pass', '已结束'], failed: ['fail', '失败'], stopped: ['skip', '已停止']};
    $('#launches').replaceChildren(table(['编号', '类型', '代理', '场景', '状态', '开始', '发起人', config.local ? '本机' : null],
      data.launches.map(item => {
        const [cls, label] = launchState[item.status] || ['skip', item.status];
        return el('tr', {}, el('td', {class: 'mono'}, '#' + item.id), el('td', {}, item.kind === 'run' ? '场景执行' : '回归沉淀'), el('td', {}, item.agent),
          el('td', {class: 'mono'}, item.scope.join('、')),
          el('td', {}, pill(cls, label + (item.exit_code !== null && item.exit_code !== undefined ? `（退出码 ${item.exit_code}）` : ''))),
          el('td', {}, item.started_at || ''), el('td', {}, item.requested_by),
          config.local ? el('td', {}, el('a', {href: `/api/launches/${item.id}/log`, target: '_blank'}, '日志'), item.session ? el('span', {class: 'muted mono'}, ' ' + item.session) : null) : null);
      }), '没有启动记录'));
    const runState = {prepared: ['retest', '进行中'], submitted: ['pass', '已提交'], abandoned: ['skip', '已放弃']};
    $('#runs').replaceChildren(table(['编号', '类型', '状态', '场景', '结果', '开始', config.local ? '会话' : null],
      data.runs.map(item => {
        const [cls, label] = runState[item.status] || ['skip', item.status];
        return el('tr', {}, el('td', {class: 'mono'}, '#' + item.id), el('td', {}, item.kind === 'run' ? (item.fix ? `修复 #${item.fix} 的复验` : '执行') : '沉淀'),
          el('td', {}, pill(cls, label)), el('td', {class: 'mono'}, item.scope.join('、')),
          el('td', {}, `登记 ${item.recorded}` + (item.voided.length ? `，内容变化未登记 ${item.voided.length}` : '') + (item.not_executed.length ? `，未执行 ${item.not_executed.length}` : '')),
          el('td', {}, item.created_at), config.local ? el('td', {class: 'mono muted'}, item.session || '') : null);
      }), '没有运行'));
  }

  // ---- 收件箱与草稿批注 ----

  function requireName(message) {
    const name = displayName();
    if (!name) { message.textContent = '请先填写姓名（右上角“署名”）'; openSigner(); }
    return name;
  }

  async function startInbox() {
    const form = $('#upload');
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const message = form.querySelector('.message');
      const problems = $('#upload-problems');
      problems.replaceChildren();
      const file = form.querySelector('input[type=file]').files[0];
      const by = requireName(message);
      if (!file || !by) return;
      try {
        const content = await file.text();
        await post('/api/inbox', {kind: form.elements.namedItem('kind').value, filename: file.name, content, by});
        location.reload();
      } catch (error) {
        message.textContent = '上传被拒绝';
        problems.replaceChildren(el('ul', {}, error.message.split('；').map(text => el('li', {}, text))));
      }
    });
    for (const answerForm of document.querySelectorAll('form.answer')) {
      answerForm.addEventListener('submit', async event => {
        event.preventDefault();
        const message = answerForm.querySelector('.message');
        const by = requireName(message);
        if (!by) return;
        try {
          await post('/api/answer', {question: Number(answerForm.dataset.question), answer: answerForm.elements.namedItem('answer').value.trim(), by});
          location.reload();
        } catch (error) { message.textContent = '提交失败：' + error.message; }
      });
    }
  }

  async function startDrafts() {
    for (const form of document.querySelectorAll('form.annotate')) {
      form.addEventListener('submit', async event => {
        event.preventDefault();
        const message = form.querySelector('.message');
        const by = requireName(message);
        if (!by) return;
        try {
          await post('/api/annotate', {checkout: config.checkout, file: form.dataset.file,
            target: form.elements.namedItem('target').value, text: form.elements.namedItem('text').value.trim(), by});
          location.reload();
        } catch (error) { message.textContent = '提交失败：' + error.message; }
      });
    }
  }

  renderSigner();
  const start = {workbench: startWorkbench, sop: startSop, runs: startRuns, inbox: startInbox, drafts: startDrafts}[config.page];
  if (start) start().catch(error => $('main').prepend(el('p', {class: 'error-bar'}, error.message)));
})();
