// P15-01 知识卡片来源管理权限回归测试。
//
// 新卡片没有稳定的 knowledgeId，来源接口无法独立保存；页面必须禁止编辑，
// 避免用户在创建后发现已选择的来源被静默丢弃。
import assert from 'node:assert/strict'
import { canManageKnowledgeSources } from '../src/shared/sourceManagement.ts'

function testOnlyPersistedUnlockedCardCanManageSources() {
  assert.equal(canManageKnowledgeSources(true, false), false, '新建卡片不得编辑来源')
  assert.equal(canManageKnowledgeSources(false, true), false, '归档或合并卡片不得编辑来源')
  assert.equal(canManageKnowledgeSources(false, false), true, '普通已保存卡片可以编辑来源')
}

testOnlyPersistedUnlockedCardCanManageSources()
console.log('source-management: 3 tests OK')
