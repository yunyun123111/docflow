<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, onMounted, ref } from 'vue'
import http from '../api/client'
import { useRouter } from 'vue-router'
import { fmtTime } from '../utils/format'

const router = useRouter()

const tab = ref('categories')
const tree = ref<any[]>([])
const flatCats = ref<any[]>([])
const rules = ref<any[]>([])
const templates = ref<any[]>([])
const fnameRules = ref<any[]>([])
const samples = ref<any[]>([])
const reqs = ref<any[]>([])
const reqDirty = ref(false)
const ruleDialog = ref(false)
const ruleForm = ref<any>({ name: '', pattern: '', match_type: 'keyword', field: 'any', category_id: null, document_type: '', confidence: 0.9, priority: 100, enabled: true })
const sim = ref<any>({ filename: '', text: '', result: null })

async function loadCats() {
  tree.value = await http.get('/categories/tree')
  flatCats.value = await http.get('/categories/flat')
}
async function loadRules() { rules.value = await http.get('/rules') }
async function loadTemplates() { templates.value = await http.get('/rules/templates/rename') }
async function loadFname() { fnameRules.value = await http.get('/rules/filename') }
async function loadSamples() { samples.value = await http.get('/review/samples') }
async function loadReqs() {
  reqs.value = await http.get('/bundles/requirements')
  reqDirty.value = false
}

async function addCat(parent: any) {
  const { value } = await ElMessageBox.prompt('分类名', parent ? `在「${parent.name}」下新增` : '新增一级分类')
  if (!value) return
  await http.post('/categories', { name: value, parent_id: parent?.id ?? null })
  await loadCats()
}
async function delCat(c: any) {
  await ElMessageBox.confirm(`删除分类「${c.name}」？`, '删除', { type: 'warning' })
  await http.delete(`/categories/${c.id}`)
  await loadCats()
}
async function editKeywords(c: any) {
  const cur: any = await http.get(`/categories/${c.id}/keywords`)
  const { value } = await ElMessageBox.prompt('逗号分隔', `「${c.name}」关键词`, { inputValue: cur.join(', '), inputType: 'textarea' })
  if (value === null) return
  await http.put(`/categories/${c.id}/keywords`, { keywords: value.split(/[,，]/).map((s: string) => s.trim()).filter(Boolean) })
  ElMessage.success('已保存')
}

function newRule() { ruleForm.value = { name: '', pattern: '', match_type: 'keyword', field: 'any', category_id: null, document_type: '', confidence: 0.9, priority: 100, enabled: true }; ruleDialog.value = true }
async function saveRule() {
  if (ruleForm.value.id) await http.put(`/rules/${ruleForm.value.id}`, ruleForm.value)
  else await http.post('/rules', ruleForm.value)
  ruleDialog.value = false
  await loadRules()
}
async function delRule(r: any) { await http.delete(`/rules/${r.id}`); await loadRules() }
async function toggleRule(r: any) { await http.put(`/rules/${r.id}`, { ...r }); await loadRules() }

async function addTemplate() {
  const { value } = await ElMessageBox.prompt(
    '占位符：{date} {contract_no} {amount} {company} {counterparty} {title} {type} {category} {year} {month}；分隔符直接写 _ 或 -',
    '新增重命名模板', { inputPlaceholder: '如：发票_{date}_{amount}元' })
  if (!value) return
  const picked = await ElMessageBox.prompt(
    '这条模板适用于哪个单据类型？通用默认改名规则请在「系统设置 → 目录与归档」里改，改完立刻生效。',
    '新增重命名模板', { inputPlaceholder: '如：发票 / 银行回单', inputValue: '', confirmButtonText: '保存' })
    .then((r) => String(r.value || '').trim()).catch(() => null)
  if (picked === null) return
  if (!picked) ElMessage.warning('没有适用类型的模板不会自动生效，只能在审核页手动选中')
  await http.post('/rules/templates/rename', { name: value.slice(0, 12), pattern: value, document_type: picked, is_default: false })
  await loadTemplates()
}
async function delTemplate(t: any) { await http.delete(`/rules/templates/rename/${t.id}`); await loadTemplates() }

async function addFname() {
  const { value } = await ElMessageBox.prompt('正则 → 替换（用 | 分隔，替换可为空）', '新增文件名规则', { inputPlaceholder: '如：副本.*$|  或  \(1\)|' })
  if (!value) return
  const [regex, replacement = ''] = value.split('|')
  await http.post('/rules/filename', { name: regex.slice(0, 20), regex, replacement, priority: 100, enabled: true })
  await loadFname()
}
async function delFname(r: any) { await http.delete(`/rules/filename/${r.id}`); await loadFname() }

async function runSim() {
  sim.value.result = await http.post('/rules/simulate', { filename: sim.value.filename, text: sim.value.text })
}

const countCats = computed(() => flatCats.value.length)
function refreshAll() { loadCats(); loadRules(); loadTemplates(); loadFname(); loadSamples(); loadReqs() }
const CONTRACT_ROLES = ['销售合同', '采购合同', '框架合同', '补充协议']
const REQUIRED_ROLES = ['发票', '结算单', '提货单', '入库单', '出货单', '银行回单']

function markDirty() { reqDirty.value = true }
function addReq() {
  reqs.value.push({ contract_role: '销售合同', required_role: '发票', min_count: 1,
    amount_check: true, enabled: true })
  markDirty()
}
function delReq(row: any) { reqs.value = reqs.value.filter((x) => x !== row); markDirty() }

async function saveReqs() {
  if (reqs.value.some((x) => !x.contract_role || !x.required_role)) {
    ElMessage.warning('合同类型与必备单据都不能留空'); return
  }
  const dup = new Set(reqs.value.map((x) => x.contract_role + '|' + x.required_role))
  if (dup.size !== reqs.value.length) { ElMessage.warning('同一种合同的同一种单据不要重复配置'); return }
  await http.put('/bundles/requirements', reqs.value.map((x) => ({
    contract_role: x.contract_role, required_role: x.required_role, min_count: x.min_count,
    amount_check: x.amount_check, enabled: x.enabled,
  })))
  reqDirty.value = false
  ElMessage.success('已保存，下次对账与归集按新清单计算')
}

onMounted(refreshAll)
onActivated(refreshAll)
</script>

<template>
  <el-card>
    <el-tabs v-model="tab">
      <el-tab-pane :label="`分类与关键词 (${countCats})`" name="categories">
        <el-button size="small" @click="addCat(null)">新增一级分类</el-button>
        <el-tree-node-content>
          <el-tree :data="tree" node-key="id" default-expand-all style="margin-top: 10px">
            <template #default="{ data }">
              <span class="cat-row">
                <span>{{ data.name }}<small class="muted" style="margin-left: 6px">{{ data.doc_count ? `已归档 ${data.doc_count}` : '' }}</small></span>
                <span>
                  <el-button size="small" text @click.stop="editKeywords(data)">关键词</el-button>
                  <el-button size="small" text @click.stop="addCat(data)">子分类</el-button>
                  <el-button size="small" text type="danger" @click.stop="delCat(data)">删</el-button>
                </span>
              </span>
            </template>
          </el-tree>
        </el-tree-node-content>
      </el-tab-pane>

      <el-tab-pane label="识别规则" name="rules">
        <el-button size="small" type="primary" @click="newRule">新增规则</el-button>
        <el-table :data="rules" size="small" style="margin-top: 10px">
          <el-table-column prop="name" label="名称" width="160" />
          <el-table-column prop="pattern" label="模式" width="160" show-overflow-tooltip />
          <el-table-column prop="match_type" label="方式" width="80" />
          <el-table-column prop="field" label="作用域" width="80" />
          <el-table-column prop="document_type" label="判为类型" width="110" />
          <el-table-column label="分类" width="150">
            <template #default="{ row }">{{ flatCats.find(c => c.id === row.category_id)?.path || '-' }}</template>
          </el-table-column>
          <el-table-column prop="confidence" label="置信" width="70" />
          <el-table-column prop="priority" label="优先级" width="70" />
          <el-table-column label="启用" width="70">
            <template #default="{ row }"><el-switch :model-value="row.enabled" size="small" @change="toggleRule(row)" /></template>
          </el-table-column>
          <el-table-column label="操作" width="110">
            <template #default="{ row }">
              <el-button size="small" text @click="ruleForm = { ...row }; ruleDialog = true">编辑</el-button>
              <el-button size="small" text type="danger" @click="delRule(row)">删</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="重命名模板" name="templates">
        <div class="hint" style="margin-bottom: 8px">
          这里只放「按单据类型定制」的模板（如发票、银行回单）；通用默认的重命名规则在「系统设置 → 目录与归档」里改，
          那里改完立刻生效。适用类型留空的模板不会被自动选中。
        </div>
        <el-button size="small" @click="addTemplate">新增模板</el-button>
        <el-table :data="templates" size="small" style="margin-top: 10px">
          <el-table-column prop="name" label="名称" width="140" />
          <el-table-column prop="pattern" label="模板" show-overflow-tooltip />
          <el-table-column prop="document_type" label="适用类型" width="140" />
          <el-table-column label="默认" width="80">
            <template #default="{ row }"><el-tag v-if="row.is_default" size="small">默认</el-tag></template>
          </el-table-column>
          <el-table-column width="70">
            <template #default="{ row }"><el-button size="small" text type="danger" @click="delTemplate(row)">删</el-button></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="文件名清洗" name="fname">
        <el-button size="small" @click="addFname">新增规则</el-button>
        <el-table :data="fnameRules" size="small" style="margin-top: 10px">
          <el-table-column prop="name" width="150" />
          <el-table-column prop="regex" label="正则" />
          <el-table-column prop="replacement" label="替换为" width="120" />
          <el-table-column width="70">
            <template #default="{ row }"><el-button size="small" text type="danger" @click="delFname(row)">删</el-button></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane :label="`样例库 (${samples.length})`" name="samples">
        <div style="font-size: 12px; color: var(--el-text-color-secondary); margin-bottom: 8px">
          审核确认时勾选“存为样例”，同模板文档下次零成本直接命中。
        </div>
        <el-table :data="samples" size="small">
          <el-table-column prop="name" label="样例" />
          <el-table-column prop="document_type" label="类型" width="140" />
          <el-table-column label="时间" width="160">
            <template #default="{ row }"><span class="mono muted">{{ fmtTime(row.created_at) }}</span></template>
          </el-table-column>
          <el-table-column width="70">
            <template #default="{ row }"><el-button size="small" text type="danger" @click="http.delete(`/review/samples/${row.id}`).then(loadSamples)">删</el-button></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="归集与必备单据" name="bundles">
        <div class="hint muted" style="margin-bottom: 10px">
          每类合同需要哪些随附单据，直接决定文档归集的齐备度和对账里的「缺发票 / 缺提货单」差异。
          回款凭证不在这里配置（由「未回款」检查负责），避免同一件事报两条。
        </div>
        <el-button size="small" type="primary" @click="addReq">新增一条</el-button>
        <el-button size="small" @click="saveReqs" :disabled="!reqDirty">保存清单</el-button>
        <el-tag v-if="reqDirty" size="small" type="warning" style="margin-left: 8px">有未保存的修改</el-tag>
        <el-table :data="reqs" size="small" style="margin-top: 10px">
          <el-table-column label="合同类型" width="170">
            <template #default="{ row }">
              <el-select v-model="row.contract_role" size="small" filterable allow-create style="width: 150px"
                         @change="markDirty()">
                <el-option v-for="r in CONTRACT_ROLES" :key="r" :label="r" :value="r" />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="必备单据" width="170">
            <template #default="{ row }">
              <el-select v-model="row.required_role" size="small" filterable allow-create style="width: 150px"
                         @change="markDirty()">
                <el-option v-for="r in REQUIRED_ROLES" :key="r" :label="r" :value="r" />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="至少几份" width="110">
            <template #default="{ row }">
              <el-input-number v-model="row.min_count" :min="1" :max="20" size="small" controls-position="right"
                               style="width: 88px" @change="markDirty()" />
            </template>
          </el-table-column>
          <el-table-column label="核对金额" width="100">
            <template #default="{ row }"><el-switch v-model="row.amount_check" size="small" @change="markDirty()" /></template>
          </el-table-column>
          <el-table-column label="启用" width="80">
            <template #default="{ row }"><el-switch v-model="row.enabled" size="small" @change="markDirty()" /></template>
          </el-table-column>
          <el-table-column label="操作" width="80">
            <template #default="{ row }">
              <el-button size="small" text type="danger" @click="delReq(row)">删</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="hint muted" style="margin-top: 10px">
          合同类型取自分类叶子名（销售合同 / 采购合同 / 框架合同 / 补充协议），也可以手写你自己的叶子名。
          采购↔销售包的合并建议在「文档归集」页处理，阈值在
          <el-button link type="primary" size="small" @click="router.push('/settings')">设置 → 归集</el-button>
          里调。
        </div>
      </el-tab-pane>

      <el-tab-pane label="规则模拟器" name="sim">
        <el-input v-model="sim.filename" placeholder="文件名" style="margin-bottom: 8px" />
        <el-input v-model="sim.text" type="textarea" :rows="6" placeholder="粘贴文档正文，测试规则命中与置信度" style="margin-bottom: 8px" />
        <el-button type="primary" @click="runSim">模拟识别</el-button>
        <template v-if="sim.result">
          <el-alert style="margin-top: 10px" :closable="false" type="success">
            类型：<b>{{ sim.result.result.document_type }}</b> ·
            分类：{{ sim.result.result.category_path || '-' }} ·
            置信度：<b>{{ sim.result.result.confidence }}</b> → {{ sim.result.decision === 'auto_archive' ? '自动归档' : sim.result.decision === 'review' ? '人工审核' : '人工判断' }}
          </el-alert>
          <el-descriptions :column="4" border size="small" style="margin-top: 8px">
            <el-descriptions-item v-for="(v, k) in sim.result.result.detail" :key="k" :label="String(k)">{{ v }}</el-descriptions-item>
          </el-descriptions>
          <el-table :data="sim.result.rule_hits" size="small" style="margin-top: 8px">
            <el-table-column prop="name" label="命中规则" />
            <el-table-column prop="document_type" label="判为" width="100" />
            <el-table-column label="依据" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="row.where === 'title' ? 'success' : 'info'">
                  {{ row.where === 'title' ? '标题命中' : '正文提及' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="confidence" label="基准" width="80" />
            <el-table-column prop="score" label="计分" width="80" />
          </el-table>
          <div class="sim-note">正文深处提到「发票/提货」只按 0.55 折算，压不过标题里的类型词。</div>
        </template>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="ruleDialog" :title="ruleForm.id ? '编辑规则' : '新增规则'" width="520px">
      <el-form label-width="90px">
        <el-form-item label="名称"><el-input v-model="ruleForm.name" /></el-form-item>
        <el-form-item label="模式"><el-input v-model="ruleForm.pattern" placeholder="关键词或正则" /></el-form-item>
        <el-form-item label="匹配方式">
          <el-radio-group v-model="ruleForm.match_type"><el-radio value="keyword">关键词</el-radio><el-radio value="regex">正则</el-radio></el-radio-group>
        </el-form-item>
        <el-form-item label="作用域">
          <el-select v-model="ruleForm.field" style="width: 160px">
            <el-option label="文件名+正文" value="any" /><el-option label="仅文件名" value="filename" /><el-option label="仅正文" value="text" />
          </el-select>
        </el-form-item>
        <el-form-item label="判为类型"><el-input v-model="ruleForm.document_type" placeholder="如：合同 / 发票" /></el-form-item>
        <el-form-item label="归入分类">
          <el-select v-model="ruleForm.category_id" clearable style="width: 240px">
            <el-option v-for="c in flatCats" :key="c.id" :label="c.path" :value="c.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="置信度"><el-slider v-model="ruleForm.confidence" :min="0" :max="1" :step="0.01" show-input /></el-form-item>
        <el-form-item label="优先级"><el-input-number v-model="ruleForm.priority" :min="1" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="ruleForm.enabled" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="ruleDialog = false">取消</el-button><el-button type="primary" @click="saveRule">保存</el-button></template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.cat-row { display: flex; justify-content: space-between; align-items: center; width: 100%; padding-right: 8px; }
.sim-note { margin-top: 6px; font-size: 12px; color: var(--df-muted); }
</style>