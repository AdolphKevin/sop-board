// Flow Board 页面脚本。
// - 顶栏署名：姓名在页面上填一次，存 localStorage，所有页面与场景共用，使用者主动修改才变。
// - 工作台：左栏 文件→节点→场景 树（展开状态按项目记在 localStorage；点击场景只切换右栏，不重建树、不动滚动位置），
//   右栏四要素、证据、复测（通过 / 不通过 / 阻塞 / 需求变更，缺陷链接与姓名都可不填）与历史。
// - 左栏（工作台与业务 SOP 共用）：“全部展开 / 全部收起”一键切换树中全部分组，结果同手动展开一样记住；
//   有筛选或搜索时只作用于当前显示的结果。左栏可隐藏，隐藏状态记在 localStorage，两页共用。
//   左栏右边缘的手柄可拖拽调宽（240px 到窗口宽度的 60%，双击恢复 360px），宽度记在 localStorage，两页共用、不分项目。
//   文件、节点与场景标题悬停显示全文。
// - 草稿审阅（工作台“草稿”筛选）：草稿场景详情依次为 文件 › 节点 路径、四要素、SOP 规则原文、检查清单、批注、「同意」；
//   草稿文件与节点行的「审阅」打开文件面板或节点面板。同意后自动打开同一文件中下一条待审场景。
// - 业务 SOP：左栏 文档→章节→问题 树，右栏显示所选一节；待确认问题按“答复”小节标出已答复 / 待答复。
(() => {
  const config = JSON.parse(document.getElementById('board-config').textContent);
  const NAME_KEY = 'sop-board-name';
  const TREE_KEY = `sop-board-tree:${config.project}:${config.page}`;
  const SIDEBAR_KEY = 'sop-board-sidebar-hidden';
  const WIDTH_KEY = 'sop-board-sidebar-width';
  const WIDTH_DEFAULT = 360;
  const WIDTH_MIN = 240;

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
    // 保留地址里的 #场景键 或面板：整段地址重新赋值，不能把 # 拼进 location.search（会被编码进查询参数）。
    select.onchange = () => {
      const search = params();
      search.set('checkout', select.value);
      location.assign(location.pathname + '?' + search.toString() + location.hash);
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
      // 打开页面时地址里的场景或章节只在它的分组没被收起过时才展开，使用者收起的状态刷新后保持。
      collapsed: keys => keys.some(key => saved[key] === false),
    };
  })();

  function group(key, fallback, filtering, summary, children, className) {
    const details = el('details', {class: className, open: filtering ? true : openState.get(key, fallback)},
      el('summary', {}, el('span', {class: 'chevron', 'aria-hidden': 'true'}), summary), children);
    details.addEventListener('toggle', () => { if (!details.dataset.filtering) openState.set(key, details.open); });
    if (filtering) details.dataset.filtering = '1';
    return details;
  }

  // ---- 左栏：隐藏与显示、拖拽调宽、全部展开或收起 ----

  // 宽度只改本浏览器的显示：记住的值按当前窗口取边界后显示，窗口变化时重新取边界，不改记住的值。
  const widthLimit = () => Math.max(WIDTH_MIN, Math.floor(window.innerWidth * 0.6));
  const clampWidth = value => Math.min(widthLimit(), Math.max(WIDTH_MIN, Math.round(value)));

  function setupResize(section) {
    const handle = section.querySelector('.sidebar-resizer');
    if (!handle) return;
    const stored = () => {
      const value = Number(localStorage.getItem(WIDTH_KEY));
      return Number.isFinite(value) && value > 0 ? value : WIDTH_DEFAULT;
    };
    let wanted = stored(), current = WIDTH_DEFAULT, drag = null;
    const apply = value => {
      current = clampWidth(value);
      section.style.setProperty('--sidebar-width', current + 'px');
      handle.setAttribute('aria-valuemin', String(WIDTH_MIN));
      handle.setAttribute('aria-valuemax', String(widthLimit()));
      handle.setAttribute('aria-valuenow', String(current));
      return current;
    };
    const save = width => {
      wanted = width;
      if (width === WIDTH_DEFAULT) localStorage.removeItem(WIDTH_KEY); else localStorage.setItem(WIDTH_KEY, String(width));
    };
    const left = () => section.getBoundingClientRect().left;
    apply(wanted);
    // 按下时记住指针在手柄中的偏移，只点不拖时宽度不变。
    handle.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      drag = {pointer: event.pointerId, offset: event.clientX - left() - current};
      document.body.classList.add('resizing');
    });
    handle.addEventListener('pointermove', event => {
      if (drag && drag.pointer === event.pointerId) apply(event.clientX - left() - drag.offset);
    });
    const finish = event => {
      if (!drag || drag.pointer !== event.pointerId) return;
      drag = null;
      document.body.classList.remove('resizing');
      save(current);
    };
    handle.addEventListener('pointerup', finish);
    handle.addEventListener('pointercancel', finish);
    handle.addEventListener('dblclick', () => save(apply(WIDTH_DEFAULT)));
    handle.addEventListener('keydown', event => {
      const step = {ArrowLeft: -16, ArrowRight: 16}[event.key];
      const target = step ? current + step : event.key === 'Home' ? WIDTH_MIN : event.key === 'End' ? widthLimit() : null;
      if (target === null) return;
      event.preventDefault();
      save(apply(target));
    });
    window.addEventListener('resize', () => apply(wanted));
    window.addEventListener('storage', event => { if (event.key === WIDTH_KEY) apply(wanted = stored()); });
  }

  function setupSidebar() {
    const section = $('.workbench');
    if (!section) return;
    setupResize(section);
    const apply = hidden => {
      section.classList.toggle('sidebar-hidden', hidden);
      for (const button of section.querySelectorAll('[data-sidebar]')) button.setAttribute('aria-expanded', String(!hidden));
    };
    apply(localStorage.getItem(SIDEBAR_KEY) === '1');
    section.addEventListener('click', event => {
      const button = event.target.closest('[data-sidebar]');
      if (!button) return;
      const hidden = button.dataset.sidebar === 'hide';
      if (hidden) localStorage.setItem(SIDEBAR_KEY, '1'); else localStorage.removeItem(SIDEBAR_KEY);
      apply(hidden);
      section.querySelector(`[data-sidebar="${hidden ? 'show' : 'hide'}"]`)?.focus();
    });
  }

  // 所有分组都展开时按钮为“全部收起”，否则为“全部展开”。改的是 details.open，
  // 由各分组自己的 toggle 监听记进 openState（筛选或搜索时不记），与手动展开收起一致。
  function setupTreeToggle() {
    const tree = $('#tree');
    const button = $('#tree-all');
    if (!tree || !button) return;
    const groups = () => [...tree.querySelectorAll('details')];
    const sync = () => {
      const all = groups();
      const expanded = all.length > 0 && all.every(details => details.open);
      button.textContent = expanded ? '全部收起' : '全部展开';
      button.dataset.action = expanded ? 'collapse' : 'expand';
      button.title = expanded ? '收起左栏树中的全部分组' : '展开左栏树中的全部分组';
      button.disabled = !all.length;
    };
    button.addEventListener('click', () => {
      const open = button.dataset.action !== 'collapse';
      for (const details of groups()) details.open = open;
      sync();
    });
    tree.addEventListener('toggle', sync, true);
    new MutationObserver(sync).observe(tree, {childList: true});
    sync();
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
    'not-run': '未执行', 'pass': '通过', 'skip': '跳过', 'draft': '草稿'};
  const VERDICTS = [['pass', '通过'], ['fail', '不通过'], ['blocked', '阻塞'], ['changed', '需求变更']];
  const BADGES = ['review', 'distill'];
  // selected：右栏打开的场景键；panel：右栏打开的草稿文件面板（node 为空）或节点面板。
  const board = {data: null, signature: '', filter: null, search: '', selected: null, panel: null};

  async function loadBoard() {
    const data = await api('/api/board?' + query());
    const signature = JSON.stringify([data.counts, data.files.map(f => [f.name, f.invalid, f.nodes]),
      Object.entries(data.items).map(([k, v]) => [k, v.state, v.distill, v.review, v.title, v.draft_review]), data.stats]);
    board.data = data;
    if (signature === board.signature) return;
    const first = !board.signature;
    board.signature = signature;
    if (first) fillCheckouts(data.checkouts, data.checkout);
    renderFilters();
    renderTree();
    if (!first && !board.selected && !board.panel) renderOverview();
  }

  // ---- 草稿审阅：进度、「审阅」按钮与面板地址 ----

  const isDraftFile = name => board.data.files.some(f => f.name === name && f.status === 'draft' && !f.invalid);

  function draftKeys(fileName, nodeId = null) {
    const file = board.data.files.find(f => f.name === fileName);
    if (!file || file.invalid) return [];
    return file.nodes.filter(node => !nodeId || node.id === nodeId)
      .flatMap(node => node.scenarios.map(id => `${file.name}#${id}`)).filter(key => board.data.items[key]);
  }

  function draftProgress(fileName, nodeId = null) {
    const keys = draftKeys(fileName, nodeId);
    const count = state => keys.filter(key => board.data.items[key].draft_review?.state === state).length;
    return {total: keys.length, pending: count('pending'), annotated: count('annotated'), approved: count('approved')};
  }

  const progressText = p => `待审 ${p.pending}、有批注 ${p.annotated}、已同意 ${p.approved}`;
  const progressTag = p => el('span', {class: 'tag progress ' + (p.total && p.approved === p.total ? 'pass' : 'draft'),
    title: progressText(p)}, `已同意 ${p.approved}/${p.total}`);

  // 「审阅」按钮在分组标题行里：阻止默认动作，点它不会展开或收起分组。
  function reviewTools(fileName, nodeId = null) {
    return [progressTag(draftProgress(fileName, nodeId)),
      el('button', {type: 'button', class: 'review-open',
        title: nodeId ? '打开节点面板：本组场景的四要素与同意' : '打开文件面板：需求说明、来源、审阅进度与文件级批注',
        onclick: event => { event.preventDefault(); event.stopPropagation(); openPanel(fileName, nodeId); }}, '审阅')];
  }

  const panelHash = (file, node) => 'review:' + file + (node ? '#' + node : '');
  const setHash = value => {
    if (decodeURIComponent(location.hash.slice(1)) !== value) history.replaceState(null, '', '#' + encodeURIComponent(value).replace(/%23/g, '#'));
  };

  function parsePanel(hash) {
    if (!hash.startsWith('review:')) return null;
    const rest = hash.slice('review:'.length);
    const index = rest.indexOf('#');
    return index < 0 ? {file: rest, node: null} : {file: rest.slice(0, index), node: rest.slice(index + 1)};
  }

  function markPanel() {
    const current = board.panel ? panelHash(board.panel.file, board.panel.node) : null;
    for (const summary of $('#tree').querySelectorAll('summary[data-panel]')) summary.classList.toggle('selected', summary.dataset.panel === current);
  }

  function openPanel(file, node = null, {notice = null} = {}) {
    $('#tree').querySelector('.scenario.selected')?.classList.remove('selected');
    board.selected = null;
    board.panel = {file, node};
    markPanel();
    setHash(panelHash(file, node));
    loadPanel({notice});
  }

  function nextPending(file, currentKey) {
    const keys = draftKeys(file);
    const index = keys.indexOf(currentKey);
    const order = keys.slice(index + 1).concat(keys.slice(0, Math.max(index, 0)));
    return order.find(key => board.data.items[key]?.draft_review?.state === 'pending') || null;
  }

  // 内网须署名；本机不填时由看板以本机操作者署名。
  function approverName(message) {
    const by = storedName() || null;
    if (!config.local && !by) { message.textContent = '请先填写姓名（右上角“署名”）再操作'; openSigner(); return undefined; }
    return by;
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

  // 草稿场景的状态点按审阅状态（待审、有批注、已同意）显示；标题、编号悬停显示全文，状态点悬停显示状态与原因。
  function scenarioRow(key, item) {
    const draft = item.draft_review;
    const dot = draft ? `draft-${draft.state}` : item.state;
    const hint = draft ? `草稿 · ${draft.label}` + (draft.approvers?.length ? `（${draft.approvers.join('、')}）` : '')
      : item.label + (item.reason ? '：' + item.reason : '');
    return el('div', {class: 'row scenario selectable' + (board.selected === key ? ' selected' : ''), role: 'treeitem', tabindex: 0,
      'data-key': key, onclick: () => select(key), onkeydown: event => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); moveSelection($('#tree'), event.currentTarget, event.key === 'ArrowDown' ? 1 : -1); }
      }},
      el('span', {class: `dot ${dot}`, title: hint}), el('span', {class: 'sid', title: key}, item.id),
      el('span', {class: 'stitle', title: item.title}, item.title),
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
        const draft = file.status === 'draft';
        // 场景少的文件缺省展开节点；大文件缺省只列节点，像目录一样逐个展开。
        const smallFile = file.nodes.reduce((sum, node) => sum + node.scenarios.length, 0) <= 40;
        const nodes = file.nodes.map(node => {
          const keys = node.scenarios.map(id => `${file.name}#${id}`).filter(key => board.data.items[key] && matches(key, board.data.items[key]));
          if (!keys.length) return null;
          fileCount += keys.length;
          const details = group(`n:${file.name}#${node.id}`, smallFile, filtering,
            el('span', {class: 'group-label'}, el('span', {class: 'nid', title: node.id}, node.id),
              el('span', {class: 'ntitle', title: node.title}, node.title),
              draft ? reviewTools(file.name, node.id) : el('span', {class: 'count'}, keys.length)),
            el('div', {class: 'rows'}, keys.map(key => scenarioRow(key, board.data.items[key]))), 'group node');
          if (draft) details.querySelector('summary').dataset.panel = panelHash(file.name, node.id);
          return details;
        }).filter(Boolean);
        if (!nodes.length) continue;
        const details = group('f:' + file.name, true, filtering,
          el('span', {class: 'group-label'}, el('span', {class: 'fname', title: file.name}, file.name),
            draft ? el('span', {class: 'tag draft'}, '草稿') : null,
            draft ? reviewTools(file.name) : el('span', {class: 'count'}, fileCount)),
          el('div', {class: 'nodes'}, nodes), 'group file');
        if (draft) details.querySelector('summary').dataset.panel = panelHash(file.name, null);
        files.push(details);
      }
      tree.replaceChildren(...(files.length ? files : [el('p', {class: 'muted pad'}, '没有符合条件的场景')]));
    });
    markPanel();
  }

  function reveal(key, {initial = false} = {}) {
    const item = board.data.items[key];
    if (!item) return;
    const keys = ['f:' + item.file, `n:${item.file}#${item.node}`];
    if (initial && openState.collapsed(keys)) return;
    for (const groupKey of keys) openState.set(groupKey, true);
    renderTree();
    $('#tree').querySelector(`[data-key="${CSS.escape(key)}"]`)?.scrollIntoView({block: 'center'});
  }

  function select(key, {fromHash = false, initial = false, notice = null} = {}) {
    const tree = $('#tree');
    tree.querySelector('.scenario.selected')?.classList.remove('selected');
    board.selected = key;
    board.panel = null;
    markPanel();
    const row = tree.querySelector(`[data-key="${CSS.escape(key)}"]`);
    row?.classList.add('selected');
    if (fromHash && !(row && shown(row))) reveal(key, {initial});
    setHash(key);
    loadDetail(key, {notice});
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
    return entry.status;
  }

  // 结果执行时被 env stop 停着的服务（故障模拟），例如“执行时 stock（库存查询与扣减）已停止”。
  function stoppedServices(entry) {
    if (!entry?.stopped_services?.length) return null;
    return el('p', {class: 'stopped-services'}, entry.stopped_services.map(s => s.text).join('；'));
  }

  // 最近一次未执行：不是结果，不改变状态，只说明谁、什么时候、缺什么。
  function notExecutedNote(item) {
    if (!item) return null;
    return el('p', {class: 'not-executed'}, el('strong', {}, '最近一次未执行'),
      `：${item.by || '未署名'}，${item.at}，运行 #${item.run}${item.finding ? '，' + item.finding : ''}（未执行不改变状态）`);
  }

  function historyItem(entry) {
    const who = entry.kind === 'manual' ? `复测人 ${entry.marked_by}` :
      [entry.independence, entry.run ? `运行 #${entry.run}` : null, entry.launch ? `启动 #${entry.launch}` : null,
        entry.executor ? `执行者 ${entry.executor}` : null, entry.snapshot ? `快照 ${entry.snapshot}` : null].filter(Boolean).join(' · ');
    return el('li', {class: 'event'},
      el('div', {class: 'event-head'}, pill(stateOf(entry), entry.label), el('span', {class: 'muted'}, entry.executed_at),
        el('span', {class: 'muted'}, who), entry.current ? null : el('span', {class: 'tag old'}, '旧内容')),
      entry.finding ? el('p', {class: 'finding'}, entry.finding) : null,
      stoppedServices(entry),
      entry.note ? el('p', {class: 'note'}, '说明：' + entry.note) : null,
      entry.defect ? el('p', {}, '缺陷：', /^https?:/.test(entry.defect) ? el('a', {href: entry.defect, target: '_blank', rel: 'noopener'}, entry.defect) : entry.defect) : null,
      entry.product_changed?.length ? el('p', {class: 'muted'}, '执行期间产品变化（只作版本记录）：' + entry.product_changed.slice(0, 5).join('、')) : null,
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

  // ---- 草稿审阅：场景详情、批注、同意 ----

  const NOTE_STATUS = {open: '待处理', adopted: '已采纳', rejected: '未采纳'};

  function noteItem(note) {
    return el('li', {}, el('div', {}, note.target === 'file' ? el('span', {class: 'chip'}, '整个文件') : null, ' ', note.text),
      el('div', {class: 'muted small'}, `#${note.id} · ${note.author} · ${note.created_at} · ${NOTE_STATUS[note.status] || note.status}` +
        (note.note ? ` · 处理说明：${note.note}` : '')));
  }

  // 批注表单：target 为场景编号或 'file'。提交后刷新左栏（审阅状态随之变化）与当前右栏。
  function annotationForm({checkout, file, target, placeholder, refresh}) {
    const text = el('textarea', {name: 'text', rows: 4, placeholder, required: true});
    const message = el('p', {class: 'message'});
    const signer = el('p', {class: 'signed-as', 'data-signer': ''});
    fillSignerLine(signer);
    return el('form', {class: 'stack', onsubmit: async event => {
      event.preventDefault();
      if (!displayName()) { message.textContent = '请先填写姓名（右上角“署名”）'; openSigner(); return; }
      if (!text.value.trim()) return;
      try {
        await post('/api/annotate', {checkout, file, target, text: text.value.trim(), by: displayName()});
        await loadBoard();
        await refresh();
      } catch (error) { message.textContent = '提交失败：' + error.message; }
    }}, el('label', {class: 'field'}, el('span', {}, '批注内容'), text), signer, el('button', {type: 'submit', class: 'primary block'}, '提交批注'), message);
  }

  function annotationCard(data) {
    const list = data.annotations?.length ? el('ul', {class: 'notes'}, data.annotations.map(noteItem))
      : el('p', {class: 'muted small'}, '还没有批注');
    return el('div', {class: 'card'}, el('h3', {}, '批注'),
      el('p', {class: 'muted small'}, '对照检查清单发现问题时写批注，开发者的会话按批注修订场景；有未处理的批注时不能同意。'), list,
      annotationForm({checkout: data.checkout, file: data.scene.file, target: data.scene.id,
        placeholder: '写明哪一条要怎样改，例如：预期缺少具体金额', refresh: () => loadDetail(data.key, {keep: true})}));
  }

  // 同意记录：只有自己的同意带“撤销”。
  function approvalList(approvals, refresh, message) {
    const me = displayName();
    if (!approvals.length) return el('p', {class: 'muted small'}, '当前内容还没有人同意');
    return el('ul', {class: 'approvals'}, approvals.map(a => el('li', {},
      el('span', {class: 'pill draft-approved'}, '同意'), el('strong', {}, a.by), el('span', {class: 'muted small'}, a.at),
      a.by === me ? el('button', {type: 'button', class: 'link', onclick: async () => {
        try { await post('/api/unapprove', {approval: a.id, by: storedName() || null}); await loadBoard(); await refresh(); }
        catch (error) { message.textContent = '撤销失败：' + error.message; }
      }}, '撤销我的同意') : null)));
  }

  function approveCard(data, notice) {
    const review = data.draft;
    const active = review.approvals.filter(a => a.active);
    const mine = active.some(a => a.by === displayName());
    const blocked = review.state === 'annotated';
    const message = el('p', {class: 'message'}, notice || '');
    const signer = el('p', {class: 'signed-as', 'data-signer': ''});
    fillSignerLine(signer);
    const refresh = () => loadDetail(data.key, {keep: true});
    const button = el('button', {type: 'button', class: 'primary block', disabled: blocked || mine, onclick: async () => {
      const by = approverName(message);
      if (by === undefined) return;
      button.disabled = true;
      message.textContent = '提交中…';
      try {
        const result = await post('/api/approve', {checkout: data.checkout, file: data.scene.file, scenario: data.scene.id,
          fingerprint: data.scene.fingerprint, by});
        await loadBoard();
        const next = nextPending(data.scene.file, data.key);
        const done = `已同意 ${data.scene.id}（${result.approved_by}）`;
        if (next) select(next, {fromHash: true, notice: `${done}，已打开 ${data.scene.file} 的下一条待审场景`});
        else await loadDetail(data.key, {keep: true, notice: `${done}；${data.scene.file} 没有待审场景`});
      } catch (error) { message.textContent = '同意失败：' + error.message; button.disabled = blocked || mine; }
    }}, mine ? '你已同意' : '同意');
    return el('div', {class: 'card approve-card'}, el('h3', {}, '同意'),
      approvalList(active, refresh, message),
      blocked ? el('p', {class: 'hint'}, '有未处理的批注，暂时不能同意：等开发者的会话按批注修订或说明不采纳后再审阅。') : null,
      signer, button, message);
  }

  function rulesBlock(rules) {
    const items = (rules || []).map(rule => rule.html
      ? el('details', {class: 'rule'}, el('summary', {}, `${rule.document} › ${rule.title}`), el('div', {class: 'markdown', html: rule.html}))
      : el('p', {class: 'muted small'}, `${rule.ref}：${rule.problem}`));
    return section('SOP 规则原文', items.length ? items : el('p', {class: 'muted'}, '场景没有 refs，无法对照 SOP 规则原文'));
  }

  function checklistBlock(items) {
    return section('审阅检查清单', el('ol', {class: 'checklist'}, items.map(text => el('li', {}, text))),
      el('p', {class: 'muted small'}, '逐项核对：有问题写批注；都没有问题时点「同意」。'));
  }

  function reviewHistory(data) {
    const events = data.draft.approvals.map(a => ({at: a.at, node: el('li', {class: 'event'}, el('div', {class: 'event-head'},
      pill(a.active ? 'draft-approved' : 'skip', a.active ? '同意' : a.revoked_at ? '已撤销的同意' : '旧内容上的同意（已失效）'),
      el('span', {class: 'muted'}, a.at), el('span', {class: 'muted'}, a.by)),
      a.revoked_at ? el('p', {class: 'muted small'}, `撤销于 ${a.revoked_at}`) : null)}))
      .concat(data.annotations.map(note => ({at: note.created_at, node: el('li', {class: 'event'}, el('div', {class: 'event-head'},
        pill(note.status === 'open' ? 'draft-annotated' : 'skip', note.target === 'file' ? '文件级批注' : '批注'),
        el('span', {class: 'muted'}, note.created_at), el('span', {class: 'muted'}, `${note.author} · ${NOTE_STATUS[note.status] || note.status}`)),
        el('p', {}, note.text))})))
      .sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0));
    return el('details', {class: 'block history'}, el('summary', {}, el('h3', {}, '审阅记录'), el('span', {class: 'muted'}, `${events.length} 条`)),
      events.length ? el('ul', {class: 'timeline'}, events.map(e => e.node)) : el('p', {class: 'muted'}, '没有记录'));
  }

  // 草稿场景详情：路径（点哪一级打开哪一级的面板）、四要素、SOP 规则原文、检查清单、批注、同意、审阅记录；没有复测表单。
  function draftDetail(data, notice) {
    const scene = data.scene, review = data.draft;
    const reason = review.state === 'approved' ? `同意人：${review.approvers.join('、')}`
      : review.state === 'annotated' ? '有未处理的批注' : '当前内容还没有人同意';
    return el('div', {class: 'detail-grid single'}, el('div', {class: 'main'},
      el('nav', {class: 'crumbs', 'aria-label': '草稿路径'},
        el('button', {type: 'button', class: 'crumb', title: '打开文件面板', onclick: () => openPanel(scene.file)}, scene.file),
        el('span', {}, '›'),
        el('button', {type: 'button', class: 'crumb', title: '打开节点面板', onclick: () => openPanel(scene.file, scene.node)},
          `${scene.node} ${scene.node_title || ''}`)),
      el('h1', {}, scene.title),
      el('div', {class: 'meta'}, el('span', {class: 'sid big'}, data.key),
        el('span', {class: 'chip'}, scene.priority), el('span', {class: 'chip'}, '入口 ' + scene.verify),
        el('span', {class: 'chip'}, scene.polarity === 'positive' ? '正向' : '反向' + (scene.negative_type ? ' · ' + scene.negative_type : '')),
        el('span', {class: 'chip'}, '角色 ' + scene.actor), pill('draft', '草稿')),
      el('div', {class: `banner draft-${review.state}`}, pill(`draft-${review.state}`, review.label), el('span', {class: 'reason'}, reason),
        el('span', {class: 'muted small'}, '草稿确认后才进入执行')),
      ...contentSections(scene),
      rulesBlock(data.rules),
      checklistBlock(data.checklist),
      annotationCard(data),
      approveCard(data, notice),
      reviewHistory(data)));
  }

  function contentSections(scene, coverage = null) {
    return [section('起点', scene.initial_state?.length ? el('ul', {class: 'plain'}, scene.initial_state.map(t => el('li', {}, t))) : el('p', {class: 'muted'}, '无')),
      section('步骤', numbered(scene.steps, null, null, 'steps')),
      section('预期', numbered(scene.expected, 'expected', coverage, 'expected')),
      section('禁止', numbered(scene.forbidden_effects, 'forbidden_effects', coverage, 'forbidden'))];
  }

  // ---- 草稿审阅：文件面板与节点面板 ----

  async function loadPanel({keep = false, notice = null} = {}) {
    const panel = board.panel;
    if (!panel) return;
    const pane = $('#detail');
    let data;
    try { data = await api('/api/draft?' + query({file: panel.file})); }
    catch (error) { pane.replaceChildren(el('div', {class: 'empty'}, el('p', {class: 'finding'}, error.message))); return; }
    if (board.panel !== panel) return;
    const top = pane.scrollTop;
    pane.replaceChildren(el('div', {class: 'detail-grid single'}, el('div', {class: 'main'},
      panel.node ? nodePanel(data, panel.node, notice) : filePanel(data, notice))));
    pane.scrollTop = keep ? top : 0;
  }

  const countOf = (rows, state) => rows.filter(row => row.review === state).length;
  const rowsProgress = rows => ({total: rows.length, pending: countOf(rows, 'pending'), annotated: countOf(rows, 'annotated'), approved: countOf(rows, 'approved')});

  function filePanel(data, notice) {
    const p = data.progress;
    const fileOpen = data.open_annotations.filter(a => a.target === 'file').length;
    const blockers = [p.pending ? `待审 ${p.pending} 条` : null, p.annotated ? `有批注 ${p.annotated} 条` : null,
      fileOpen ? `文件级批注未处理 ${fileOpen} 条` : null].filter(Boolean);
    const refresh = () => loadPanel({keep: true});
    return [
      el('div', {class: 'crumbs'}, el('span', {}, data.file)),
      el('h1', {}, data.file, ' ', pill('draft', '草稿')),
      notice ? el('p', {class: 'notice'}, notice) : null,
      section('需求说明', el('p', {}, data.requirement || '—'), el('p', {class: 'muted small'}, `版本 ${data.version ?? '—'}`)),
      section('来源', data.sources?.length ? el('ul', {class: 'plain'}, data.sources.map(s =>
        el('li', {}, `${s.ref}（版本 ${s.version}，确认人 ${s.confirmed_by}）`))) : el('p', {class: 'muted'}, '无')),
      section('审阅进度', el('p', {class: 'progress-line'}, `${progressText(p)}（共 ${data.total} 条）`),
        el('ul', {class: 'node-list'}, data.nodes.map(node => el('li', {},
          el('button', {type: 'button', class: 'link-row', onclick: () => openPanel(data.file, node.id)},
            el('span', {class: 'nid'}, node.id), el('span', {class: 'ntitle'}, node.title), progressTag(rowsProgress(node.scenarios))))))),
      data.ready
        ? el('div', {class: 'banner draft-approved'}, pill('draft-approved', '可以确认'),
          el('span', {class: 'reason'}, '全部场景已同意，没有未处理的批注；由开发者的会话运行确认命令。'),
          config.local && data.command ? el('code', {class: 'command'}, data.command) : null)
        : el('div', {class: 'banner draft'}, pill('draft', '还不能确认'), el('span', {class: 'reason'}, blockers.join('；'))),
      el('div', {class: 'card'}, el('h3', {}, '文件级批注'),
        el('p', {class: 'muted small'}, '针对整个文件的意见（例如漏掉的场景）。文件级批注不影响单条场景的同意，但处理完之前不能确认。'),
        data.annotations.length ? el('ul', {class: 'notes'}, data.annotations.map(noteItem)) : el('p', {class: 'muted small'}, '还没有文件级批注'),
        annotationForm({checkout: data.checkout, file: data.file, target: 'file', placeholder: '写明整个文件缺什么或要怎样改', refresh})),
    ];
  }

  function nodePanel(data, nodeId, notice) {
    const node = data.nodes.find(n => n.id === nodeId);
    if (!node) return [el('p', {class: 'finding'}, `${data.file} 中没有节点 ${nodeId}`)];
    const pending = node.scenarios.filter(s => s.review === 'pending');
    const annotated = node.scenarios.filter(s => s.review === 'annotated');
    const message = el('p', {class: 'message'}, notice || '');
    const signer = el('p', {class: 'signed-as', 'data-signer': ''});
    fillSignerLine(signer);
    const refresh = () => loadPanel({keep: true});
    const button = el('button', {type: 'button', class: 'primary block', disabled: !pending.length, onclick: async () => {
      const by = approverName(message);
      if (by === undefined) return;
      button.disabled = true;
      message.textContent = '提交中…';
      try {
        const result = await post('/api/approve', {checkout: data.checkout, file: data.file, node: node.id, by,
          fingerprints: Object.fromEntries(node.scenarios.map(s => [s.id, s.fingerprint]))});
        const text = [result.approved.length ? `已同意 ${result.approved.join('、')}（${result.approved_by}）` : '没有新同意的场景',
          ...result.skipped.map(s => `跳过 ${s.scenario}：${s.reason}`)].join('；');
        await loadBoard();
        await loadPanel({keep: true, notice: text});
      } catch (error) { message.textContent = '同意失败：' + error.message; button.disabled = false; }
    }}, pending.length ? `同意本组 ${pending.length} 条待审场景` : '本组没有待审场景');
    return [
      el('nav', {class: 'crumbs', 'aria-label': '草稿路径'},
        el('button', {type: 'button', class: 'crumb', title: '打开文件面板', onclick: () => openPanel(data.file)}, data.file),
        el('span', {}, '›'), el('span', {}, node.id)),
      el('h1', {}, `${node.id} ${node.title}`),
      el('div', {class: 'meta'}, el('span', {class: 'chip'}, '角色 ' + node.actor), el('span', {class: 'chip'}, '入口 ' + node.entry),
        progressTag(rowsProgress(node.scenarios))),
      el('div', {class: 'card approve-card'}, el('h3', {}, '同意本组'),
        el('p', {class: 'muted small'}, '逐条读完下面每条场景的四要素后再同意；同意逐条记录，可以逐条撤销自己的同意。'),
        annotated.length ? el('ul', {class: 'skip-list'}, annotated.map(s => el('li', {}, `将跳过 ${s.id}：有未处理的批注`))) : null,
        signer, button, message),
      ...node.scenarios.map(scene => el('article', {class: 'scenario-card'},
        el('div', {class: 'event-head'}, pill(`draft-${scene.review}`, scene.label),
          el('button', {type: 'button', class: 'crumb', title: '打开场景详情（批注、规则原文与检查清单）', onclick: () => select(scene.key, {fromHash: true})}, scene.id),
          el('strong', {}, scene.title)),
        approvalList(scene.approvals, refresh, message),
        ...contentSections(scene))),
    ];
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

  async function loadDetail(key, {keep = false, notice = null} = {}) {
    const pane = $('#detail');
    let data;
    try { data = await api('/api/scenario?' + query({key})); }
    catch (error) { pane.replaceChildren(el('div', {class: 'empty'}, el('p', {class: 'finding'}, error.message))); return; }
    if (board.selected !== key) return;
    const scene = data.scene;
    const latest = data.latest;
    const current = data.history.filter(h => h.current);
    const top = pane.scrollTop;
    if (scene.draft) {
      pane.replaceChildren(draftDetail(data, notice));
      pane.scrollTop = keep ? top : 0;
      return;
    }
    const main = el('div', {class: 'main'},
      el('div', {class: 'crumbs'}, scene.file, el('span', {}, '›'), `${scene.node} ${scene.node_title || ''}`),
      el('h1', {}, scene.title),
      el('div', {class: 'meta'}, el('span', {class: 'sid big'}, data.key),
        el('span', {class: 'chip'}, scene.priority), el('span', {class: 'chip'}, '入口 ' + scene.verify),
        el('span', {class: 'chip'}, scene.polarity === 'positive' ? '正向' : '反向' + (scene.negative_type ? ' · ' + scene.negative_type : '')),
        el('span', {class: 'chip'}, '角色 ' + scene.actor)),
      el('div', {class: `banner ${data.state.state}`}, pill(data.state.state, data.state.label),
        el('span', {class: 'reason'}, data.state.reason || ''), data.state.review ? el('span', {class: 'tag review'}, '待人工复核') : null,
        data.state.distill ? el('span', {class: 'tag distill'}, '待沉淀') : null),
      notExecutedNote(data.not_executed),
      ...contentSections(scene, data.coverage),
      section('最新执行结果与证据', latest ? el('div', {},
        el('div', {class: 'event-head'}, pill(stateOf(latest), latest.label), el('span', {class: 'muted'}, latest.executed_at)),
        latest.actual ? el('p', {}, latest.actual) : null, latest.finding ? el('p', {class: 'finding'}, latest.finding) : null,
        stoppedServices(latest),
        gallery(latest.evidence)) : el('p', {class: 'muted'}, '当前内容还没有执行结果')),
      el('details', {class: 'block history'}, el('summary', {}, el('h3', {}, '历史'), el('span', {class: 'muted'}, `${data.history.length} 条，当前内容 ${current.length} 条`)),
        data.history.length ? el('ul', {class: 'timeline'}, data.history.map(historyItem)) : el('p', {class: 'muted'}, '没有记录'),
        data.guard_runs.length ? el('div', {}, el('h4', {}, '守护运行'), el('ul', {class: 'timeline'}, data.guard_runs.map(run =>
          el('li', {class: 'event'}, el('div', {class: 'event-head'}, pill(run.status === 'pass' ? 'pass' : 'fail', run.status === 'pass' ? '守护运行通过' : '守护运行未通过'),
            el('span', {class: 'muted'}, run.ran_at), el('span', {class: 'muted'}, `${run.operator} · 快照 ${run.snapshot}`)), run.reason ? el('p', {}, run.reason) : null)))) : null));
    const side = el('aside', {class: 'side'}, markCard(data), localCard(data));
    pane.replaceChildren(el('div', {class: 'detail-grid'}, main, side));
    pane.scrollTop = keep ? top : 0;
  }

  // 百分比与报告一致：恰为 .5 时取偶数（同 Python 的 {:.0%}）。
  const percent = value => { const x = value * 100, r = Math.round(x); return r - x === 0.5 && r % 2 ? r - 1 : r; };
  const ratio = item => item.denominator ? `${item.numerator}/${item.denominator}（${percent(item.value)}%）` : '—';

  // 本检出各服务的状态与业务说明，只在本机页面显示（内网接口不返回 environment）。
  function servicesBlock(environment) {
    if (!config.local || !environment?.services?.length) return null;
    const ENV = {ready: '就绪', starting: '启动中', failed: '启动失败', stopped: '未运行'};
    const tone = {running: 'pass', stopped: 'skip', absent: 'not-run', crashed: 'fail', unreported: 'blocked', down: 'skip'};
    return el('section', {class: 'block services'}, el('h3', {}, `本检出的服务（环境${ENV[environment.state] || environment.state}）`),
      el('ul', {class: 'service-list'}, environment.services.map(s => el('li', {},
        pill(tone[s.state] || 'skip', s.label), el('span', {class: 'mono'}, ' ' + s.name), el('span', {class: 'muted'}, `（${s.about}）`),
        s.stoppable ? el('span', {class: 'tag'}, '可停') : null))));
  }

  function renderOverview() {
    const pane = $('#detail');
    const {counts, total, items} = board.data;
    const tile = (state, label) => el('button', {type: 'button', class: `tile ${state}`,
      onclick: () => { board.filter = state; renderFilters(); renderTree(); }}, el('strong', {}, counts[state] || 0), el('span', {}, label));
    const urgent = Object.entries(items).filter(([, item]) => ['retest', 'fail', 'blocked', 'changed'].includes(item.state));
    pane.replaceChildren(el('div', {class: 'overview'},
      el('div', {}, el('h1', {}, `检出 ${board.data.checkout} · ${total} 条场景`),
        el('p', {class: 'muted'}, '从左侧选择场景查看四要素、证据并复测。代码改动不改变场景状态，结果记下取证时的快照。')),
      el('div', {class: 'tiles'}, [['retest', '待复测'], ['fail', '失败'], ['blocked', '受阻'], ['changed', '需求变更'],
        ['not-run', '未执行'], ['pass', '通过'], ['review', '待人工复核'], ['distill', '待沉淀']].map(([s, l]) => tile(s, l))),
      el('p', {class: 'muted metrics'}, `Agent 独立通过的人工确认率 ${ratio(board.data.stats.agent_confirm_rate)} · 复核覆盖 ${ratio(board.data.stats.agent_review_coverage)}`),
      servicesBlock(board.data.environment),
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
    const panel = parsePanel(key);
    if (panel && isDraftFile(panel.file)) openPanel(panel.file, panel.node);
    else if (key && board.data.items[key]) select(key, {fromHash: true, initial: true});
    else renderOverview();
    window.addEventListener('hashchange', () => {
      const next = decodeURIComponent(location.hash.slice(1));
      const target = parsePanel(next);
      if (target) {
        if (isDraftFile(target.file) && !(board.panel && board.panel.file === target.file && board.panel.node === target.node)) openPanel(target.file, target.node);
      } else if (next && next !== board.selected && board.data.items[next]) select(next, {fromHash: true});
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
      el('span', {class: 'stitle', title: label || shortTitle(section.title)}, label || shortTitle(section.title)));
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
          const head = el('span', {class: 'group-label'}, el('span', {class: 'ntitle', title: shortTitle(section.title)}, shortTitle(section.title)),
            el('span', {class: 'count'}, children.length));
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
          el('span', {class: 'group-label'}, el('span', {class: 'fname', title: doc.name}, doc.name.replace(/\.md$/, '')),
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

  function selectSection(key, {fromHash = false, initial = false} = {}) {
    const found = findSection(key);
    if (!found) return;
    const tree = $('#tree');
    tree.querySelectorAll('.selectable.selected').forEach(node => node.classList.remove('selected'));
    docs.selected = key;
    let row = tree.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (fromHash && !(row && shown(row))) {
      const parent = docOutline(found.doc).items.find(item => item.children.includes(found.index));
      const keys = ['d:' + found.doc.name].concat(parent ? [`s:${found.doc.name}::${found.doc.sections[parent.index].anchor}`] : []);
      if (!(initial && openState.collapsed(keys))) {
        for (const groupKey of keys) openState.set(groupKey, true);
        renderDocTree();
        row = tree.querySelector(`[data-key="${CSS.escape(key)}"]`);
        row?.scrollIntoView({block: 'center'});
      }
    }
    row?.classList.add('selected');
    if (decodeURIComponent(location.hash.slice(1)) !== key) history.replaceState(null, '', '#' + encodeURIComponent(key));
    const {doc, index} = found;
    const sections = sectionRange(doc, index);
    const head = doc.sections[index];
    const parent = docOutline(doc).items.find(item => item.children.includes(index));
    // 翻页按树中的顺序走全部章节（含收起的分组与隐藏的左栏），筛选或搜索时只走当前结果。
    const rows = [...tree.querySelectorAll('.selectable')];
    const position = rows.findIndex(node => node.dataset.key === key);
    const nav = (step, label) => {
      const target = position < 0 ? null : rows[position + step];
      return el('button', {type: 'button', disabled: !target, onclick: () => {
        selectSection(target.dataset.key);
        if (shown(target)) target.scrollIntoView({block: 'nearest'});
      }}, label);
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
    if (key && findSection(key)) selectSection(key, {fromHash: true, initial: true});
    else if (docs.data.documents[0]?.sections.length) {
      const first = docs.data.documents[0];
      selectSection(`${first.name}::${first.sections[0].anchor}`, {fromHash: true, initial: true});
    }
    window.addEventListener('hashchange', () => {
      const next = decodeURIComponent(location.hash.slice(1));
      if (next && next !== docs.selected) selectSection(next, {fromHash: true});
    });
  }

  // ---- 运行记录 ----

  const SERVICE_ACTIONS = {stop: '停止', start: '启动', restore: '自动恢复'};
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
          el('td', {}, `登记 ${item.recorded}` + (item.voided.length ? `，内容变化未登记 ${item.voided.length}` : '') + (item.not_executed.length ? `，未执行 ${item.not_executed.length}` : ''),
            item.service_events?.length ? el('ul', {class: 'service-events'}, item.service_events.map(e => el('li', {class: 'muted'},
              `${e.at} ${e.operator} ${SERVICE_ACTIONS[e.action] || e.action} ${e.service}` + (e.ok ? '' : '（未完成）')
              + (e.scenarios?.length ? `；停着期间执行 ${e.scenarios.join('、')}` : '') + (e.note ? `；${e.note}` : '')))) : null),
          el('td', {}, item.created_at), config.local ? el('td', {class: 'mono muted'}, item.session || '') : null);
      }), '没有运行'));
  }

  // ---- 收件箱与待答复 ----

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

  renderSigner();
  setupSidebar();
  setupTreeToggle();
  const start = {workbench: startWorkbench, sop: startSop, runs: startRuns, inbox: startInbox}[config.page];
  if (start) start().catch(error => $('main').prepend(el('p', {class: 'error-bar'}, error.message)));
})();
