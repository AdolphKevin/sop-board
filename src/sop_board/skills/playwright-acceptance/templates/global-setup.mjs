// 预检与场景共用依赖和连接实现；不通过时整轮不执行业务步骤。
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {cdpEndpoint, connectTimeout, checkPort} from './scenarios/connection.mjs';
import {readScope, webEvidence} from './scenarios/scope.mjs';

export default async function globalSetup() {
  const problems = [];
  let scope;
  try { scope = readScope(); } catch (error) { problems.push('执行范围不合格：' + error.message); }
  try {
    const require = createRequire(import.meta.url);
    const packageFile = require.resolve('@playwright/test/package.json');
    const pkg = JSON.parse(fs.readFileSync(packageFile, 'utf8'));
    const [major, minor] = pkg.version.split('.').map(Number);
    if (major < 1 || major === 1 && minor < 64) throw new Error('模板需要 @playwright/test >=1.64');
    if (!problems.length && (!scope || Object.keys(scope.scenarios).some(id => webEvidence(scope, id).video))) {
      const cli = path.resolve(path.dirname(packageFile), typeof pkg.bin === 'string' ? pkg.bin : pkg.bin.playwright);
      const output = execFileSync(process.execPath, [cli, 'install', '--dry-run', 'ffmpeg'], {encoding: 'utf8', timeout: 15000});
      const location = output.match(/Install location:\s*(.+)/)?.[1]?.trim();
      if (!location || !fs.existsSync(location) || !fs.readdirSync(location).length) throw new Error('缺少录屏 ffmpeg；运行 sop-board browser install');
    }
  } catch (error) { problems.push('依赖检查失败：' + error.message); }
  if (!problems.length) {
    try { await checkPort(cdpEndpoint(), Math.min(connectTimeout(), 5000)); }
    catch (error) { problems.push('浏览器检查失败：' + error.message); }
  }
  if (problems.length) throw new Error('受阻：环境检查未通过，本轮未执行任何场景：\n- ' + problems.join('\n- '));
}
