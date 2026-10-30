// 逐条落盘（D87）：每个用例结束就重写一份与 JSON 报告同结构的部分报告。
// 执行进程中途退出时，已完成的场景仍可用 `sop-board playwright convert --report <PW_OUT>/report.partial.json --allow-partial` 转换；
// 正常结束时以官方 report.json 为准。
import fs from 'node:fs';
import path from 'node:path';

function steps(items) {
  return (items || []).filter(step => step.category === 'test.step').map(step => ({
    title: step.title, duration: step.duration,
    ...(step.error ? {error: {message: step.error.message || String(step.error.value || '')}} : {}),
    steps: steps(step.steps),
  }));
}

export default class ProgressReporter {
  constructor(options = {}) {
    this.file = options.outputFile;
    this.report = {config: {metadata: {}}, suites: []};
    this.suites = new Map();
  }

  onBegin(config) {
    this.report.config = {metadata: config.metadata || {}};
  }

  onTestEnd(test, result) {
    if (!this.file) return;
    const file = path.relative(process.cwd(), test.location.file);
    let suite = this.suites.get(file);
    if (!suite) {
      suite = {title: file, file, specs: [], suites: []};
      this.suites.set(file, suite);
      this.report.suites.push(suite);
    }
    const annotations = [...test.annotations, ...(result.annotations || [])]
      .filter((item, index, all) => all.findIndex(other => other.type === item.type && other.description === item.description) === index);
    const errors = (result.errors || []).map(error => ({message: error.message || String(error.value || '')}));
    suite.specs.push({title: test.title, tests: [{
      expectedStatus: test.expectedStatus, annotations, tags: test.tags,
      results: [{
        status: result.status, startTime: result.startTime.toISOString(), duration: result.duration,
        ...(errors.length ? {error: errors[0]} : {}), errors,
        attachments: result.attachments.map(item => ({name: item.name, contentType: item.contentType,
          ...(item.path ? {path: item.path} : {}), ...(item.body ? {body: item.body.toString('base64')} : {})})),
        steps: steps(result.steps),
      }],
    }]});
    fs.mkdirSync(path.dirname(this.file), {recursive: true});
    const temporary = this.file + '.tmp';
    fs.writeFileSync(temporary, JSON.stringify(this.report));
    fs.renameSync(temporary, this.file);
  }
}
