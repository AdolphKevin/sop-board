// 通用只读示例：将映射与步骤替换为项目 surface-map.md 中已核对的内容。
// 此文件中的视图名和选择器只是示例，不是业务预期或跨项目默认值。
import { test, expect, step, shot, blocked, tags } from './fixtures.mjs';

const SCENARIO = 'SOP-EXAMPLE-P01';
const targetMap = {
  // 若目标在主文档中，frame 填 null；若在弹窗 frame 中，填写其已核对的唯一选择器。
  frame: null,
  heading: 'Records',
  entryFrame: null,
  openButton: 'Open records',
};
const root = (page, frame) => frame ? page.frameLocator(frame) : page;

test(`${SCENARIO} 查看目标视图`, {tag: tags(SCENARIO)}, async ({page}, info) => {
  const view = root(page, targetMap.frame);
  const ready = view.getByRole('heading', {name: targetMap.heading, exact: true});

  await step(page, '进入或复用目标视图', async () => {
    // 已打开的视图直接使用；该判断只用于导航准备，不跳过场景要求的刷新或重开。
    if (!await ready.isVisible()) {
      const entry = root(page, targetMap.entryFrame).getByRole('button', {
        name: targetMap.openButton, exact: true,
      });
      if (await entry.count() !== 1 || !await entry.isVisible()) {
        throw blocked('AUTOMATION-SURFACE：目标未就绪且不能唯一定位入口；停止动作，重新只读探测界面地图');
      }
      await entry.click();
    }
    await expect(ready).toBeVisible();
    await shot(info, page, '目标视图');
  });
});
