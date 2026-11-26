<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import http from '../api/client'
import DocThumb from '../components/DocThumb.vue'
import PreviewDialog from '../components/PreviewDialog.vue'
import { useResponsive } from '../composables/useResponsive'
import { fmtTime } from '../utils/format'

/**
 * 文档归集：一份业务的采购/销售合同连同它的发票、结算单、提货单自动聚成一个业务包。
 * 左侧看齐备度，右侧按合同角色分块；缺哪类单据，那块就直接给上传入口。
 */
/** 从对账抽屉「在文档归集里打开」跳过来时，直接定位到那个业务包。 */
const route = useRoute()
const { isCompact } = useResponsive()
const items = ref<any[]>([])
const total = ref(0)
const q = ref('')
const state = ref('')
const page = ref(1)
const size = ref(50)
const cur = ref<any>(null)
const sugs = ref<any[]>([])
const sugOpen = ref(false)
const busy = ref(false)
const ignored = ref<Record<string, boolean>>({})

const previewOpen = ref(false)
const previewId = ref<number | null>(null)
const addOpen = ref(false)
const addPicked = ref<number[]>([])
const addCandidates = ref<any[]>([])
const addQuery = ref('')
const fileInput = ref<HTMLInputElement>()
const uploadRole = ref('')

const REQ_HINT: Record<string, string> = {
  发票: '影响开票、入账与认证',
  结算单: '无法核对结算金额',
  提货单: '货权转移没有依据',
  入库单: '采购收货没有依据',
  银行回单: '钱是否到账没有依据',
  收付款凭证: '钱是否到账没有依据',
}

const IGNORE_KEY = 'docflow-bundle-ignored'
const sugKey = (p: any) => String(p.sale_bundle_id) + '-' + String(p.purchase_bundle_id)
/** 忽略记录存 localStorage：全部忽略后必须还能一键恢复，否则建议永久消失。 */
const ignoredCount = computed(() => Object.keys(ignored.value).length)

async function load() {
  busy.value = true
  try {
    const r: any = await http.get('/bundles', {
      params: { q: q.value, state: state.value, limit: size.value, offset: (page.value - 1) * size.value },
    })
    items.value = r.items
    total.value = r.total
    const target = pickTarget(items.value)
    cur.value = target ? await http.get("/bundles/" + target.id) : null
    const s: any = await http.get('/bundles/suggestions')
    sugs.value = (s.items || []).filter((p: any) => !ignored.value[sugKey(p)])
  } finally { busy.value = false }
}

/** 选中哪一个包：URL 上的 ?cno= 优先，其次保持当前选中，最后取列表第一个。 */
function pickTarget(list: any[]): any | null {
  if (!list.length) return null
  const want = String(route.query.cno || '').trim().toUpperCase()
  if (want) {
    const hit = list.find((b: any) => (b.contract_nos || []).some((x: string) =>
      String(x).toUpperCase() === want))
    if (hit) return hit
  }
  return list.find((b: any) => cur.value && b.id === cur.value.id) || list[0]
}


async function open(b: any) {
  cur.value = await http.get('/bundles/' + b.id)
}

async function newBundle() {
  const { value } = await ElMessageBox.prompt('可留空，归档文档会自动带出名称', '新建业务包',
    { inputValue: '', inputPlaceholder: '如：XS-2026-0088 北方钢铁钢材销售' })
  await http.post('/bundles', { name: value || '', note: '' })
  ElMessage.success('已新建，用「加入文档」把单据关联进来')
  await load()
}

async function rebuild() {
  await ElMessageBox.confirm('按当前档案库重建全部自动业务包？手工建的包会保留。', '重建归集', { type: 'warning' })
  const r: any = await http.post('/bundles/rebuild')
  ElMessage.success('已重建 ' + r.bundles + ' 个业务包（入包 ' + r.attached + ' 份，' +
    r.without_contract_no + ' 份无合同编号跳过）')
  await load()
}

async function rename(b: any) {
  const { value } = await ElMessageBox.prompt('业务包名称', '重命名', { inputValue: b.name })
  if (value === null) return
  await http.patch('/bundles/' + b.id, { name: value })
  await load()
}

async function toggleSettled(b: any) {
  await http.patch('/bundles/' + b.id, { status: b.status === 'settled' ? 'open' : 'settled' })
  await load()
}

async function removeBundle(b: any) {
  await ElMessageBox.confirm('解散业务包「' + b.name + '」？只解除关联，文档本身不会删除。', '解散',
    { type: 'warning' })
  await http.delete('/bundles/' + b.id)
  cur.value = null
  ElMessage.success('已解散')
  await load()
}

async function pickDoc(d: any) {
  await ElMessageBox.confirm('把「' + d.filename + '」移出本业务包？', '移出', { type: 'warning' })
  await http.delete('/bundles/' + cur.value.id + '/items/' + d.document_id)
  await open(cur.value)
  await load()
}

async function editRole(d: any) {
  const { value } = await ElMessageBox.prompt('这份单据在本业务里充当什么角色', '改角色',
    { inputValue: d.role })
  if (!value || !value.trim()) return
  await http.patch('/bundles/items/' + d.item_id, { role: value.trim() })
  await open(cur.value)
}

async function openAdd() {
  addOpen.value = true
  addPicked.value = []
  addQuery.value = ''
  await searchCandidates('')
}

async function searchCandidates(kw: string) {
  const r: any = await http.get('/documents', { params: { q: kw, limit: 30 } })
  addCandidates.value = r.items || []
}

async function doAdd() {
  if (!addPicked.value.length) { ElMessage.warning('先勾选文档'); return }
  const r: any = await http.post('/bundles/' + cur.value.id + '/items', { document_ids: addPicked.value })
  addOpen.value = false
  ElMessage.success('已加入 ' + r.added.length + ' 份' + (r.skipped.length ? '，' + r.skipped.length + ' 份跳过' : ''))
  await load()
}

function uploadFor(role: string) {
  uploadRole.value = role
  fileInput.value?.click()
}

/** 缺件上传：走正常入库流水线，识别归档后按角色并入本包，再刷新齐备度。 */
async function onFiles(e: Event) {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (!files.length || !cur.value) return
  const fd = new FormData()
  files.forEach((f) => fd.append('files', f))
  const r: any = await http.post('/documents/upload', fd)
  if ((r.rejected || []).length) ElMessage.warning('已跳过 ' + r.rejected.length + ' 个不支持的文件')
  if (!r.job) { await load(); return }
  const ids = await waitJob(r.job.id)
  if (ids.length) {
    await http.post('/bundles/' + cur.value.id + '/items', { document_ids: ids, role: uploadRole.value })
    ElMessage.success('已把 ' + ids.length + ' 份' + uploadRole.value + '归入本业务包')
  } else {
    ElMessage.info('已入库，但未识别为' + uploadRole.value + '，可在审核通过后手动加入')
  }
  await load()
}

async function waitJob(jobId: number): Promise<number[]> {
  for (let i = 0; i < 400; i++) {
    const j: any = await http.get('/processing/jobs/' + jobId)
    if (j.status !== 'running') break
    await new Promise((r) => setTimeout(r, 250))
  }
  const docs: any = await http.get('/processing/jobs/' + jobId + '/documents')
  return (docs || []).filter((d: any) => d.status === 'archived').map((d: any) => d.id)
}

async function exportAll() {
  if (!docIds.value.length) { ElMessage.warning('包里还没有文档'); return }
  const name = zipName.value
  const blob: any = await http.post('/documents/export-zip', { ids: docIds.value, zip_name: name },
    { responseType: 'blob' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name + '.zip'
  a.click()
  URL.revokeObjectURL(url)
}

async function mergeInto(saleId: number, purchaseId: number) {
  const r: any = await http.post('/bundles/' + purchaseId + '/merge', { target_id: saleId, note: '由合并建议确认' })
  ElMessage.success('已并入，搬运 ' + r.moved + ' 条关联')
  await load()
}

function ignoreSug(p: any) {
  ignored.value[sugKey(p)] = true
  localStorage.setItem(IGNORE_KEY, JSON.stringify(Object.keys(ignored.value)))
  sugs.value = sugs.value.filter((x) => sugKey(x) !== sugKey(p))
}

function resetIgnored() {
  ignored.value = {}
  localStorage.removeItem(IGNORE_KEY)
  load()
}

const docIds = computed<number[]>(() => {
  const out: number[] = []
  for (const g of cur.value?.groups || []) {
    (g.anchors || []).concat(g.attachments || []).forEach((d: any) => out.push(d.document_id))
  }
  return out
})

const zipName = computed(() =>
  String(cur.value?.name || '业务包').replace(/[\\/:*?"<>|]/g, '_').slice(0, 60))

const money = (v: number | null | undefined) =>
  v === null || v === undefined
    ? '—'
    : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const numGap = (g: any) => Math.abs(Number(g.amount_gap || 0)) > 1
const dirCn = (d: string) =>
  (({ sale: '销售', purchase: '采购', mixed: '购销一体' } as Record<string, string>)[d] || '未定性')
const completenessText = (b: any) => b.completeness.have + '/' + b.completeness.need

/** 回款视角：钱没收齐的业务包要在列表上直接看得见。 */
const payWords = (b: any) => (b && b.payment && b.payment.words) || {}
const shortOf = (b: any) => !!(b && b.payment && Number(b.payment.outstanding) > 0)
const wan = (v: number) => (Math.abs(v) >= 10000
  ? (v / 10000).toFixed(1).replace(/\.0$/, '') + '万'
  : Number(v).toFixed(2))
function payChip(b: any) {
  return (payWords(b).short || '未收') + ' ' + wan(Number(b.payment.outstanding))
}
function payTip(b: any) {
  const w = payWords(b)
  return (w.due || '应收') + ' ' + money(b.payment.due) + ' · ' + (w.paid || '已收') + ' '
    + money(b.payment.paid) + ' · ' + (w.short || '未收') + ' ' + money(b.payment.outstanding)
}
const payDocs = computed(() => (cur.value?.groups || []).flatMap((g: any) => g.pay_docs || []))
const payTitle = computed(() => (payWords(cur.value).money || '收付') + '情况')

/** 附件角色是否已被必备清单接受（不在清单里的角色不算错）。 */
function reqOk(g: any, role: string) {
  const r = (g.required || []).find((x: any) => x.role === role)
  return !r || r.ok
}

function showDoc(d: any) {
  previewId.value = d.document_id
  previewOpen.value = true
}

onMounted(async () => {
  try {
    const saved = JSON.parse(localStorage.getItem(IGNORE_KEY) || '[]')
    saved.forEach((k: string) => { ignored.value[k] = true })
  } catch { localStorage.removeItem(IGNORE_KEY) }
  await load()
})
onActivated(load)
</script>
<template>
  <div>
    <el-alert v-if="sugs.length" class="sug-bar" type="success" :closable="false" show-icon>
      <template #title>
        <span style="cursor: pointer" @click="sugOpen = !sugOpen">
          发现 {{ sugs.length }} 组疑似同一业务（销售⇄采购成对），点击{{ sugOpen ? '收起' : '展开' }}逐条确认
        </span>
      </template>
      <div v-show="sugOpen" class="sug-list">
        <div v-for="p in sugs" :key="sugKey(p)" class="sug-row">
          <div class="sug-main">
            <div class="sug-title">{{ p.sale_name }} ⇄ {{ p.purchase_name }}</div>
            <div class="sug-sub">{{ p.reason }} · 我方主体 {{ p.our_entity }}</div>
          </div>
          <div class="sug-ops">
            <el-button size="small" type="primary" @click="mergeInto(p.sale_bundle_id, p.purchase_bundle_id)">合并</el-button>
            <el-button size="small" text @click="ignoreSug(p)">忽略</el-button>
          </div>
        </div>
        <div class="hint muted">
          合并会把采购包并进销售包，三方贸易分两次合并即可。
          <el-button link type="primary" size="small" @click="resetIgnored">恢复已忽略</el-button>
        </div>
      </div>
    </el-alert>
    <el-alert v-else-if="ignoredCount" class="sug-bar" type="info" :closable="false" show-icon>
      <template #title>
        已忽略 {{ ignoredCount }} 组合并建议，
        <el-button link type="primary" size="small" @click="resetIgnored">恢复显示</el-button>
      </template>
    </el-alert>

    <el-card class="page-card toolbar-card">
      <div class="toolbar">
        <el-input v-model="q" placeholder="搜合同号 / 客户 / 名称" clearable style="width: 250px" :disabled="busy"
                  @keyup.enter="page = 1; load()" @clear="page = 1; load()" />
        <el-select v-model="state" placeholder="全部状态" style="width: 128px" @change="page = 1; load()">
          <el-option label="全部状态" value="" />
          <el-option label="有缺失" value="missing" />
          <el-option label="已齐备" value="complete" />
        </el-select>
        <div class="grow" />
        <el-button :disabled="busy" @click="load">刷新</el-button>
        <el-button :disabled="busy" @click="rebuild">重建归集</el-button>
        <el-button type="primary" :disabled="busy" @click="newBundle">新建业务包</el-button>
      </div>
    </el-card>

    <div class="split" :class="isCompact() ? '' : 'split-4'">
      <el-card class="page-card" :header="'业务包（' + total + '）'">
        <div v-loading="busy" class="activity">
          <button v-for="b in items" :key="b.id" class="act-row" :class="{ on: cur?.id === b.id }" @click="open(b)">
            <span class="act-avatar" :class="{ plain: b.complete }">{{ completenessText(b) }}</span>
            <span class="act-main">
              <span class="act-title">{{ b.contract_nos?.[0] || b.name }}</span>
              <span class="act-sub pay-line">
                <span class="pay-line-t">{{ b.customer || '未识别客户' }} · {{ money(b.amount) }}</span>
                <span v-if="shortOf(b)" class="pay-chip" :title="payTip(b)">{{ payChip(b) }}</span>
              </span>
            </span>
            <span v-if="b.diff_count" class="dot bad" :title="'关联 ' + b.diff_count + ' 条未处理差异'">{{ b.diff_count }}</span>
            <span v-else-if="!b.complete" class="dot" :title="'缺 ' + b.completeness.missing.join('、')">!</span>
            <span class="act-time">{{ dirCn(b.direction) }}</span>
          </button>
          <el-empty v-if="!items.length && !busy" :image-size="60" description="还没有业务包：归档带合同编号的文档，或点「重建归集」" />
        </div>
        <el-pagination v-if="total > size" style="margin-top: 8px" layout="prev, pager, next" :total="total"
                       v-model:current-page="page" :page-size="size" @change="load" />
      </el-card>

      <div v-if="cur">
        <el-card class="page-card">
          <div class="bundle-head">
            <div class="bh-main">
              <div class="bh-title">{{ cur.name }}</div>
              <div class="hint muted">{{ cur.contract_nos.join(' · ') || '无合同编号（手工建的包）' }}</div>
            </div>
            <div class="bh-tags">
              <el-tag size="small" :type="cur.complete ? 'success' : 'danger'">必备单据 {{ cur.completeness.have }}/{{ cur.completeness.need }}</el-tag>
              <el-tag v-if="cur.diff_count" size="small" type="warning">{{ cur.diff_count }} 条差异</el-tag>
              <el-tag v-if="cur.status === 'settled'" size="small" type="info">已结清</el-tag>
              <el-tag size="small" effect="plain" :title="cur.source === 'auto' ? '由归档流水线自动维护' : '名字由你维护，重建归集不会覆盖'">{{ cur.source === 'auto' ? '自动归集' : '手工命名' }}</el-tag>
            </div>
          </div>
          <div class="bh-meta">
            <span><i>客户</i>{{ cur.customer || '—' }}</span>
            <span><i>我方主体</i>{{ cur.our_entity || '—' }}</span>
            <span><i>方向</i>{{ dirCn(cur.direction) }}</span>
            <span><i>合同金额</i>{{ money(cur.amount) }}</span>
            <span><i>签订日期</i>{{ cur.sign_date || '—' }}</span>
            <span><i>更新</i>{{ fmtTime(cur.updated_at) }}</span>
          </div>
          <div class="toolbar" style="margin-top: 12px">
            <el-button size="small" @click="openAdd">加入文档</el-button>
            <el-button size="small" @click="exportAll">导出整包</el-button>
            <el-button size="small" @click="rename(cur)">重命名</el-button>
            <el-button size="small" @click="toggleSettled(cur)">{{ cur.status === 'settled' ? '标记未结清' : '标记已结清' }}</el-button>
            <div class="grow" />
            <el-button size="small" text type="danger" @click="removeBundle(cur)">解散</el-button>
          </div>
        </el-card>

        <el-card v-for="(g, gi) in cur.groups" :key="gi" class="page-card grp-card" :header="g.contract_no || '未编号单据'">
          <div class="grp-sum hint muted">
            合同金额 {{ money(g.amount) }} · 已结算<span class="muted">（{{ g.settlement_role || '单据' }}）</span>
            {{ money(g.settlement_sum) }}
            <span v-if="g.amount_gap !== null && g.amount_gap !== undefined" :class="numGap(g) ? 'gap-bad' : 'gap-ok'">
              · 差额 {{ money(g.amount_gap) }}
            </span>
            <span v-if="g.missing.length" class="gap-bad">· 缺 {{ g.missing.join('、') }}</span>
            <span v-else-if="g.required.length" class="gap-ok">· 单据齐备</span>
            <span v-if="g.due_sum" class="pay-note">· {{ payWords(cur).paid || '已收' }}
              {{ money(g.pay_sum) }}<span v-if="Number(g.pay_gap) > 0" class="gap-bad">
                （{{ payWords(cur).short || '未收' }} {{ money(g.pay_gap) }}）</span></span>
          </div>

          <div class="doc-grid">
            <div v-for="d in g.anchors" :key="d.item_id" class="doc-tile anchor" @click="showDoc(d)">
              <DocThumb :doc-id="d.document_id" :file-type="d.file_type" :exists="d.file_exists" :size="104" />
              <div class="dt-cap">{{ d.filename }}</div>
              <div class="dt-meta"><el-tag size="small">{{ d.role }}</el-tag><span class="mono">{{ money(d.amount) }}</span></div>
              <div class="dt-meta"><span class="muted">{{ d.doc_date || '—' }}</span></div>
              <div class="dt-ops">
                <el-button size="small" text @click.stop="editRole(d)">改角色</el-button>
                <el-button size="small" text type="danger" @click.stop="pickDoc(d)">移出</el-button>
              </div>
            </div>

            <div v-for="d in g.attachments" :key="d.item_id" class="doc-tile" @click="showDoc(d)">
              <DocThumb :doc-id="d.document_id" :file-type="d.file_type" :exists="d.file_exists" :size="104" />
              <div class="dt-cap">{{ d.filename }}</div>
              <div class="dt-meta">
                <el-tag size="small" :type="reqOk(g, d.role) ? 'success' : 'warning'">{{ d.role }}</el-tag>
                <span class="mono">{{ money(d.amount) }}</span>
              </div>
              <div class="dt-meta"><span class="muted">{{ d.doc_date || '—' }}</span></div>
              <div class="dt-ops">
                <el-button size="small" text @click.stop="editRole(d)">改角色</el-button>
                <el-button size="small" text type="danger" @click.stop="pickDoc(d)">移出</el-button>
              </div>
            </div>

            <div v-for="r in g.required.filter((x: any) => !x.ok)" :key="'miss' + r.role" class="doc-tile miss" @click="uploadFor(r.role)">
              <div class="miss-box">
                <div class="miss-ico">＋</div>
                <div class="miss-title">缺{{ r.role }}</div>
                <div class="miss-sub">{{ REQ_HINT[r.role] || '点这里上传对应单据' }}</div>
                <el-button size="small" type="primary" plain @click.stop="uploadFor(r.role)">上传</el-button>
              </div>
            </div>
          </div>
        </el-card>

        <el-card v-if="cur.groups.length" class="page-card pay-card" :header="payTitle">
          <div class="grp-sum hint muted">
            {{ payWords(cur).due || '应收' }} {{ money(cur.payment?.due) }} ·
            {{ payWords(cur).paid || '已收' }} {{ money(cur.payment?.paid) }}
            <span v-if="Number(cur.payment?.outstanding) > 0" class="gap-bad">
              · {{ payWords(cur).short || '未收' }} {{ money(cur.payment.outstanding) }}</span>
            <span v-else-if="Number(cur.payment?.outstanding) < 0" class="gap-bad">
              · 超出{{ payWords(cur).due || '应收' }} {{ money(-Number(cur.payment.outstanding)) }}</span>
            <span v-else-if="cur.payment?.due" class="gap-ok">· 已收齐</span>
            <span v-else class="muted">· 结算金额未定，暂不核对{{ payWords(cur).money || '收付' }}</span>
          </div>
          <div class="doc-grid">
            <div v-for="d in payDocs" :key="'pay' + d.item_id" class="doc-tile" @click="showDoc(d)">
              <DocThumb :doc-id="d.document_id" :file-type="d.file_type" :exists="d.file_exists" :size="104" />
              <div class="dt-cap">{{ d.filename }}</div>
              <div class="dt-meta">
                <el-tag size="small" type="success">{{ d.role }}</el-tag>
                <span class="mono">{{ money(d.amount) }}</span>
              </div>
              <div class="dt-meta"><span class="muted">{{ d.doc_date || '—' }}</span></div>
              <div class="dt-ops">
                <el-button size="small" text @click.stop="editRole(d)">改角色</el-button>
                <el-button size="small" text type="danger" @click.stop="pickDoc(d)">移出</el-button>
              </div>
            </div>
            <div v-if="!payDocs.length" class="doc-tile miss pay-miss" @click="uploadFor('银行回单')">
              <div class="miss-box">
                <div class="miss-ico">＋</div>
                <div class="miss-title">缺{{ payWords(cur).voucher || '收付凭证' }}</div>
                <div class="miss-sub">{{ REQ_HINT['银行回单'] || '点这里上传回单/凭证' }}</div>
                <el-button size="small" type="primary" plain @click.stop="uploadFor('银行回单')">上传</el-button>
              </div>
            </div>
          </div>
          <div class="hint muted pay-hint">
            对账按「{{ payWords(cur).due || '应收' }} = 已结算金额」比对凭证合计，容差可在设置里调；
            缺凭证/金额不符会作为差异出现在台账对账页，从那里上传同样会自动归进本包。
          </div>
        </el-card>

        <el-empty v-if="!cur.groups.length" description="这个包里还没有文档，点「加入文档」把单据关联进来" />
      </div>

      <el-card v-else class="page-card">
        <el-empty :image-size="80" description="左侧选择一个业务包" />
      </el-card>
    </div>

    <input ref="fileInput" type="file" multiple hidden @change="onFiles" />

    <el-dialog v-model="addOpen" title="把已有文档加入本业务包" :width="isCompact() ? '94%' : '640px'">
      <el-input v-model="addQuery" placeholder="按文件名 / 合同号 / 客户搜索，回车" clearable
                @keyup.enter="searchCandidates(addQuery)" @clear="searchCandidates('')" />
      <el-table :data="addCandidates" size="small" style="margin-top: 10px" max-height="330"
                @selection-change="(v: any[]) => (addPicked = v.map((x) => x.id))">
        <el-table-column type="selection" width="42" />
        <el-table-column prop="current_filename" label="文件" show-overflow-tooltip />
        <el-table-column prop="document_type" label="类型" width="104" class-name="col-md" label-class-name="col-md" />
        <el-table-column prop="status" label="状态" width="88" class-name="col-md" label-class-name="col-md" />
      </el-table>
      <div class="hint muted" style="margin-top: 8px">加入后角色默认按文档类型识别，进包后仍可单独改。</div>
      <template #footer>
        <el-button @click="addOpen = false">取消</el-button>
        <el-button type="primary" @click="doAdd">加入（{{ addPicked.length }}）</el-button>
      </template>
    </el-dialog>

    <PreviewDialog v-model="previewOpen" v-model:doc-id="previewId" :ids="docIds" :title="cur?.name || ''" />
  </div>
</template>

<style scoped>
.sug-bar { margin-bottom: var(--df-gap); }
.sug-list { margin-top: 6px; display: flex; flex-direction: column; gap: 6px; }
.sug-row { display: flex; align-items: center; gap: 12px; padding: 8px 10px; background: var(--df-surface);
  border-radius: 12px; box-shadow: var(--df-shadow-sm); }
.sug-main { min-width: 0; flex: 1 1 auto; }
.sug-title { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sug-sub { font-size: 12px; color: var(--el-text-color-secondary); }
.sug-ops { flex: 0 0 auto; }

.activity button.act-row { width: 100%; text-align: left; border: none; background: transparent; font: inherit;
  color: inherit; cursor: pointer; }
.activity .act-row.on { background: var(--df-tint); }
.activity .act-row.on .act-title { color: var(--df-green-deep); font-weight: 700; }
.dot { flex: 0 0 auto; min-width: 18px; height: 18px; border-radius: 9px; padding: 0 5px; display: grid;
  place-items: center; font-size: 11px; font-weight: 700; background: var(--el-color-warning); color: #fff; }
.dot.bad { background: var(--el-color-danger); }

.bundle-head { display: flex; align-items: flex-start; gap: 14px; }
.bh-main { min-width: 0; flex: 1 1 auto; }
.bh-title { font-size: 16px; font-weight: 700; line-height: 1.4; word-break: break-all; }
.bh-tags { flex: 0 0 auto; display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.bh-meta { display: grid; gap: 6px 18px; grid-template-columns: repeat(auto-fit, minmax(178px, 1fr));
  margin-top: 12px; font-size: 12.5px; }
.bh-meta span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bh-meta i { font-style: normal; color: var(--el-text-color-secondary); margin-right: 6px; }

.grp-sum { margin-bottom: 10px; }
.activity .act-sub.pay-line { display: flex; align-items: center; gap: 6px; overflow: visible; }
.activity .pay-line-t { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pay-chip { flex: 0 0 auto; font-size: 11px; font-weight: 600; padding: 0 7px; line-height: 17px;
  border-radius: 999px; color: var(--el-color-warning); background: var(--el-color-warning-light-9);
  border: 1px solid var(--el-color-warning-light-7); }
.pay-note { margin-left: 4px; }
.pay-hint { margin-top: 10px; line-height: 1.7; }
.gap-bad { color: var(--el-color-danger); font-weight: 600; }
.gap-ok { color: var(--df-green-deep); }

.doc-tile { position: relative; background: var(--df-surface); border: 1px solid var(--df-line);
  border-radius: 16px; padding: 10px; cursor: pointer; transition: transform .14s, box-shadow .14s;
  display: flex; flex-direction: column; gap: 6px; }
.doc-tile:hover { transform: translateY(-3px); box-shadow: var(--df-shadow); }
.doc-tile.anchor { border-color: var(--df-tint-2); background: var(--df-tint); }
.doc-tile.miss { border-style: dashed; border-color: var(--el-color-danger-light-5);
  background: var(--df-surface-2); justify-content: center; }
.doc-tile.miss:hover { border-color: var(--el-color-danger); }
.miss-box { display: flex; flex-direction: column; align-items: center; gap: 5px; text-align: center; padding: 8px 4px; }
.miss-ico { width: 42px; height: 42px; border-radius: 50%; display: grid; place-items: center; font-size: 20px;
  background: var(--el-color-danger-light-9); color: var(--el-color-danger); }
.miss-title { font-size: 13px; font-weight: 700; color: var(--el-color-danger); }
.miss-sub { font-size: 11.5px; color: var(--el-text-color-secondary); line-height: 1.5; min-height: 3em;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.dt-cap { font-size: 12.5px; font-weight: 600; line-height: 1.45; overflow: hidden; text-overflow: ellipsis;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; word-break: break-all; }
.dt-meta { display: flex; align-items: center; gap: 6px; font-size: 11.5px; min-height: 20px; }
.dt-meta .mono { margin-left: auto; }
.dt-ops { display: flex; gap: 2px; margin-top: auto; opacity: 0; transition: opacity .14s; }
.doc-tile:hover .dt-ops { opacity: 1; }
.doc-grid { grid-template-columns: repeat(auto-fill, minmax(168px, 1fr)); }

@media (max-width: 640px) {
  .bh-meta { grid-template-columns: 1fr 1fr; }
  .doc-grid { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }
  .dt-ops { opacity: 1; }
}
</style>
