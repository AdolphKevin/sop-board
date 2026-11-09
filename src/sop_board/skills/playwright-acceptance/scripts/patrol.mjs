// 仅运行已审核声明；所有非静态请求在发出前核对，未知请求中止当前条目。
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {connectTarget} from '../templates/connection.mjs';
import {recording} from '../templates/recording.mjs';

const inputFile = path.resolve(process.argv[2]), out = path.dirname(inputFile);
const input = JSON.parse(fs.readFileSync(inputFile, 'utf8'));
let chromium;
for (const root of [process.env.PW_NODE_ROOT, input.node_root, out, process.cwd()].filter(Boolean)) {
  try { ({chromium} = createRequire(path.join(path.resolve(root), 'package.json'))('@playwright/test')); break; } catch {}
}
const report = {version: 1, started_at: input.started_at, web_evidence: input.web_evidence, plan_sha256: input.plan_sha256, entries: []};
const staticTypes = new Set(['document', 'script', 'stylesheet', 'image', 'font', 'media']);
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])]));
}
function approval(item) {
  const copy = {...item}; delete copy.review;
  return createHash('sha256').update(JSON.stringify(canonical(copy))).digest('hex');
}
function matches(url, pattern) {
  const value = new URL(url), base = new URL(input.address);
  const full = /^https?:\/\//.test(pattern);
  if (!full && value.origin !== base.origin) return false;
  const text = full ? value.origin + value.pathname : value.pathname;
  const escaped = pattern.split('**').map(part => part.split('*').map(s => s.replace(/[.+?^$()|[\]{}\\]/g, '\\$&')).join('[^/]*')).join('.*');
  return new RegExp('^' + escaped + '$').test(text);
}
function located(page, spec) {
  if (!spec || typeof spec !== 'object') throw new Error('定位声明缺失');
  const root = spec.frame ? page.frameLocator(spec.frame) : page;
  if (spec.css) return root.locator(spec.css);
  if (spec.testid) return root.getByTestId(spec.testid);
  if (spec.role) return root.getByRole(spec.role, {name: spec.name, exact: spec.exact !== false});
  if (spec.text) return root.getByText(spec.text, {exact: spec.exact !== false});
  if (spec.expression) {
    const m = /^(locator|getByTestId|getByText|getByLabel|getByPlaceholder)\((['"])(.*?)\2\)$/.exec(spec.expression);
    if (m) return root[m[1]](m[3]);
    throw new Error('定位表达式不在声明语法中，请改成 css/testid/role/text 对象');
  }
  throw new Error('定位声明缺失');
}
function validate(item) {
  for (const key of ['id', 'entry', 'pages', 'allow_requests', 'reach', 'targets', 'forbidden_actions', 'uncovered', 'source', 'identity', 'review'])
    if (!Object.hasOwn(item, key)) throw new Error('巡检条目缺少 ' + key);
  if (!Array.isArray(item.pages) || !item.pages.length || !item.pages.every(p => typeof p === 'string' && p.length)) throw new Error('pages 须声明允许地址');
  if (!Array.isArray(item.allow_requests) || !item.allow_requests.every(r => /^[A-Z]+$/.test(r.method) && r.path && r.purpose?.trim() && !r.purpose.startsWith('待审核'))) throw new Error('请求须逐条填写 method/path 与已核实业务用途');
  if (!Array.isArray(item.reach) || !Array.isArray(item.targets) || !item.targets.length) throw new Error('须补齐到达步骤与检查目标');
  if (!Array.isArray(item.forbidden_actions) || !item.forbidden_actions.length || !Array.isArray(item.uncovered)) throw new Error('须声明禁止动作与未覆盖范围');
  if (!Number.isFinite(Date.parse(item.review.at))) throw new Error('审核时间不合格');
  if (item.review.plan_sha256 && item.review.plan_sha256 !== approval(item)) throw new Error('审核后计划已变化');
  for (const target of item.targets) if (!Number.isSafeInteger(target.count) || target.count < 1) throw new Error('目标 count 须为正整数');
}
let fatal;
try {
  if (!chromium) throw new Error('找不到当前工作区 @playwright/test；安装到数据目录或设置 PW_NODE_ROOT');
  for (const [index, item] of input.plan.entries.entries()) {
    if (item.entry !== input.entry) continue;
    const result = {id: item.id, entry: item.entry, page: item.page, status: 'not-executed', requests: [], uncovered: item.uncovered, evidence: []};
    report.entries.push(result);
    if (!item.review?.by?.trim() || !item.review?.at?.trim()) { result.reason = '未审核，未执行'; continue; }
    let target, page, capture, route, context, failure, activeControl;
    const pendingRequests = new Set();
    const folder = path.join(out, 'entries', String(index + 1));
    fs.mkdirSync(folder, {recursive: true});
    let stopped;
    const stopSignal = new Promise(resolve => {stopped = resolve;});
    const stop = reason => { if (!failure) {failure = reason; stopped(reason);} };
    const active = async action => {
      if (failure) throw new Error(failure);
      const value = await Promise.race([Promise.resolve().then(action), stopSignal.then(reason => {throw new Error(reason);})]);
      if (failure) throw new Error(failure);
      return value;
    };
    try {
      validate(item);
      const identity = item.identity ? input.identities.find(i => i.id === item.identity) : null;
      if (item.identity && !identity) throw new Error('条目身份没有在入口声明');
      target = await connectTarget(chromium, {...process.env, PW_CDP_ENDPOINT: identity?.cdp || input.cdp || process.env.PW_CDP_ENDPOINT, PW_TAB: input.tab});
      context = target.context;
      // 在已有登录 context 中创建本轮专用页，拦截在第一笔导航之前安装。
      page = await context.newPage();
      page.setDefaultTimeout(15000); page.setDefaultNavigationTimeout(30000);
      const cdp = await context.newCDPSession(page);
      await cdp.send('Network.setBypassServiceWorker', {bypass: true}); await cdp.detach();
      const allowedPage = url => item.pages.some(p => matches(url, p));
      const allowedRequest = (method, url) => item.allow_requests.some(r => r.method === method && matches(url, r.path));
      const routeRequest = async handler => {
        const request = handler.request();
        let response;
        try {
          let owner;
          try { owner = request.frame().page(); } catch { stop('无法识别请求所属页面'); await handler.abort(); return; }
          if (owner !== page) { await handler.continue(); return; }
          let url = new URL(request.url()), method = request.method(), body = request.postDataBuffer();
          const type = request.resourceType(), navigation = request.isNavigationRequest();
          const permit = (verb, address) => {
            if (navigation && !allowedPage(address.href)) return '导航地址未在 pages 声明：' + address.origin + address.pathname;
            if (!staticTypes.has(type) && !allowedRequest(verb, address.href)) return '未声明请求，已在发出前中止：' + verb + ' ' + address.pathname;
            return '';
          };
          let headers = {...await request.allHeaders()};
          // API请求不能替缺少Cookie的浏览器请求自动附加登录凭据。
          headers.cookie ??= '';
          const deadline = Date.now() + 30000;
          for (let hop = 0; hop <= 20; hop++) {
            if (failure) { await handler.abort(); return; }
            const reason = permit(method, url);
            const record = {method, path: url.pathname, type, hop, allowed: !reason, sent: false};
            result.requests.push(record);
            if (reason) { stop(reason); await handler.abort(); return; }
            if (Date.now() >= deadline) throw new Error('请求链核对超时');
            record.sent = true;
            response = await handler.fetch({url: url.href, method, headers, postData: body ?? Buffer.alloc(0),
              maxRedirects: 0, maxRetries: 0, timeout: deadline - Date.now()});
            const status = response.status();
            if (![301, 302, 303, 307, 308].includes(status)) {
              if (failure) { await handler.abort(); return; }
              await handler.fulfill({response}); return;
            }
            const location = response.headers().location;
            if (!location) throw new Error('重定向响应没有Location');
            const next = new URL(location, url);
            if (!['http:', 'https:'].includes(next.protocol) || next.username || next.password) throw new Error('重定向地址不能安全执行');
            const nextMethod = status === 303 && method !== 'HEAD' || [301, 302].includes(status) && method === 'POST' ? 'GET' : method;
            const nextReason = permit(nextMethod, next);
            if (nextReason) {
              result.requests.push({method: nextMethod, path: next.pathname, type, hop: hop + 1, allowed: false, sent: false});
              stop(nextReason); await handler.abort(); return;
            }
            // 不能以最终内容冒充浏览器实际导航地址，亦不转发跨来源凭据。
            if (navigation || next.origin !== url.origin) {
              result.requests.push({method: nextMethod, path: next.pathname, type, hop: hop + 1, allowed: true, sent: false});
              stop(navigation ? '导航重定向无法安全保持浏览器地址语义；请使用已审核的最终直达地址'
                : '跨来源重定向未执行；不能安全保持浏览器凭据与来源语义');
              await handler.abort(); return;
            }
            if (hop === 20) throw new Error('重定向次数超过上限');
            if (nextMethod !== method) {
              body = Buffer.alloc(0); headers = {...headers};
              delete headers['content-type']; delete headers['content-length'];
            }
            method = nextMethod; url = next;
            await response.dispose(); response = null;
          }
        } catch {
          stop(failure || '请求链无法安全核对：' + request.method() + ' ' + new URL(request.url()).pathname);
          await handler.abort().catch(() => {});
        } finally {
          if (response) await response.dispose().catch(() => {});
        }
      };
      route = async handler => {
        const pending = routeRequest(handler);
        pendingRequests.add(pending);
        try { await pending; } finally { pendingRequests.delete(pending); }
      };
      await context.route('**/*', route);
      if (!context.routeWebSocket) throw new Error('当前 Playwright 不支持 WebSocket 发出前拦截');
      await page.routeWebSocket('**/*', socket => {
        const record = {method: 'GET', path: new URL(socket.url()).pathname, type: 'websocket'};
        record.allowed = !failure && allowedRequest('GET', socket.url().replace(/^ws/, 'http'));
        result.requests.push(record);
        if (!record.allowed) { stop('未声明 WebSocket，连接已中止：GET ' + record.path); socket.close(); }
        else socket.connectToServer();
      });
      page.on('framenavigated', frame => {
        if (frame.url() !== 'about:blank' && !allowedPage(frame.url())) stop('导航地址未在 pages 声明：' + frame.url().split('?')[0]);
      });
      page.on('popup', popup => { stop('未声明的新页面，已停止条目'); popup.close().catch(() => {}); });
      const info = {title: 'patrol ' + item.id, outputPath: name => path.join(folder, name),
        attach: async (name, attachment) => result.evidence.push({name, path: path.relative(out, attachment.path), contentType: attachment.contentType})};
      capture = recording(page, context, info, input.web_evidence);
      await capture.start();
      for (const step of item.reach) await active(async () => {
        if (step.action === 'goto') await page.goto(new URL(step.path, input.address).href, {waitUntil: 'networkidle'});
        else if (step.action === 'click') {
          const element = located(page, step.locator);
          if (await element.count() !== 1) throw new Error('到达步骤不能唯一定位');
          await element.click();
          await page.waitForLoadState('networkidle');
        } else throw new Error('到达步骤只允许已审核 goto/click：' + step.action);
      });
      for (const check of item.targets) await active(async () => {
        activeControl = check.ref;
        const element = located(page, check.locator);
        await element.first().waitFor({state: 'attached'});
        const count = await element.count();
        if (count !== check.count) throw new Error('定位数量不符：' + check.ref + '，要求 ' + check.count + '，实际 ' + count);
        for (let i = 0; i < count; i++) await element.nth(i).waitFor({state: 'visible'});
      });
      if (failure) throw new Error(failure);
      result.status = 'pass';
    } catch (error) {
      result.status = 'fail'; result.reason = failure || error.message;
      const screenshot = path.join(folder, 'failure.png');
      if (page && !page.isClosed()) {
        try { await page.screenshot({path: screenshot, timeout: 5000}); result.evidence.push({name: '失败截图', path: path.relative(out, screenshot), contentType: 'image/png'}); }
        catch (error_) { result.screenshot_error = error_.message; }
      } else result.screenshot_error = '浏览器或页面不可用，无法截图';
    } finally {
      const errors = [];
      let lastState;
      if (page && !page.isClosed()) {
        try { lastState = await page.screenshot({timeout: 5000}); } catch {}
      }
      if (capture) errors.push(...await capture.stop().catch(error => [error.message]));
      if (failure && page && !page.isClosed()) {
        try { lastState = await page.screenshot({timeout: 5000}); } catch {}
      }
      if (page) await page.close().catch(error => errors.push(error.message));
      if (route && context) await context.unroute('**/*', route).catch(error => errors.push(error.message));
      await Promise.allSettled([...pendingRequests]);
      if (target) await target.browser.close().catch(error => errors.push(error.message));
      if (failure || errors.length) {
        result.status = 'fail';
        result.reason ||= failure || '巡检证据保存或连接清理不完整';
        if (failure) result.request_failure = failure;
        if (errors.length) result.cleanup_errors = errors;
        if (!result.evidence.some(e => e.name === '失败截图')) {
          if (lastState) {
            const screenshot = path.join(folder, 'failure.png'); fs.writeFileSync(screenshot, lastState);
            result.evidence.push({name: '失败截图', path: path.relative(out, screenshot), contentType: 'image/png'});
          } else result.screenshot_error ||= '页面不可用，无法保存失败现场';
        }
      }
      if (result.status === 'fail') {
        fs.appendFileSync(path.join(out, 'atlas-feedback.jsonl'), JSON.stringify({entry: item.entry, page: item.page,
          control: activeControl, kind: /定位/.test(result.reason) ? 'locator' : 'reach', detail: result.reason,
          evidence: result.evidence.find(e => e.name === '失败截图')?.path || result.evidence.map(e => e.path).join('、')}) + '\n');
      }
      fs.writeFileSync(path.join(out, 'patrol-report.json'), JSON.stringify(report, null, 2) + '\n');
    }
  }
} catch (error) { fatal = error.message; report.error = fatal; }
report.finished_at = new Date().toISOString();
report.ok = !fatal && !report.entries.some(r => r.status === 'fail');
fs.writeFileSync(path.join(out, 'patrol-report.json'), JSON.stringify(report, null, 2) + '\n');
process.stdout.write(JSON.stringify({ok: report.ok, report: path.join(out, 'patrol-report.json'), entries: report.entries.map(({id, status, reason}) => ({id, status, reason}))}, null, 2) + '\n');
if (!report.ok) process.exitCode = 1;
