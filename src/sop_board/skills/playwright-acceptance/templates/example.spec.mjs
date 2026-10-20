// 示例：Shopify 后台内嵌应用的收件箱场景。标题以场景编号开头，tag 来自 PW_CASES。
// 每个 step 的标题照抄场景步骤；前置条件不满足时 throw blocked(...)，不要跳过。
import { test, expect, step, shot, blocked, tags } from './fixtures.mjs';

const SCENARIO = 'SOP-02-P01';
const MESSAGE = '123456789';

test(`${SCENARIO} 人工接管 AI 会话并发送消息`, { tag: tags(SCENARIO) }, async ({ page }, testInfo) => {
  const app = page.frameLocator('iframe[name="app-iframe"]');

  await step(page, '打开 Shopify 后台 > 应用 > CWILL AI Chat test 首页', async () => {
    await page.goto(page.url().replace(/\/apps\/chatwill-test\/.*/, '/apps/chatwill-test/home'));
    await expect(app.getByText('Escalated Tickets', { exact: true })).toBeVisible({ timeout: 30000 });
    await shot(testInfo, page, '首页');
  });

  await step(page, '在应用首页 "Escalated Tickets" 卡片中点击按钮 "Go to inbox"', async () => {
    await app.getByRole('button', { name: 'Go to inbox' }).click();
  });

  // 收件箱由 App Bridge 全屏 modal 打开（Chat.tsx: shopify.modal.show('chat-modal')）；按 src 排除 0×0 的预加载副本。
  const inbox = page.frameLocator('iframe[name^="frame://chatwill-test/modal/"][src*="/inbox/chat"]');
  // 会话列表是 react-virtualized 列表，行元素没有可访问角色。
  const firstConversation = inbox.locator('.ReactVirtualized__Grid__innerScrollContainer > div').first();

  await step(page, '在收件箱弹窗的会话列表中点击第一项', async () => {
    await expect(firstConversation).toBeVisible();
    const summary = await firstConversation.innerText();
    await testInfo.attach('第一项会话', { body: summary, contentType: 'text/plain' });
    if (!/Assistant/.test(summary)) throw blocked('前置条件不满足：会话列表第一项不由 AI 处理（未显示 "Assistant"）');
    await firstConversation.click();
    await shot(testInfo, page, '打开第一项会话');
  });

  const join = inbox.getByRole('button', { name: 'Join conversation' });
  const editor = inbox.locator('[contenteditable="true"]').first();

  await step(page, '在 "AI is processing" 提示下点击按钮 "Join conversation"', async () => {
    await expect(join).toBeVisible();
    await join.click();
    await expect(join).toBeHidden();
    await expect(editor).toBeVisible();
    await shot(testInfo, page, '接管后');
  });

  await step(page, `在输入框 "Start writing..." 中输入 ${MESSAGE}`, async () => {
    await editor.click();
    await editor.pressSequentially(MESSAGE);
    await expect(editor).toHaveText(MESSAGE);
  });

  await step(page, '点击按钮 "Send"', async () => {
    // forbidden_effects：出现 "Send email" 表示客户离线，走邮件渠道，不是本场景的分支。
    const sendEmail = inbox.getByRole('button', { name: 'Send email' });
    if (await sendEmail.isVisible()) throw blocked('发送按钮为 "Send email"，客户离线，与 initial_state 不符');
    await inbox.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(editor).toHaveText('');
    await expect(inbox.getByText(MESSAGE, { exact: true }).first()).toBeVisible();
    await expect(firstConversation).toContainText('Me');
    await page.waitForTimeout(1500);
    await shot(testInfo, page, '发送后');
  });
});
