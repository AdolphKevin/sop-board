// 接管已打开的 Chrome 执行场景验收。复制到仓库外的本轮任务目录后按需调整。
// 用户要求沉淀为产品回归测试时使用随包 regression-distill，由它选择层级并沿用产品已有设施。
//
// 环境变量：
//   PW_TAB               必填。目标标签页 URL 的子串，只操作匹配的那一个标签页。
//   PW_CDP_ENDPOINT      可选。CDP 地址（ws://… 或 http://127.0.0.1:9222）；缺省读 Chrome 的 DevToolsActivePort。
//   PW_CHROME_USER_DATA  可选。Chrome 用户数据目录；缺省按操作系统取默认目录。
//   PW_CASES             可选。scripts/case_tags.py 生成的 cases.json，用于给测试加上游用例 tag。
//   PW_OUT               可选。输出目录（仓库外），由 playwright.config.mjs 读取。
//   PW_CDP_MODE          browser（默认）或 target（Node >=22，仅目标页及其新弹窗）。
//   PW_CONNECT_TIMEOUT_MS 可选。连接超时，默认 60000。
//   PW_FRAME             可选。surface-map 已确认的唯一 iframe 选择器，预检等待 body 可见。
//   PW_SCOPE、PW_SCENARIO_FILE 可选。上游执行范围快照及原始场景文件，见 references/run-scope.md。
//
// 依赖：@playwright/test >= 1.64（page.screencast）；录屏需先执行 npx playwright install ffmpeg
//（ffmpeg 装在 PLAYWRIGHT_BROWSERS_PATH 或默认缓存目录，换机器、换该变量都要重装；global-setup.mjs 会在开跑前检查）。
import { test as base, chromium } from '@playwright/test';
import fs from 'node:fs';
import {connectTarget, cdpEndpoint} from './connection.mjs';
import {readScope, scopeAnnotation} from './scope.mjs';
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
  // 一个 worker 仅建立一次 CDP 连接；case 的录屏/trace 仍独立。
  // worker 重启仍会重连；须保留配置模板的 maxFailures: 1，不能仅依赖 workers/retries。
  _acceptanceTarget: [async ({}, use) => {
    let target;
    try { target = await connectTarget(chromium); }
    catch (error) { throw blocked('浏览器连接准备失败：' + error.message); }
    try { await use(target); }
    finally {
      try { await target.browser.close(); }
      catch { throw blocked('本轮 CDP 连接释放失败'); }
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
    try {
      const target = _acceptanceTarget;
      if (!target.browser.isConnected() || target.page.isClosed()) throw new Error('共享 CDP 连接已断开或目标页已关闭；不在用例中静默重连，请核对 Chrome 后重新开始本轮');
      capture = recording(target.page, target.context, testInfo);
      await capture.start();
      enteredTest = true;
      await use(target.page);
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

// 一条场景步骤对应一个 step：录屏中显示章节标题，report 中显示同名步骤。
export async function step(page, title, body) {
  return test.step(title, async () => {
    await page.screencast.showChapter(title, { duration: 1200 }).catch(() => {});
    return body();
  });
}

// 截图并附到 report；target 可以是 page 或新标签页。
export async function shot(testInfo, target, name) {
  await testInfo.attach(name, { body: await target.screenshot(), contentType: 'image/png' });
}

export { expect } from '@playwright/test';
