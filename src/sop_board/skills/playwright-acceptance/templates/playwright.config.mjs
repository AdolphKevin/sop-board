// 场景验收配置。PW_OUT 指向业务仓库之外的目录；放进仓库会改变产品指纹。
import { defineConfig } from '@playwright/test';
import path from 'node:path';
import {readScope} from './scenarios/scope.mjs';

const out = process.env.PW_OUT || path.join(process.env.TMPDIR || '/tmp', 'playwright-acceptance');
// 缺省时间上限（D87）：单场景 4 分钟、定位或操作 15 秒、导航 30 秒；可用环境变量覆盖，记入报告 metadata.limits。
const limit = (name, value) => Number(process.env[name]) > 0 ? Number(process.env[name]) : value;
export const limits = {
  scenario: limit('PW_SCENARIO_TIMEOUT_MS', 240000),
  action: limit('PW_ACTION_TIMEOUT_MS', 15000),
  navigation: limit('PW_NAVIGATION_TIMEOUT_MS', 30000),
};

export default defineConfig({
  metadata: {sopScope: readScope(), limits},
  globalSetup: './global-setup.mjs',
  testDir: './scenarios',
  testMatch: /.*\.spec\.mjs/,
  workers: 1,
  fullyParallel: false,
  retries: 0,
  // 业务失败由 fixture 标为预期失败，worker 不重启、连接保留，后续场景照常执行；
  // 受阻、连接断开等意外失败仍在首个出现时停止本轮，避免新 worker 再次连接并触发授权。
  maxFailures: 1,
  timeout: limits.scenario,
  expect: { timeout: limits.action },
  outputDir: path.join(out, 'results'),
  reporter: [
    ['list'],
    ['html', { outputFolder: path.join(out, 'html'), open: 'never' }],
    ['json', { outputFile: path.join(out, 'report.json') }],
    // 逐条落盘：每个用例结束就重写一份同结构的部分报告；中途退出时用 --allow-partial 转换。
    ['./progress-reporter.mjs', { outputFile: path.join(out, 'report.partial.json') }],
    ['junit', { outputFile: path.join(out, 'junit.xml') }],
  ],
});
