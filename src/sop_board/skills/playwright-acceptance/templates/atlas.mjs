// 交互地图操作库（D86、D87）：按任务目录 atlas.json（PW_ATLAS）打开页面、取控件，执行者每一步只写 2～4 行。
// 地图只说明怎么操作，不提供预期；断言仍按场景原文写，结论以实际页面为准。
// 地图里找不到时抛出 AtlasGap：执行者把缺口写进任务目录 atlas-feedback.jsonl，再只读探测或查阅前端源码补足。
//
// 环境变量：
//   PW_ATLAS        任务目录 atlas.json 的路径
//   PW_ATLAS_ENTRY  可选，入口名；地图只有一个入口时可省略
//   PW_BASE_URL     可选，入口地址；缺省用当前页面的源
import fs from 'node:fs';

export class AtlasGap extends Error {}

let cache;
function document() {
  if (!cache) {
    const file = process.env.PW_ATLAS;
    if (!file || !fs.existsSync(file)) throw new AtlasGap('未提供 atlas.json（PW_ATLAS）');
    const entries = JSON.parse(fs.readFileSync(file, 'utf8')).entries || [];
    const wanted = process.env.PW_ATLAS_ENTRY;
    cache = wanted ? entries.find(item => item.entry === wanted) : entries.length === 1 ? entries[0] : null;
    if (!cache) throw new AtlasGap('atlas.json 有多个入口，请设置 PW_ATLAS_ENTRY');
  }
  return cache;
}

export function pageSpec(id) {
  const found = document().pages.find(page => page.id === id);
  if (!found) {
    const known = document().index.some(page => page.id === id);
    throw new AtlasGap(known ? `页面 ${id} 不在本轮节选中，可用 sop-board atlas show --entry … --page ${id} 查看` : `地图中没有页面 ${id}`);
  }
  return found;
}

// 引用：<页面>/<控件>、<页面>/<弹窗或表格>/<操作或字段或列>、<页面>/<弹窗>（弹窗本身）。
export function element(ref) {
  const [pageId, first, second] = ref.split('/');
  const page = pageSpec(pageId);
  if (second === undefined) {
    const found = page.controls.find(c => c.id === first) || page.dialogs.find(d => d.id === first) || page.tables.find(t => t.id === first);
    if (found) return found;
  } else {
    const dialog = page.dialogs.find(d => d.id === first);
    const table = page.tables.find(t => t.id === first);
    const found = dialog && (dialog.actions.find(a => a.id === second) || dialog.fields.find(f => f.id === second))
      || table && (table.row_actions.find(a => a.id === second) || table.columns.find(c => c.id === second));
    if (found) return found;
  }
  throw new AtlasGap(`地图中没有 ${ref}`);
}

const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const ROLES = new Set(['button', 'link', 'menuitem', 'tab', 'textbox', 'combobox', 'checkbox', 'radio', 'switch', 'option', 'heading', 'dialog', 'row']);

// 定位：有 data-testid 用 testid；角色已知时用角色加任一声明语言的名称；否则执行地图记录的定位器表达式。
export function locate(page, ref) {
  const item = element(ref);
  if (item.testid) return page.getByTestId(item.testid);
  const names = (item.name || item.label || item.title || []).map(t => t.text).filter(text => !/^<.*>$/.test(text));
  if (ROLES.has(item.role) && names.length) {
    return page.getByRole(item.role, {name: new RegExp('^\\s*(' + names.map(escape).join('|') + ')\\s*$')}).first();
  }
  if (!item.locator) throw new AtlasGap(`${ref} 没有可用的定位方式`);
  return new Function('page', 'return page.' + item.locator)(page);
}

// 按路由打开页面并等待就绪；params 替换路由中的 :name 或 {name}。
export async function openPage(page, id, {params = {}, base = process.env.PW_BASE_URL} = {}) {
  const spec = pageSpec(id);
  if (!spec.route) throw new AtlasGap(`页面 ${id} 没有路由，请按 reach 经控件打开`);
  const route = spec.route.replace(/[:{]([A-Za-z_]+)\}?/g, (_, key) => encodeURIComponent(params[key] ?? ''));
  const origin = base || new URL(page.url()).origin;
  const url = new URL(route, origin);
  const current = new URL(page.url());
  url.search = current.search;
  await page.goto(url.toString());
  await ready(page, id);
  return spec;
}

// 就绪：页面上至少一个记录过的控件可见。
export async function ready(page, id) {
  const spec = pageSpec(id);
  const refs = spec.controls.slice(0, 3).map(c => `${id}/${c.id}`);
  if (!refs.length) return;
  await Promise.any(refs.map(ref => locate(page, ref).waitFor({state: 'visible'})));
}

// 点击并等待地图记录的请求完成（没有记录请求时只点击）。返回响应或 null。
export async function act(page, ref, action = locator => locator.click()) {
  const item = element(ref);
  const request = item.requests && item.requests[0];
  if (!request) { await action(locate(page, ref)); return null; }
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === request.method.toUpperCase() && new URL(r.url()).pathname.endsWith(request.path.split('?')[0])),
    action(locate(page, ref)),
  ]);
  return response;
}

// 证据：截图与文字说明附到报告。
export async function shot(testInfo, page, name) {
  await testInfo.attach(name, {body: await page.screenshot(), contentType: 'image/png'});
}

export async function note(testInfo, name, text) {
  await testInfo.attach(name, {body: text, contentType: 'text/plain'});
}
