// 连接看板启动的 Chrome for Testing 执行场景验收。复制到运行目录后按需调整。
// 回归测试由随包 regression-distill 在沉淀时编写，执行会话不写测试。
//
// 环境变量（运行目录的 run.env 已给出，执行前 `set -a; . ./run.env; set +a`）：
//   PW_CDP_ENDPOINT      看板浏览器的调试地址；缺失时连接失败并记受阻。
//   PW_IDENTITIES        各身份浏览器的地址（identity.mjs 读取）。
//   PW_SCOPE、PW_SCENARIO_FILE  本轮范围快照与场景文件，见 references/run-scope.md。
//   PW_OUT               输出目录（运行目录内），由 playwright.config.mjs 读取。
//   PW_TAB               可选。复用 URL 含该子串的唯一标签页；缺省新开一页。
//   PW_FRAME             可选。surface-map 已确认的唯一 iframe 选择器。
//   PW_CASES             可选。scripts/case_tags.py 生成的 cases.json，用于给测试加上游用例 tag。
//   PW_CONNECT_TIMEOUT_MS 可选。连接超时，默认 30000。
//
// 运行时由 sop-board browser install 安装（固定版本、自带 ffmpeg）；不要自行 npx 安装另一个版本。
import { test as base, chromium } from '@playwright/test';
import fs from 'node:fs';
import {connectBroker, cdpEndpoint} from './connection.mjs';
import {readScope, scopeAnnotation, webEvidence} from './scope.mjs';
import {recording} from './recording.mjs';
export {cdpEndpoint};

const BLOCKED_PREFIX = '受阻：';

// 受阻：前置条件不满足、人机验证、登录过期、未知遮挡。to_scenarios.py 据此输出 blocked。
export function blocked(reason) {
  return new Error(BLOCKED_PREFIX + reason);
}

// 读取 cases.json，为场景生成 tag：['@SOP-02-P01', '@CASE-037', …]。tag 只能来自配对文件。
let casesCache;
export function tags(scenarioId) {
  if (casesCache === undefined) {
    const file = process.env.PW_CASES;
    casesCache = file && fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')).scenarios || {} : {};
  }
  return ['@' + scenarioId, ...(casesCache[scenarioId] || []).map((id) => '@' + id)];
}

export const test = base.extend({
  // 一个 worker 只建立一次 CDP 连接；每条场景按入口声明独立留证。
  _acceptanceTarget: [async ({}, use) => {
    let target;
    try { target = await connectBroker(chromium); }
    catch (error) { throw blocked('浏览器连接准备失败：' + error.message); }
    const action = Number(process.env.PW_ACTION_TIMEOUT_MS) > 0 ? Number(process.env.PW_ACTION_TIMEOUT_MS) : 15000;
    const navigation = Number(process.env.PW_NAVIGATION_TIMEOUT_MS) > 0 ? Number(process.env.PW_NAVIGATION_TIMEOUT_MS) : 30000;
    target.context.setDefaultTimeout(action);
    target.context.setDefaultNavigationTimeout(navigation);
    try { await use(target); }
    finally {
      try { await target.browser.close(); }
      catch { /* 浏览器已断开时不改变本轮结论 */ }
    }
  }, {scope: 'worker', timeout: 120000}],
  _scope: [async ({}, use, testInfo) => {
    try {
      const annotation = scopeAnnotation(readScope(), testInfo.title.split(/\s+/)[0]);
      if (annotation) testInfo.annotations.push(annotation);
    } catch (error) { throw blocked('执行范围不合格：' + error.message); }
    await use();
  }, {auto: true}],
  page: async ({_scope, _acceptanceTarget}, use, testInfo) => {
    let capture;
    let enteredTest = false, primaryError = false;
    const target = _acceptanceTarget;
    const before = new Set(target.context.pages());
    let crashed = false;
    target.page.once('crash', () => { crashed = true; });
    try {
      if (!target.browser.isConnected() || target.page.isClosed()) throw new Error('浏览器连接已断开或页面已关闭（可能是浏览器崩溃，见 sop-board browser status）；不在用例中重连');
      capture = recording(target.page, target.context, testInfo, webEvidence(readScope(), testInfo.title.split(/\s+/)[0]));
      await capture.start();
      enteredTest = true;
      await use(target.page);
      if (crashed) testInfo.errors.push({message: BLOCKED_PREFIX + '页面渲染进程崩溃（Target crashed）'});
      if (testInfo.errors.length) await scenarioFailure(target, testInfo, before);
    } catch (error) {
      primaryError = true;
      if (!enteredTest) throw blocked('浏览器或证据环境准备失败：' + error.message);
      throw error;
    } finally {
      const errors = [];
      try { if (capture) errors.push(...await capture.stop()); }
      catch { errors.push('证据清理异常'); }
      if (errors.length) {
        try { await testInfo.attach('环境清理问题', {body: errors.join('\n'), contentType: 'text/plain'}); }
        catch { /* 保留原业务失败；清理操作仍已逐项尝试。 */ }
        if (!primaryError && testInfo.status === 'passed') throw blocked('证据保存或连接清理不完整：' + errors.join('；'));
      }
    }
  },
});

// 业务失败只记本场景：留截图与页面结构快照，关闭本场景新开的页面，标为预期失败，
// worker 不重启、连接保留，后续场景照常执行。受阻、连接断开、目标页关闭仍按意外失败停止本轮。
function stopsRun(target, testInfo) {
  if (!target.browser.isConnected() || target.page.isClosed()) return true;
  return testInfo.errors.some(error => (error.message || '').includes(BLOCKED_PREFIX) ||
    /Target page, context or browser has been closed|Browser has been closed|has been disconnected|Target crashed/.test(error.message || ''));
}

async function scenarioFailure(target, testInfo, before) {
  const stop = stopsRun(target, testInfo);
  if (stop && !testInfo.errors.some(error => (error.message || '').includes(BLOCKED_PREFIX))) {
    // 连接中断或目标页关闭不是产品缺陷：注记为受阻，转换时据此输出 blocked。
    testInfo.annotations.push({type: 'blocked', description: BLOCKED_PREFIX + '浏览器连接中断、页面关闭或渲染进程崩溃，本轮停止，剩余场景未执行'});
  }
  if (!target.page.isClosed()) {
    try { await testInfo.attach('失败时截图', {body: await target.page.screenshot(), contentType: 'image/png'}); } catch { /* 页面不可用时只保留错误 */ }
    try {
      const tree = await target.page.locator('body').ariaSnapshot({timeout: 5000});
      await testInfo.attach('失败时页面结构', {body: tree, contentType: 'text/plain'});
    } catch { /* 页面结构不可读时只保留截图 */ }
  }
  if (stop) return;
  for (const page of target.context.pages()) {
    if (page !== target.page && !page.isClosed()) {
      try { if (!before.has(page)) await page.close(); } catch { /* 关闭失败不改变业务结论 */ }
    }
  }
  testInfo.annotations.push({type: 'scenario-failure', description: '业务失败，本轮继续执行后续场景'});
  testInfo.fail();
}

// 一条场景步骤对应一个 step：录屏中显示章节标题，report 中显示同名步骤。
export async function step(page, title, body) {
  return test.step(title, async () => {
    if (webEvidence(readScope(), test.info().title.split(/\s+/)[0]).video) {
      await page.screencast.showChapter(title, { duration: 1200 }).catch(() => {});
    }
    return body();
  });
}

// 截图并附到 report；target 可以是 page 或新标签页。
export async function shot(testInfo, target, name) {
  await testInfo.attach(name, { body: await target.screenshot(), contentType: 'image/png' });
}

export { expect } from '@playwright/test';
