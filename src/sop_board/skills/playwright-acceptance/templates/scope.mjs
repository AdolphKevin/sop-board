import fs from 'node:fs';
import {createHash} from 'node:crypto';

export function readScope(env = process.env) {
  if (!env.PW_SCOPE) return null;
  if (!env.PW_SCENARIO_FILE) throw new Error('PW_SCOPE 须配合 PW_SCENARIO_FILE 核对来源版本');
  const scope = JSON.parse(fs.readFileSync(env.PW_SCOPE, 'utf8'));
  const sha = createHash('sha256').update(fs.readFileSync(env.PW_SCENARIO_FILE)).digest('hex');
  if (!scope || !['project', 'batch'].every(key => typeof scope[key] === 'string' && scope[key].trim()) || scope.scenario_file_sha256 !== sha) throw new Error('scope 项目、批次或场景文件 SHA-256 不合格');
  if (!scope.scenarios || typeof scope.scenarios !== 'object' || Array.isArray(scope.scenarios) || !Object.keys(scope.scenarios).length || !Object.entries(scope.scenarios).every(([id, hash]) => id.trim() && typeof hash === 'string' && /^[a-f0-9]{64}$/.test(hash))) throw new Error('scope.scenarios 须包含非空场景编号及内容指纹');
  if ('web_evidence' in scope && (!scope.web_evidence || typeof scope.web_evidence !== 'object' || Array.isArray(scope.web_evidence)
      || Object.keys(scope.web_evidence).length !== Object.keys(scope.scenarios).length
      || !Object.keys(scope.scenarios).every(id => {
        const policy = scope.web_evidence[id];
        return policy && typeof policy === 'object' && !Array.isArray(policy)
          && Object.keys(policy).length === 2 && ['video', 'trace'].every(key => typeof policy[key] === 'boolean');
      }))) throw new Error('scope.web_evidence 须为每条场景声明 video、trace 布尔开关');
  return scope;
}

export function webEvidence(scope, id) {
  return scope?.web_evidence?.[id] || {video: true, trace: true};
}

export function scopeAnnotation(scope, id) {
  if (!scope) return null;
  if (!scope.scenarios[id]) throw new Error('测试编号不在本轮 scope 中：' + id);
  const binding = {project: scope.project, batch: scope.batch, scenario_file_sha256: scope.scenario_file_sha256, scenario: id, fingerprint: scope.scenarios[id]};
  return {type: 'sop-scope', description: JSON.stringify(binding, Object.keys(binding).sort())};
}
