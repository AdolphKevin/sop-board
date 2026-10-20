import fs from 'node:fs';

// 控制器先创建再 start，初始化中途失败时仍可 stop；每一项清理相互独立。
export function recording(page, context, testInfo) {
  const video = testInfo.outputPath('video.webm');
  const trace = testInfo.outputPath('trace.zip');
  let videoAttempted = false, traceAttempted = false, listening = false;
  const popups = [], errors = [];
  const onPage = popup => {
    const file = testInfo.outputPath(`video-tab-${popups.length + 1}.webm`);
    const item = {popup, file, attempted: false};
    item.started = (async () => {
      if (await popup.opener() !== page) return;
      await popup.waitForLoadState('domcontentloaded');
      const ratio = await popup.evaluate(() => window.innerHeight / window.innerWidth);
      item.attempted = true;
      await popup.screencast.start({path: file, size: {width: 800, height: Math.round(800 * ratio)}});
    })().catch(() => errors.push('场景打开的新标签页录屏失败'));
    popups.push(item);
  };
  async function safely(name, action) {
    try { await action(); } catch { errors.push(name); }
  }
  async function attach(name, file, contentType) {
    if (fs.existsSync(file)) await safely(name + '附件登记失败', () => testInfo.attach(name, {path: file, contentType}));
    else errors.push(name + '文件未生成');
  }
  return {
    async start() {
      if (!page.screencast?.start) throw new Error('Playwright 缺少 screencast API，请安装 @playwright/test >=1.64');
      const size = await page.evaluate(() => ({width: window.innerWidth, height: window.innerHeight}));
      videoAttempted = true;
      await page.screencast.start({path: video, size});
      await page.screencast.showActions({position: 'top', duration: 800});
      context.on('page', onPage); listening = true;
      traceAttempted = true;
      await context.tracing.start({screenshots: true, snapshots: true, title: testInfo.title});
    },
    async stop() {
      if (listening) await safely('新标签页监听移除失败', () => context.off('page', onPage));
      if (traceAttempted) {
        await safely('trace 保存失败', () => context.tracing.stop({path: trace}));
        await attach('trace', trace, 'application/zip');
      }
      if (videoAttempted) {
        await safely('录屏动作提示清理失败', () => page.screencast.hideActions());
        await safely('录屏停止失败', () => page.screencast.stop());
        await attach('video', video, 'video/webm');
      }
      for (const [index, item] of popups.entries()) {
        await item.started;
        if (!item.attempted) continue;
        await safely('新标签页录屏停止失败', () => item.popup.screencast.stop());
        await attach(`video-新标签页-${index + 1}`, item.file, 'video/webm');
      }
      return [...errors];
    },
  };
}
