<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import http from '../api/client'
import DocPreview from '../components/DocPreview.vue'
import { useResponsive } from '../composables/useResponsive'

/**
 * 人工审核：左边队列（按不确定度排序）、中间原件预览、右边判定面板。
 * 支持逐件与同模板批次两种节奏，键盘全程可完成（J/K 切换，A 归档，S 跳过）。
 */
const { isCompact } = useResponsive()
const mode = ref<'single' | 'cluster'>((localStorage.getItem('docflow_review_mode') as any) || 'single')
const queue = ref<any[]>([])
const clusters = ref<any[]>([])
const cats = ref<any[]>([])
const types = ref<string[]>([])
const templates = ref<any[]>([])
const suggestions = ref<any[]>([])
const activeId = ref<number | null>(null)
const activeCluster = ref(0)
const detail = ref<any>(null)
const busy = ref(false)
const panel = ref('doc')

const pick = ref<any>({ category_id: null as number | null, document_type: '', filename: '', template_id: null as number | null })
const saveSample = ref(true)

const activeDoc = computed(() => queue.value.find((d) => d.id === activeId.value) || null)
const activeGroup = computed(() => clusters.value[activeCluster.value] || null)
const queueIds = computed(() => queue.value.map((d) => d.id))

async function loadQueue() {
  queue.value = await http.get('/review')
  clusters.value = await http.get('/review/clusters')
  if (!queue.value.some((d) => d.id === activeId.value)) activeId.value = queue.value[0]?.id ?? null
  if (activeCluster.value >= clusters.value.length) activeCluster.value = 0
}

async function loadDetail(id: number) {
  detail.value = await http.get(`/review/${id}`)
  pick.value = {
    category_id: detail.value.suggested_category_id ?? null,
    document_type: detail.value.document_type || '',
    filename: '',
    template_id: null,
  }
}

watch(activeId, async (id) => {
  if (id) {
    await loadDetail(id)
  } else {
    detail.value = null
  }
})

watch(mode, (v) => localStorage.setItem('docflow_review_mode', v))

function why(d: any): string {
  const det = d?.confidence_detail || {}
  const parts: string[] = []
  if (det.rule != null) parts.push(`规则 ${Math.round((det.rule || 0) * 100)}`)
  if (det.field != null) parts.push(`字段 ${Math.round((det.field || 0) * 100)}`)
  if (det.keyword != null) parts.push(`关键词 ${Math.round((det.keyword || 0) * 100)}`)
  if (det.ai != null) parts.push(`AI ${Math.round((det.ai || 0) * 100)}`)
  if (det.reason) parts.push(det.reason)
  return parts.join(' · ')
}

async function approve() {
  const d = activeDoc.value
  if (!d) return
  busy.value = true
  try {
    await http.post(`/review/${d.id}/approve`, {
      category_id: pick.value.category_id,
      document_type: pick.value.document_type || null,
      filename: pick.value.filename || null,
      rename_template_id: pick.value.template_id,
      save_as_sample: saveSample.value,
    })
    ElMessage.success(`#${d.id} 已归档${saveSample.value ? '并存为样例' : ''}`)
    await step(1)
    await loadQueue()
  } finally {
    busy.value = false
  }
}

async function skip() {
  const d = activeDoc.value
  if (!d) return
  await http.post(`/review/${d.id}/skip`)
  ElMessage.success('已跳过（保留待整理）')
  await step(1)
  await loadQueue()
}

async function recycle() {
  const d = activeDoc.value
  if (!d) return
  await ElMessageBox.confirm(`把「${d.filename}」移入回收站？`, '删除', { type: 'warning' })
  await http.delete(`/documents/${d.id}`)
  await step(1)
  await loadQueue()
}

async function step(delta: number) {
  const i = queueIds.value.indexOf(activeId.value ?? -1)
  const next = queueIds.value[Math.min(queueIds.value.length - 1, Math.max(0, i + delta))]
  if (next && next !== activeId.value) activeId.value = next
  else if (!queueIds.value.length) activeId.value = null
  await nextTick()
}

async function approveSameType() {
  const d = activeDoc.value
  if (!d) return
  const same = queue.value.filter((x) => x.document_type === d.document_type && x.fingerprint === d.fingerprint)
  await ElMessageBox.confirm(`确认 ${same.length} 个同模板文档按「${d.document_type}」归档？`, '批量确认')
  const r: any = await http.post('/review/batch-approve', { ids: same.map((x) => x.id), category_id: pick.value.category_id })
  ElMessage.success(`${r.done.length} 成功${r.errors?.length ? `，${r.errors.length} 失败` : ''}`)
  await loadQueue()
}

async function confirmCluster() {
  const g = activeGroup.value
  if (!g) return
  busy.value = true
  try {
    const r: any = await http.post('/review/confirm-cluster', {
      fingerprint: g.fingerprint,
      category_id: null,
      document_type: g.document_type || undefined,
      base_filename: g.base_filename || null,
    })
    if (r.errors?.length) ElMessage.warning(`${r.done.length} 成功，${r.errors.length} 失败`)
    else ElMessage.success(`本组 ${r.done.length} 个文档已归档`)
    await loadQueue()
    await loadSuggestions()
  } finally {
    busy.value = false
  }
}

async function adopt(s: any) {
  await http.post('/rules/learning/adopt', s)
  ElMessage.success('已生成候选规则（默认停用，可在规则中心启用）')
  await loadSuggestions()
}

async function loadSuggestions() {
  suggestions.value = await http.get('/rules/learning/suggestions')
}

async function saveField(row: any) {
  const fields: Record<string, string> = {}
  fields[row.name] = row.value
  await http.patch(`/documents/${detail.value.id}`, { fields })
  ElMessage.success('字段已修正')
}

function onKey(e: KeyboardEvent) {
  const t = e.target as HTMLElement
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(t?.tagName)) return
  if (mode.value === 'single') {
    if (e.key === 'j') step(1)
    else if (e.key === 'k') step(-1)
    else if (e.key === 'a' || e.key === 'Enter') approve()
    else if (e.key === 's') skip()
  } else {
    if (e.key === 'j') activeCluster.value = Math.min(clusters.value.length - 1, activeCluster.value + 1)
    if (e.key === 'k') activeCluster.value = Math.max(0, activeCluster.value - 1)
    if (e.key === 'Enter' || e.key === 'y') confirmCluster()
  }
}

onMounted(async () => {
  cats.value = await http.get('/categories/flat')
  types.value = await http.get('/documents/types')
  templates.value = await http.get('/rules/templates/rename')
  await loadQueue()
  if (activeId.value) await loadDetail(activeId.value)
  await loadSuggestions()
  window.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
onActivated(loadQueue)
</script>

<template>
  <div class="review">
    <div class="toolbar" style="margin-bottom: 10px">
      <el-radio-group v-model="mode" size="small">
        <el-radio-button value="single">逐件审核（{{ queue.length }}）</el-radio-button>
        <el-radio-button value="cluster">同模板批次（{{ clusters.length }}）</el-radio-button>
      </el-radio-group>
      <span class="muted hint">机器把没把握的交给你；确认过的可存为样例，下次自动归档</span>
      <div class="grow" />
      <el-button size="small" @click="loadQueue">刷新</el-button>
    </div>

    <el-empty v-if="mode === 'single' && !queue.length" description="审核队列已清空 🎉" />

    <div v-else-if="mode === 'single'" class="split review-split" :class="{ compact: isCompact() }">
      <el-card class="queue-card" body-class="queue-body">
        <template #header>待审队列（不确定度优先）</template>
        <div v-for="d in queue" :key="d.id" class="q-item" :class="{ on: d.id === activeId }" @click="activeId = d.id">
          <div class="q-line">
            <b>{{ d.filename }}</b>
            <span class="mono conf" :class="{ low: (d.confidence ?? 0) < 0.6 }">{{ d.confidence != null ? Math.round(d.confidence * 100) + '%' : '—' }}</span>
          </div>
          <div class="q-sub muted">{{ d.document_type || '未知类型' }} · {{ why(d) }}</div>
        </div>
        <div class="kbd-tip muted hint">J/K 上下 · A 归档 · S 跳过 · Enter 归档</div>
      </el-card>

      <div class="pv-wrap">
        <DocPreview v-if="activeId" :doc-id="activeId" :pane="true" height="100%" />
        <el-empty v-else description="左侧选择一件开始审核" />
      </div>

      <el-card class="decide" body-class="decide-body">
        <template #header>判定</template>
        <template v-if="activeDoc">
          <el-form label-position="top" size="small">
            <el-form-item label="归档分类">
              <el-select v-model="pick.category_id" placeholder="沿用机器建议" clearable style="width: 100%">
                <el-option v-for="c in cats" :key="c.id" :label="c.path" :value="c.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="文档类型">
              <el-select v-model="pick.document_type" filterable allow-create default-first-option clearable style="width: 100%">
                <el-option v-for="t in types" :key="t" :label="t" :value="t" />
              </el-select>
            </el-form-item>
            <el-form-item label="命名模板">
              <el-select v-model="pick.template_id" placeholder="按类型默认模板" clearable style="width: 100%">
                <el-option v-for="t in templates" :key="t.id" :label="`${t.name} ${t.pattern}`" :value="t.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="指定文件名（可选，不含扩展名）">
              <el-input v-model="pick.filename" placeholder="留空=按模板自动生成" />
            </el-form-item>
          </el-form>

          <div class="conf-box">
            <div>置信度 <b class="mono">{{ activeDoc.confidence != null ? Math.round(activeDoc.confidence * 100) + '%' : '—' }}</b></div>
            <div class="muted hint">{{ why(activeDoc) }}</div>
            <el-progress v-if="activeDoc.confidence != null" :percentage="Math.round(activeDoc.confidence * 100)" :stroke-width="8"
                         :color="activeDoc.confidence >= 0.85 ? '#3eb75a' : activeDoc.confidence >= 0.6 ? '#e6a23c' : '#f56c6c'" />
          </div>

          <el-collapse v-model="panel">
            <el-collapse-item title="识别字段（可直接修正）" name="fields">
              <div v-for="f in detail?.fields || []" :key="f.name" class="field-row">
                <span class="muted">{{ f.name }}</span>
                <el-input v-model="f.value" size="small" @change="saveField(f)" />
                <el-tag size="small" effect="plain">{{ f.source }}</el-tag>
              </div>
              <div v-if="!(detail?.fields || []).length" class="muted hint">没有识别到字段</div>
            </el-collapse-item>
          </el-collapse>

          <div class="ops">
            <el-checkbox v-model="saveSample">存为样例（下次同类自动归档）</el-checkbox>
            <div class="op-btns">
              <el-button type="success" :loading="busy" @click="approve">归档 (A)</el-button>
              <el-button @click="skip">跳过 (S)</el-button>
            </div>
            <div class="op-btns">
              <el-button plain @click="approveSameType">同组全部归档</el-button>
              <el-button plain type="danger" @click="recycle">回收站</el-button>
            </div>
          </div>
        </template>
        <el-empty v-else description="队列为空" :image-size="70" />
      </el-card>
    </div>

    <div v-else class="split review-split" :class="{ compact: isCompact() }">
      <el-card class="queue-card" body-class="queue-body">
        <template #header>批次（同模板）</template>
        <div v-for="(g, i) in clusters" :key="g.fingerprint" class="q-item" :class="{ on: i === activeCluster }"
             @click="activeCluster = i">
          <div class="q-line"><b>{{ g.base_filename }}</b><el-tag size="small">× {{ g.count }}</el-tag></div>
          <div class="q-sub muted">{{ g.document_type || '未知类型' }} · 最低置信 {{ Math.round(g.min_confidence * 100) }}%</div>
        </div>
        <div class="kbd-tip muted hint">J/K 切换 · Enter 确认整组</div>
      </el-card>

      <div class="pv-wrap">
        <DocPreview v-if="activeGroup" :doc-id="activeGroup.base_id" :pane="true" height="100%" />
        <el-empty v-else description="左侧选择一个批次" />
      </div>

      <el-card class="decide" body-class="decide-body">
        <template #header>
          批次 {{ activeCluster + 1 }}/{{ clusters.length }} · {{ activeGroup?.count }} 件
        </template>
        <template v-if="activeGroup">
          <el-descriptions :column="1" size="small" border>
            <el-descriptions-item v-for="(v, k) in activeGroup.fields" :key="k" :label="String(k)">{{ v }}</el-descriptions-item>
          </el-descriptions>
          <div class="muted hint" style="margin-top: 8px">
            整组按同一模板命名（第 1、2… 件自动编号），确认前可先看原件核对。
          </div>
          <div class="ops">
            <div class="op-btns">
              <el-button type="success" :loading="busy" @click="confirmCluster">确认整组归档</el-button>
              <el-button @click="mode = 'single'">逐件审核</el-button>
            </div>
          </div>
          <h4>学习建议（来自你的纠错）</h4>
          <div v-if="!suggestions.length" class="muted hint">暂无足够纠错样本</div>
          <div v-for="s in suggestions" :key="s.keyword + String(s.category_id)" class="sugg">
            <div>「{{ s.keyword }}」→ {{ s.category_path }}（{{ s.count }} 次）</div>
            <el-button size="small" @click="adopt(s)">候选规则</el-button>
          </div>
        </template>
        <el-empty v-else description="没有待审批次" :image-size="70" />
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.review-split { grid-template-columns: 260px minmax(0, 1fr) 330px; height: calc(100vh - 150px); }
.review-split.compact { grid-template-columns: minmax(0, 1fr); height: auto; }
.queue-card { min-height: 0; display: flex; flex-direction: column; }
.queue-card :deep(.queue-body) { overflow: auto; padding: 6px; flex: 1 1 auto; }
.q-item { padding: 8px; border-radius: 8px; cursor: pointer; border: 1px solid transparent; }
.q-item:hover { background: var(--el-fill-color-light); }
.q-item.on { background: var(--el-color-primary-light-9); border-color: var(--el-color-primary-light-7); }
.q-line { display: flex; align-items: center; gap: 6px; }
.q-line b { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.conf { font-size: 12px; color: var(--el-color-success); }
.conf.low { color: var(--el-color-danger); }
.q-sub { font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pv-wrap { min-width: 0; min-height: 420px; }
.decide :deep(.decide-body) { padding: 10px 12px; overflow: auto; }
.conf-box { border: 1px dashed var(--df-line); border-radius: 8px; padding: 8px 10px; margin: 6px 0 10px;
  display: flex; flex-direction: column; gap: 6px; }
.field-row { display: grid; grid-template-columns: 84px minmax(0, 1fr) 58px; gap: 6px; align-items: center; margin-bottom: 6px; }
.ops { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
.op-btns { display: flex; gap: 8px; flex-wrap: wrap; }
.kbd-tip { margin-top: 8px; text-align: center; }
.sugg { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 0;
  border-bottom: 1px dashed var(--df-line); font-size: 13px; }
@media (max-width: 860px) {
  .review-split { grid-template-columns: minmax(0, 1fr); }
  .pv-wrap { min-height: 62vh; }
}
</style>