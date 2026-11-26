<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import http from '../api/client'
import DocThumb from '../components/DocThumb.vue'
import PreviewDialog from '../components/PreviewDialog.vue'
import { useResponsive } from '../composables/useResponsive'
import { fmtTime } from '../utils/format'

const router = useRouter()
const { isCompact } = useResponsive()

const ledgers = ref<any[]>([])
const runs = ref<any[]>([])
const source = ref('auto')
const diffs = ref<any[]>([])
const activeRun = ref<any>(null)
const onlyOpen = ref(true)
const typeFilter = ref('')
const busy = ref(false)
const ledgerUpload = ref<HTMLInputElement>()

const TYPE_LABEL: any = {
  missing_doc: '缺结算/发票', missing_required: '缺必备单据',
  missing_payment: '未回款/缺凭证', payment_shortfall: '收付金额不符',
  payment_mismatch: '凭证与台账不符',
  amount_mismatch: '金额不符', extra_doc: '文档无台账', date_suspect: '日期可疑',
  seal_missing: '疑似缺章',
}
const PAY_TYPES = ['missing_payment', 'payment_shortfall', 'payment_mismatch']
const SEV_TYPE: any = { error: 'danger', warn: 'warning', info: 'info' }

/** 差异标签：缺必备单据要说清楚缺的是哪一类，不能只写“缺必备单据”。 */
function diffLabel(d: any) {
  if (d.type === 'missing_required') return '缺' + ((d.detail && d.detail.required_role) || '必备单据')
  if (d.type === 'missing_payment') {
    const det = d.detail || {}
    return det.reason === 'ledger_paid' ? '台账已回款·缺凭证' : '未回款/缺凭证'
  }
  return TYPE_LABEL[d.type] || d.type
}

async function loadAll() {
  ledgers.value = await http.get('/reconciliation/ledgers')
  runs.value = await http.get('/reconciliation/runs')
}

async function uploadLedger(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (!f) return
  const fd = new FormData()
  fd.append('name', f.name.replace(/\.[^.]+$/, ''))
  fd.append('file', f)
  const r: any = await http.post('/reconciliation/ledgers', fd)
  ElMessage.success(`识别 ${r.rows} 条合同（结算 ${r.settlement_rows} / 回款 ${r.payment_rows}）`)
  await loadAll()
}

async function runRecon() {
  busy.value = true
  try {
    const r: any = await http.post('/reconciliation/runs', { ledger_id: null, source: source.value })
    const parts = Object.entries(r.counts || {}).map(([k, v]) => `${TYPE_LABEL[k] || k} ${v}`)
    const from = r.source === 'archive' ? '（档案库自对账）' : '（台账）'
    ElMessage.success(parts.length ? `对账完成${from}：${parts.join(' · ')}` : `对账完成${from}：无差异 ✓`)
    await loadAll()
    await selectRun(runs.value[0])
  } finally { busy.value = false }
}

async function selectRun(run: any) {
  if (!run) return
  activeRun.value = run
  const params: any = { only_open: onlyOpen.value }
  if (typeFilter.value) params.type = typeFilter.value
  diffs.value = await http.get(`/reconciliation/runs/${run.id}/diffs`, { params })
}

async function resolve(d: any) {
  const { value } = await ElMessageBox.prompt('处理备注（将跨期结转）', '标记已处理', { inputValue: '' })
  await http.post(`/reconciliation/diffs/${d.id}/resolve`, { note: value || '' })
  await selectRun(activeRun.value)
}

function exportRun(fmt: string) {
  window.open(`/api/reconciliation/runs/${activeRun.value.id}/export?fmt=${fmt}`, '_blank')
}

const ledgerView = ref<any>(null)
const ledgerOpen = ref(false)

async function previewLedger(l: any) {
  ledgerView.value = await http.get(`/preview/ledger/${l.id}`)
  ledgerOpen.value = true
}

const ledgerRows = computed(() => {
  const cols: string[] = ledgerView.value?.columns || []
  return (ledgerView.value?.rows || []).map((r: any[]) => {
    const o: Record<string, string> = {}
    cols.forEach((c, i) => { o[c] = r[i] ?? '' })
    return o
  })
})

async function removeRun(run: any) {
  const n = Object.values(run.counts || {}).reduce((a: number, b) => a + Number(b), 0)
  await ElMessageBox.confirm(`删除第 ${run.id} 期对账记录及其 ${n} 条差异？`, '删除对账期', { type: 'warning' })
  await http.delete(`/reconciliation/runs/${run.id}`)
  if (activeRun.value?.id === run.id) { activeRun.value = null; diffs.value = [] }
  ElMessage.success('已删除')
  await loadAll()
}

async function removeLedger(l: any) {
  await ElMessageBox.confirm(`删除台账「${l.name}」及其解析数据？`, '删除')
  await http.delete(`/reconciliation/ledgers/${l.id}`)
  await loadAll()
}

// ---------- 依据抽屉：差异 → 业务包 → 缺什么 → 就地上传补齐 ----------
const evOpen = ref(false)
const ev = ref<any>(null)
const evDiff = ref<any>(null)
const evLoading = ref(false)
const evFileInput = ref<HTMLInputElement>()
const previewOpen = ref(false)
const previewId = ref<number | null>(null)

async function openEvidence(d: any) {
  evDiff.value = d
  evOpen.value = true
  evLoading.value = true
  try {
    ev.value = await http.get(`/reconciliation/diffs/${d.id}/evidence`)
  } finally { evLoading.value = false }
}

function evUpload(role: string) {
  evRole.value = role
  evFileInput.value?.click()
}

const evRole = ref('')

async function waitJob(jobId: number) {
  for (let i = 0; i < 400; i++) {
    const j: any = await http.get(`/processing/jobs/${jobId}`)
    if (j.status !== 'running') return
    await new Promise((r) => setTimeout(r, 250))
  }
}

async function evUploadFiles(e: Event) {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (!files.length || !evDiff.value) return
  const fd = new FormData()
  files.forEach((f) => fd.append('files', f))
  evLoading.value = true
  try {
    const up: any = await http.post(`/reconciliation/diffs/${evDiff.value.id}/upload-doc`, fd)
    if (!up.job_id) {
      ElMessage.warning((up.rejected || []).map((x: any) => `${x.filename}：${x.reason}`).join('；') || '没有可入库的文件')
      return
    }
    await waitJob(up.job_id)
    const out: any = await http.post(`/reconciliation/diffs/${evDiff.value.id}/recheck`)
    ev.value = out.evidence
    if (out.evidence?.diff) evDiff.value = out.evidence.diff
    if (out.resolved) ElMessage.success(out.note || '差异已关闭')
    else if ((out.remaining || []).length) ElMessage.warning('仍缺 ' + out.remaining.join('、'))
    else ElMessage.info('条件还没满足，检查一下上传的文件是否识别正确')
    await selectRun(activeRun.value)
  } finally { evLoading.value = false }
}

async function recheck(d: any) {
  evLoading.value = true
  try {
    const out: any = await http.post(`/reconciliation/diffs/${d.id}/recheck`)
    ev.value = out.evidence
    if (out.evidence?.diff) evDiff.value = out.evidence.diff
    if (out.resolved) ElMessage.success(out.note || '差异已关闭')
    else ElMessage.warning((out.remaining || []).length ? '仍缺 ' + out.remaining.join('、') : '条件还没满足')
    await selectRun(activeRun.value)
  } finally { evLoading.value = false }
}

const evDocs = computed(() => (ev.value && ev.value.docs) || [])
const money = (v: number | null | undefined) =>
  v === null || v === undefined
    ? '—'
    : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function goBundles() {
  evOpen.value = false
  router.push({ path: '/bundles', query: evDiff.value?.contract_no ? { cno: evDiff.value.contract_no } : {} })
}

onMounted(loadAll)
onActivated(loadAll)
</script>

<template>
  <div>
    <el-row :gutter="12">
      <el-col :span="7">
        <el-card class="page-card" header="业务台账">
          <el-button @click="ledgerUpload?.click()">上传台账 Excel</el-button>
          <input ref="ledgerUpload" type="file" accept=".xlsx,.xlsm" hidden @change="uploadLedger" />
          <el-table :data="ledgers" size="small" style="margin-top: 10px">
            <el-table-column prop="name" label="名称" show-overflow-tooltip />
            <el-table-column prop="rows" label="合同行" width="70" class-name="col-md" label-class-name="col-md" />
            <el-table-column label="操作" width="104">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click="previewLedger(row)">原表</el-button>
                <el-button size="small" text type="danger" @click="removeLedger(row)">删</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div style="font-size: 12px; color: var(--el-text-color-secondary); margin-top: 6px">
            表头支持常见别名：合同编号/客户名称/合同金额/签订日期/业务类别；结算、回款各一张工作表自动识别。
          </div>
        </el-card>
        <el-card class="page-card" header="对账">
          <div class="toolbar" style="margin-bottom: 8px">
            <el-select v-model="source" size="small" style="width: 172px">
              <el-option label="自动（有台账用台账）" value="auto" />
              <el-option label="业务台账" value="ledger" />
              <el-option label="档案库自对账" value="archive" />
            </el-select>
            <el-button type="primary" :loading="busy" :disabled="source === 'ledger' && !ledgers.length" @click="runRecon">运行对账</el-button>
          </div>
          <el-table :data="runs" size="small" style="margin-top: 10px" highlight-current-row @row-click="selectRun">
            <el-table-column prop="id" label="期" width="44" />
            <el-table-column label="差异" min-width="150">
              <template #default="{ row }">
                <div class="run-tags">
                  <el-tag v-for="(v, k) in row.counts" :key="String(k)" size="small"
                       :type="String(k).indexOf('missing') === 0 ? 'danger' : 'info'">
                    {{ TYPE_LABEL[k] || k }} {{ v }}
                  </el-tag>
                  <span v-if="!Object.keys(row.counts || {}).length">无差异 ✓</span>
                </div>
                <div class="muted hint run-time">
                  {{ row.source === 'archive' ? '档案库自对账' : '台账' }} · {{ fmtTime(row.finished_at).slice(5) }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="58">
              <template #default="{ row }">
                <el-button size="small" text type="danger" @click.stop="removeRun(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="17">
        <el-card :header="activeRun ? `第 ${activeRun.id} 期差异明细` : '差异明细'">
          <div style="display: flex; gap: 10px; margin-bottom: 10px">
            <el-checkbox v-model="onlyOpen" @change="selectRun(activeRun)">只看未处理</el-checkbox>
            <el-select v-model="typeFilter" placeholder="类型" clearable style="width: 150px" @change="selectRun(activeRun)">
              <el-option v-for="(v, k) in TYPE_LABEL" :key="k" :label="v" :value="k" />
            </el-select>
            <div style="flex: 1"></div>
            <el-button :disabled="!activeRun" size="small" @click="exportRun('csv')">导出 CSV</el-button>
            <el-button :disabled="!activeRun" size="small" @click="exportRun('xlsx')">导出 Excel</el-button>
          </div>
          <el-table :data="diffs" size="small" max-height="560">
            <el-table-column label="类型" width="138">
              <template #default="{ row }"><el-tag size="small" :type="SEV_TYPE[row.severity]">{{ diffLabel(row) }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="contract_no" label="合同编号" width="150" />
            <el-table-column prop="customer" label="客户" width="150" show-overflow-tooltip />
            <el-table-column prop="expected" label="期望" width="120" />
            <el-table-column prop="actual" label="实际" show-overflow-tooltip />
            <el-table-column label="依据" width="74" align="center">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click="openEvidence(row)">依据</el-button>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="130">
              <template #default="{ row }">
                <el-tag v-if="row.resolved" size="small" type="success">已处理 {{ row.resolution_note }}</el-tag>
                <el-button v-else size="small" @click="resolve(row)">标记处理</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <input ref="evFileInput" type="file" multiple hidden @change="evUploadFiles" />

    <el-drawer v-model="evOpen" :size="isCompact() ? '100%' : '46%'" :title="'差异依据 · ' + (evDiff ? diffLabel(evDiff) : '')">
      <div v-if="evLoading && !ev" class="hint muted">正在取依据…</div>
      <template v-else-if="ev">
        <el-alert v-if="evDiff && evDiff.resolved" type="success" :closable="false" show-icon
                  :title="'已关闭：' + (evDiff.resolution_note || '')" style="margin-bottom: 12px" />
        <el-alert v-else-if="ev.still_missing.length" type="warning" :closable="false" show-icon
                  :title="'复核后仍缺：' + ev.still_missing.join('、')" style="margin-bottom: 12px" />

        <div class="sec-head">
          <div class="sec-title">业务包</div>
          <div class="sec-link" @click="goBundles">在文档归集里打开 →</div>
        </div>
        <div v-if="ev.bundle" class="ev-card">
          <div class="ev-name">{{ ev.bundle.name }}</div>
          <div class="ev-row"><i>合同号</i><span class="mono">{{ ev.bundle.contract_nos.join(' · ') || '—' }}</span></div>
          <div class="ev-row"><i>客户</i>{{ ev.bundle.customer || '—' }}</div>
          <div class="ev-row"><i>合同金额</i>{{ money(ev.bundle.amount) }}</div>
          <div class="ev-row"><i>齐备度</i>
            <el-tag size="small" :type="ev.bundle.complete ? 'success' : 'danger'">
              {{ ev.bundle.completeness.have }}/{{ ev.bundle.completeness.need }}
            </el-tag>
          </div>
          <template v-if="ev.payment">
            <div class="ev-row"><i>{{ ev.payment.due_word }}</i>{{ money(ev.payment.due) }}</div>
            <div class="ev-row"><i>{{ ev.payment.paid_word }}</i>{{ money(ev.payment.effective) }}
              <span v-if="ev.payment.paid_ledger" class="muted">
                （台账 {{ money(ev.payment.paid_ledger) }}·凭证 {{ money(ev.payment.paid) }}）</span>
            </div>
            <div class="ev-row"><i>{{ ev.payment.short_word }}</i>
              <span :class="Number(ev.payment.outstanding) > 0 ? 'gap-bad' : 'gap-ok'">
                {{ money(ev.payment.outstanding) }}</span>
              <span v-if="Number(ev.payment.uncleared) > 0" class="muted">
                （凭证还差 {{ money(ev.payment.uncleared) }}）</span>
            </div>
          </template>
        </div>
        <div v-else class="hint muted">这条差异还没有对应的业务包（档案库里找不到该合同的任何单据），先把合同归档即可自动归集。</div>

        <div class="sec-head" style="margin-top: 18px">
          <div class="sec-title">已匹配文档</div>
          <div class="sec-link">{{ ev.docs.length }} 份</div>
        </div>
        <div v-if="ev.docs.length" class="ev-docs">
          <div v-for="d in ev.docs" :key="d.item_id" class="ev-doc" @click="previewId = d.document_id; previewOpen = true">
            <DocThumb :doc-id="d.document_id" :file-type="d.file_type" :exists="d.file_exists" :size="62" />
            <div class="ev-doc-main">
              <div class="ev-doc-name">{{ d.filename }}</div>
              <div class="hint muted">
                <el-tag size="small" effect="plain">{{ d.role }}</el-tag>
                {{ money(d.amount) }} · {{ d.doc_date || '—' }}
                <span v-if="!d.file_exists" class="gap-bad">· 文件缺失</span>
              </div>
            </div>
          </div>
        </div>
        <div v-else class="hint muted">暂无已归档的依据文档。</div>

        <div class="sec-head" style="margin-top: 18px">
          <div class="sec-title">缺什么</div>
          <div class="sec-link">上传后自动重算并关闭本差异</div>
        </div>
        <div v-if="ev.missing.length" class="ev-miss">
          <div v-for="m in ev.missing" :key="m.role" class="ev-miss-row">
            <div class="grow">
              <div class="ev-miss-role">缺{{ m.label || m.role }}</div>
              <div class="hint muted">
                已归档 {{ m.present }}/{{ m.min_count }} 份
                <span v-if="m.expected"> · {{ (ev.payment && ev.payment.due_word) || '应到' }}
                  {{ money(m.expected) }}，已 {{ money(m.actual) }}</span>
                <span v-else-if="m.amount_check"> · 需与合同金额一致</span>
              </div>
            </div>
            <el-button size="small" type="primary" plain :disabled="evLoading"
                       :loading="evLoading" @click="evUpload(m.role)">上传{{ m.role }}</el-button>
          </div>
        </div>
        <div v-else class="hint muted">必备单据都齐了；若差异仍未关闭，点下面「重新复核」。</div>

        <div class="toolbar" style="margin-top: 18px">
          <el-button size="small" :disabled="evLoading" @click="evDiff && recheck(evDiff)">重新复核</el-button>
          <el-button v-if="evDiff && !evDiff.resolved" size="small" text @click="evDiff && resolve(evDiff)">手动标记已处理</el-button>
        </div>
      </template>
    </el-drawer>

    <PreviewDialog v-model="previewOpen" v-model:doc-id="previewId" :ids="evDocs.map((d: any) => d.document_id)"
                   :title="(evDocs.find((d: any) => d.document_id === previewId) || {}).filename || ''" />

    <el-dialog v-model="ledgerOpen" :title="`台账原表：${ledgerView?.name || ''}`" width="80%" top="6vh">
      <div v-if="ledgerView">
        <div class="muted hint" style="margin-bottom: 8px">
          工作表：{{ (ledgerView.sheets || []).join(' · ') || '—' }}
          <span v-if="ledgerView.truncated"> · 仅显示前 {{ ledgerRows.length }} 行</span>
        </div>
        <el-table :data="ledgerRows" size="small" border max-height="60vh">
          <el-table-column v-for="c in ledgerView.columns || []" :key="c" :prop="c" :label="c" min-width="130" show-overflow-tooltip />
        </el-table>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.run-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.ev-card { background: var(--df-surface-2); border: 1px solid var(--df-line); border-radius: 16px;
  padding: 12px 14px; }
.ev-name { font-size: 14px; font-weight: 700; margin-bottom: 8px; word-break: break-all; }
.ev-row { display: flex; gap: 8px; font-size: 12.5px; line-height: 1.9; min-width: 0; }
.ev-row i { font-style: normal; color: var(--el-text-color-secondary); flex: 0 0 68px; }
.ev-row span, .ev-row { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ev-docs { display: flex; flex-direction: column; gap: 8px; }
.ev-doc { display: flex; gap: 10px; align-items: center; padding: 8px; border-radius: 14px;
  border: 1px solid var(--df-line); background: var(--df-surface); cursor: pointer; transition: box-shadow .14s; }
.ev-doc:hover { box-shadow: var(--df-shadow-sm); }
.ev-doc-main { min-width: 0; flex: 1 1 auto; }
.ev-doc-name { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ev-miss { display: flex; flex-direction: column; gap: 8px; }
.ev-miss-row { display: flex; gap: 10px; align-items: center; padding: 10px 12px; border-radius: 14px;
  border: 1px dashed var(--el-color-danger-light-5); background: var(--df-surface-2); }
.ev-miss-role { font-size: 13px; font-weight: 700; color: var(--el-color-danger); }
.gap-bad { color: var(--el-color-danger); }
.run-time { font-size: 11.5px; margin-top: 2px; }
</style>
