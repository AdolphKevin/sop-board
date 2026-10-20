// 场景验收配置。PW_OUT 指向业务仓库之外的目录；放进仓库会改变产品指纹。
import { defineConfig } from '@playwright/test';
import path from 'node:path';
import {readScope} from './scenarios/scope.mjs';

const out = process.env.PW_OUT || path.join(process.env.TMPDIR || '/tmp', 'playwright-acceptance');

export default defineConfig({
  metadata: {sopScope: readScope()},
  globalSetup: './global-setup.mjs',
  testDir: './scenarios',
  testMatch: /.*\.spec\.mjs/,
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 120000,
  expect: { timeout: 15000 },
  outputDir: path.join(out, 'results'),
  reporter: [
    ['list'],
    ['html', { outputFolder: path.join(out, 'html'), open: 'never' }],
    ['json', { outputFile: path.join(out, 'report.json') }],
    ['junit', { outputFile: path.join(out, 'junit.xml') }],
  ],
});
