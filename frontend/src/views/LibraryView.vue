<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import http, { desktop } from '../api/client'
import { fmtTime } from '../utils/format'
import { opText } from '../utils/labels'
import DocThumb from '../components/DocThumb.vue'
import PreviewDialog from '../components/PreviewDialog.vue'
import { useResponsive } from '../composables/useResponsive'

const { isCompact } = useResponsive()
const rows = ref<any[]>([])
const total = ref(0)
const recycledHidden = ref(0)
const snippets = ref<Record<string, string>>({})
const q = ref('')
const filters = ref<any>({ status: [], document_type: '', file_type: '', dateRange: null })
const types = ref<string[]>([])
const fileTypes = ['pdf', 'image', 'docx', 'doc', 'xlsx', 'xls', 'csv', 'txt']
const cats = ref<any[]>([])
const filterOpen = ref(false)
const page = ref(1)
const size = ref(50)
const loading = ref(false)
const view = ref<'table' | 'cards'>(localStorage.getItem('docflow_lib_view') as any || 'table')
const selected = ref<any[]>([])

const previewOpen = ref(false)
const previewId = ref<number | null>(null)
const ids = computed(() => rows.value.map((r) => r.id))

const drawer = ref(false)
const detail = ref<any>(null)

const STATUS_LABEL: Record<string, [string, string]> = {
  pending: ['待整理', 'info'], processing: ['识别中', 'primary'], archived: ['已归档', 'success'],
  need_review: ['待审核', 'warning'], failed: ['失败', 'danger'], duplicate: ['重复', 'info'],
  recycled: ['回收站', 'info'],
}
const activeFilterCount = computed(() =>
  [filters.value.status?.length ? 1 : 0, filters.value.document_type ? 1 : 0,
   filters.value.file_type ? 1 : 0, filters.value.dateRange?.length === 2 ? 1 : 0]
    .reduce((a, b) => a + b, 0))

const route = useRoute()
if (route.query.q) q.value = String(route.query.q)
if (route.query.status) filters.value.status = String(route.query.status).split(',')

async function load() {
  loading.value = true
  try {
    const params: any = { q: q.value, limit: size.value, offset: (page.value - 1) * size.value }
    if (filters.value.status?.length) params.status = filters.value.status.join(',')
    if (filters.value.document_type) params.document_type = filters.value.document_type
    if (filters.value.file_type) params.file_type = filters.value.file_type
    if (filters.value.dateRange?.length === 2) {
      params.date_from = filters.value.dateRange[0]
      params.date_to = filters.value.dateRange[1]
    }
    const r: any = await http.get('/documents', { params })
    rows.value = r.items
    total.value = r.total
    snippets.value = r.snippets || {}
    recycledHidden.value = r.recycled_hidden || 0
  } finally {
    loading.value = false
  }
}

function reload() { page.value = 1; load() }

watch(view, (v) => localStorage.setItem('docflow_lib_view', v))

function openPreview(row: any) {
  previewId.value = row.id
  previewOpen.value = true
}

async function open(row: any) {
  detail.value = await http.get(`/documents/${row.id}`)
  drawer.value = true
}

async function renameRow(row: any) {
  const { value } = await ElMessageBox.prompt('新文件名（不含扩展名）', '重命名', {
    inputValue: row.current_filename.replace(/\.[^.]+$/, ''),
  })
  if (!value) return
  await http.post(`/documents/${row.id}/rename`, { filename: value })
  ElMessage.success('已重命名')
  load()
}

async function reprocess(row: any) {
  await http.post(`/documents/${row.id}/reprocess`)
  ElMessage.success('已重新排队识别')
}

async function del(row: any) {
  const missing = row.file_exists === false
  await ElMessageBox.confirm(
    missing
      ? `「${row.current_filename}」的文件已不在磁盘上，把这条失效记录移入回收站？`
      : `把「${row.current_filename}」移入回收站？文件搬进回收站，文档库不再显示它。`,
    '移入回收站', { type: 'warning', confirmButtonText: '移入回收站' })
  const r: any = await http.delete(`/documents/${row.id}`)
  ElMessage.success(r?.file_missing
    ? '文件本就缺失，失效记录已移入回收站，文档库不再显示'
    : '已移入回收站，文档库不再显示；在回收站可恢复或彻底删除')
  load()
}

function showRecycled() {
  filters.value.status = ['recycled']
  reload()
}

async function batchExport() {
  const ids2 = selected.value.map((s) => s.id)
  const blob: any = await http.post('/documents/export-zip', { ids: ids2, zip_name: '导出文档' },
    { responseType: 'blob' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = '导出文档.zip'
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success(`已打包 ${ids2.length} 个文件`)
}

async function batchUndo(row?: any) {
  const targets = row ? [row] : selected.value
  await ElMessageBox.confirm(`撤销 ${targets.length} 个文档的整理结果（文件回到原位）？`, '撤销')
  const r: any = await http.post('/documents/batch-undo', { ids: targets.map((t) => t.id) })
  if (r.errors?.length) ElMessage.warning(`${r.done.length} 成功，${r.errors.length} 个失败`)
  else ElMessage.success('已撤销')
  load()
}

async function savePatch() {
  const d = detail.value
  const fields: Record<string, string> = {}
  d.fields.forEach((f: any) => { fields[f.name] = f.value })
  await http.patch(`/documents/${d.id}`, {
    document_type: d.document_type, title: d.title,
    suggested_category_id: d.suggested_category_id, fields,
  })
  ElMessage.success('已保存')
  drawer.value = false
  load()
}

async function revealDetail() {
  if (detail.value) await desktop.revealDocument(detail.value.id)
}

function onKey(e: KeyboardEvent) {
  const t = e.target as HTMLElement
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(t?.tagName)) return
  if (e.key === '/') {
    e.preventDefault()
    nextTick(() => document.querySelector<HTMLInputElement>('.lib-search input')?.focus())
  } else if (e.key === 'r' && !e.ctrlKey && !e.metaKey) reload()
}

onMounted(() => {
  http.get('/documents/types').then((v: any) => (types.value = v))
  http.get('/categories/flat').then((v: any) => (cats.value = v))
  load()
  window.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
onActivated(() => load())
</script>

<template>
  <div class="lib">
    <el-card class="page-card">
      <div class="toolbar">
        <el-input v-model="q" class="lib-search" placeholder="全文 / 文件名 / 字段搜索（按 / 聚焦）"
                  clearable style="max-width: 380px" @keyup.enter="reload" @clear="reload">
          <template #append><el-button @click="reload">搜索</el-button></template>
        </el-input>

        <template v-if="!isCompact()">
          <el-select v-model="filters.status" multiple collapse-tags collapse-tags-tooltip placeholder="状态"
                     style="width: 168px" @change="reload">
            <el-option v-for="(v, k) in STATUS_LABEL" :key="k" :label="v[0]" :value="k" />
          </el-select>
          <el-select v-model="filters.document_type" placeholder="类型" clearable style="width: 132px" @change="reload">
            <el-option v-for="t in types" :key="t" :label="t" :value="t" />
          </el-select>
          <el-select v-model="filters.file_type" placeholder="格式" clearable style="width: 110px" @change="reload">
            <el-option v-for="t in fileTypes" :key="t" :label="t" :value="t" />
          </el-select>
          <el-date-picker v-model="filters.dateRange" type="daterange" value-format="YYYY-MM-DD"
                          start-placeholder="开始" end-placeholder="结束" style="width: 230px" @change="reload" />
        </template>
        <el-popover v-else v-model:visible="filterOpen" placement="bottom-start" :width="300" trigger="click">
          <template #reference>
            <el-button>筛选<el-badge v-if="activeFilterCount" :value="activeFilterCount" class="fb" /></el-button>
          </template>
          <div class="filter-pop">
            <el-select v-model="filters.status" multiple collapse-tags placeholder="状态" @change="reload">
              <el-option v-for="(v, k) in STATUS_LABEL" :key="k" :label="v[0]" :value="k" />
            </el-select>
            <el-select v-model="filters.document_type" placeholder="类型" clearable @change="reload">
              <el-option v-for="t in types" :key="t" :label="t" :value="t" />
            </el-select>
            <el-select v-model="filters.file_type" placeholder="格式" clearable @change="reload">
              <el-option v-for="t in fileTypes" :key="t" :label="t" :value="t" />
            </el-select>
            <el-date-picker v-model="filters.dateRange" type="daterange" value-format="YYYY-MM-DD"
                            start-placeholder="开始" end-placeholder="结束" @change="reload" />
          </div>
        </el-popover>

        <div class="grow" />
        <el-radio-group v-model="view" size="small">
          <el-radio-button value="table">表格</el-radio-button>
          <el-radio-button value="cards">卡片</el-radio-button>
        </el-radio-group>
        <el-button size="small" :disabled="!selected.length" @click="batchExport">打包下载</el-button>
        <el-button size="small" :disabled="!selected.length" @click="batchUndo()">批量撤销</el-button>
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </el-card>

    <el-alert v-if="recycledHidden" class="bin-note" type="info" :closable="false" show-icon>
      <template #title>
        另有 {{ recycledHidden }} 份文档已移入回收站，不在文档库显示
        <el-button link type="primary" size="small" @click="$router.push('/recycle')">去回收站</el-button>
        <el-button link type="primary" size="small" @click="showRecycled">在这里显示</el-button>
      </template>
      <template #default>
        <span class="hint">恢复后回到文档库原位；到期由后台自动清理，也可在回收站立刻彻底删除（保留期在「设置 → 目录与归档」调整）。</span>
      </template>
    </el-alert>

    <el-card v-loading="loading">
      <el-table v-if="view === 'table'" :data="rows" size="small" @row-dblclick="open"
                @selection-change="(v: any) => (selected = v)">
        <el-table-column type="selection" width="40" />
        <el-table-column label="文件" min-width="300">
          <template #default="{ row }">
            <div class="fname-row" @click="openPreview(row)">
              <DocThumb :doc-id="row.id" :file-type="row.file_type" :size="34"
                        :exists="row.file_exists !== false" class="thumb-xs" />
              <div class="fname-cell">
                <div class="fname-line">
                  <a class="fname">{{ row.current_filename }}</a>
                  <el-tag v-if="row.file_exists === false" size="small" type="warning" class="miss-tag"
                          title="磁盘上已找不到该文件，预览/打开会失败">文件缺失</el-tag>
                </div>
                <div v-if="snippets[row.id]" class="snippet">{{ snippets[row.id] }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="document_type" label="类型" width="110" class-name="col-lg" label-class-name="col-lg" />
        <el-table-column label="置信度" width="120">
          <template #default="{ row }">
            <el-progress v-if="row.confidence != null" :percentage="Math.round(row.confidence * 100)" :stroke-width="10"
                         :color="row.confidence >= 0.85 ? '#3eb75a' : row.confidence >= 0.6 ? '#e6a23c' : '#9aa3b2'" />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="92">
          <template #default="{ row }">
            <el-tag size="small" :type="STATUS_LABEL[row.status]?.[1] as any">{{ STATUS_LABEL[row.status]?.[0] || row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="盖章" width="60" class-name="col-xl" label-class-name="col-xl">
          <template #default="{ row }">{{ row.has_seal === true ? '✓' : row.has_seal === false ? '—' : '' }}</template>
        </el-table-column>
        <el-table-column label="入库" width="132" class-name="col-xxl" label-class-name="col-xxl">
          <template #default="{ row }"><span class="mono muted">{{ fmtTime(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openPreview(row)">预览</el-button>
            <el-button size="small" text @click="open(row)">详情</el-button>
            <el-dropdown size="small" trigger="click">
              <el-button size="small" text>更多</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item @click="renameRow(row)">重命名</el-dropdown-item>
                  <el-dropdown-item @click="reprocess(row)">重新识别</el-dropdown-item>
                  <el-dropdown-item @click="batchUndo(row)">撤销整理</el-dropdown-item>
                  <el-dropdown-item divided @click="del(row)">移入回收站</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-table-column>
      </el-table>

      <div v-else class="doc-grid">
        <div v-for="row in rows" :key="row.id" class="doc-card" @click="openPreview(row)">
          <DocThumb :doc-id="row.id" :file-type="row.file_type" :size="132"
                    :exists="row.file_exists !== false" />
          <div class="doc-name">{{ row.current_filename }}</div>
          <div class="doc-meta">
            <el-tag size="small" :type="STATUS_LABEL[row.status]?.[1] as any">{{ STATUS_LABEL[row.status]?.[0] || row.status }}</el-tag>
            <span class="muted">{{ row.document_type || '—' }}</span>
            <el-tag v-if="row.file_exists === false" size="small" type="warning">文件缺失</el-tag>
            <span v-if="row.has_seal === true" title="检测到盖章">🔴</span>
            <div class="grow" />
            <span class="mono muted">{{ row.confidence != null ? Math.round(row.confidence * 100) + '%' : '' }}</span>
          </div>
          <div class="doc-ops" @click.stop>
            <el-button size="small" text @click="open(row)">详情</el-button>
            <el-button size="small" text type="danger" @click="del(row)">删除</el-button>
          </div>
        </div>
        <el-empty v-if="!rows.length && !loading" description="没有符合条件的文档" />
      </div>

      <el-pagination style="margin-top: 10px" layout="total, prev, pager, next, sizes" :total="total"
                     v-model:current-page="page" v-model:page-size="size"
                     :page-sizes="[25, 50, 100, 200]" @change="load" />
    </el-card>

    <PreviewDialog v-model="previewOpen" v-model:doc-id="previewId" :ids="ids"
                   :title="rows.find((r) => r.id === previewId)?.current_filename || ''" />

    <el-drawer v-model="drawer" :title="detail?.current_filename" :size="isCompact() ? '100%' : '46%'">
      <template v-if="detail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="类型"><el-input v-model="detail.document_type" size="small" /></el-descriptions-item>
          <el-descriptions-item label="标题"><el-input v-model="detail.title" size="small" /></el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="STATUS_LABEL[detail.status]?.[1] as any">{{ STATUS_LABEL[detail.status]?.[0] || detail.status }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="大小">{{ (detail.file_size / 1024).toFixed(1) }} KB</el-descriptions-item>
          <el-descriptions-item label="置信度">{{ detail.confidence }}</el-descriptions-item>
          <el-descriptions-item label="指纹"><span class="mono">{{ (detail.fingerprint || '').slice(0, 16) }}…</span></el-descriptions-item>
          <el-descriptions-item label="路径" :span="2"><span class="mono">{{ detail.path }}</span></el-descriptions-item>
        </el-descriptions>
        <h4>识别字段（可修正）</h4>
        <el-table :data="detail.fields" size="small">
          <el-table-column prop="name" label="字段" width="110" />
          <el-table-column label="值"><template #default="{ row }"><el-input v-model="row.value" size="small" /></template></el-table-column>
          <el-table-column prop="source" label="来源" width="70" class-name="col-md" label-class-name="col-md" />
        </el-table>
        <h4>正文片段</h4>
        <el-input :model-value="detail.text_preview" type="textarea" :rows="6" readonly />
        <h4>操作历史</h4>
        <el-timeline>
          <el-timeline-item v-for="op in detail.operations" :key="op.id" :timestamp="fmtTime(op.created_at, true)" size="small">
            {{ opText(op.op) }} {{ op.detail }} <el-tag v-if="op.undone" size="small">已撤销</el-tag>
          </el-timeline-item>
        </el-timeline>
        <div class="drawer-ops">
          <el-button type="primary" @click="savePatch">保存修正</el-button>
          <el-button @click="openPreview(detail)">预览原件</el-button>
          <el-button v-if="desktop.available" @click="revealDetail">资源管理器</el-button>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.fname-row { display: flex; align-items: center; gap: 10px; cursor: pointer; min-width: 0; }
.thumb-xs { flex: 0 0 34px; }
.fname-cell { min-width: 0; }
.fname-line { display: flex; align-items: center; gap: 6px; min-width: 0; }
.fname-line .fname { flex: 0 1 auto; min-width: 0; }
.miss-tag { flex: 0 0 auto; }
.fname { color: var(--el-color-primary); cursor: pointer; display: block; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.snippet { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.doc-card { border-radius: 18px; padding: 8px; background: var(--df-surface); box-shadow: var(--df-shadow-sm);
  display: flex; flex-direction: column; gap: 6px; cursor: pointer; transition: box-shadow .15s, transform .15s; }
.doc-card:hover { box-shadow: var(--df-shadow); transform: translateY(-3px); }
.doc-name { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.doc-meta { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.doc-ops { display: flex; justify-content: space-between; border-top: 1px dashed var(--df-line); padding-top: 4px; }
.filter-pop { display: flex; flex-direction: column; gap: 8px; }
.bin-note { margin-bottom: var(--df-gap); }
.bin-note :deep(.el-alert__title) { display: flex; align-items: center; gap: 2px; flex-wrap: wrap; }
.fb { margin-left: 6px; }
.drawer-ops { display: flex; gap: 8px; flex-wrap: wrap; margin: 14px 0 26px; }
</style>