<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import http, { desktop } from '../api/client'
import SelfCheckPanel from '../components/SelfCheckPanel.vue'
import { fmtTime } from '../utils/format'

const router = useRouter()

const s = ref<any>(null)
const tab = ref('dirs')
const saving = ref(false)
const aiTesting = ref(false)
const emailTesting = ref(false)
const newPwd = ref('')
const watchDirsText = ref('')
const backups = ref<any[]>([])
const components = ref<any>(null)
const info = ref<any>(null)

async function load() {
  s.value = await http.get('/settings')
  // 后端存的是小数容差，界面上按百分比更好读，也更好改
  s.value.bundle.amount_tolerance_pct = Math.round(Number(s.value.bundle.amount_tolerance || 0) * 1000) / 10
  watchDirsText.value = (s.value.watch.dirs || []).join('\n')
  backups.value = await http.get('/backup/list')
  components.value = await http.get('/system/components')
  info.value = await http.get('/system/info')
}

async function save() {
  saving.value = true
  try {
    const payload: any = {
      document_root: s.value.document_root, inbox_root: s.value.inbox_root,
      archive_template: s.value.archive_template, rename_template: s.value.rename_template,
      auto_archive_threshold: s.value.auto_archive_threshold,
      review_threshold: s.value.review_threshold,
      w_rule: s.value.weights.rule, w_field: s.value.weights.field,
      w_keyword: s.value.weights.keyword, w_ai: s.value.weights.ai,
      allow_overwrite: s.value.allow_overwrite,
      recycle_retention_days: s.value.recycle_retention_days,
      max_workers: s.value.max_workers, max_upload_mb: s.value.max_upload_mb,
      ocr_enabled: s.value.ocr_enabled, ocr_max_pages: s.value.ocr_max_pages,
      ai_enabled: s.value.ai.enabled, ai_base_url: s.value.ai.base_url,
      ai_model: s.value.ai.model, ai_timeout: s.value.ai.timeout,
      watch_enabled: s.value.watch.enabled, watch_interval: s.value.watch.interval,
      seal_detect_enabled: s.value.seal_detect_enabled,
      email_enabled: s.value.email.enabled, email_imap_host: s.value.email.host,
      email_imap_port: s.value.email.port, email_ssl: s.value.email.ssl,
      email_user: s.value.email.user, email_poll_interval: s.value.email.poll_interval,
      email_sender_whitelist: s.value.email.sender_whitelist,
      email_subject_prefix: s.value.email.subject_prefix,
      recon_enabled: s.value.recon.enabled, recon_interval_minutes: s.value.recon.interval_minutes,
      auto_backup_enabled: s.value.auto_backup.enabled, auto_backup_keep: s.value.auto_backup.keep,
      bundle_merge_suggest_enabled: s.value.bundle.merge_suggest_enabled,
      bundle_merge_amount_tolerance: s.value.bundle.amount_tolerance_pct / 100,
      bundle_merge_date_window_days: s.value.bundle.date_window_days,
      host: s.value.host,
    }
    try {
      payload.watch_dirs = JSON.parse(JSON.stringify(watchDirsText.value.split('\n').map((x) => x.trim()).filter(Boolean)))
    } catch {
      ElMessage.error('监控目录格式异常，每行一个目录')
      saving.value = false
      return
    }
    if (newPwd.value !== '') payload.access_password = newPwd.value
    if (s.value.ai.api_key_input) { payload.ai_api_key = s.value.ai.api_key_input; s.value.ai.api_key_input = '' }
    if (s.value.email.password_input) { payload.email_password = s.value.email.password_input; s.value.email.password_input = '' }
    await http.put('/settings', payload)
    ElMessage.success('已保存并生效（监听地址在下次启动时生效）')
    newPwd.value = ''
    await load()
  } finally {
    saving.value = false
  }
}

async function pickDir(key: 'document_root' | 'inbox_root') {
  const path = await desktop.pickDirectory(key === 'document_root' ? '选择归档根目录' : '选择待整理目录',
    String(s.value[key] || ''))
  if (path) s.value[key] = path
}

async function testAi() {
  aiTesting.value = true
  try {
    const r: any = await http.post('/settings/test/ai')
    r.ok ? ElMessage.success(`AI 正常：${r.echo}`) : ElMessage.warning(r.detail || 'AI 未启用')
  } finally { aiTesting.value = false }
}

async function testEmail() {
  emailTesting.value = true
  try {
    const r: any = await http.post('/settings/test/email')
    r.ok ? ElMessage.success(`本轮新附件 ${r.new_attachments} 个`) : ElMessage.warning(r.detail || '未配置')
  } finally { emailTesting.value = false }
}

async function createBackup() {
  const r: any = await http.post('/backup/create', { include_documents: false })
  ElMessage.success(`已生成 ${r.filename}`)
  backups.value = await http.get('/backup/list')
}

async function downloadBackup(b: any) {
  const blob: any = await http.get(`/backup/download/${encodeURIComponent(b.filename)}`, { responseType: 'blob' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = b.filename
  a.click()
  URL.revokeObjectURL(url)
}

async function removeBackup(b: any) {
  await ElMessageBox.confirm(`删除备份「${b.filename}」？`, '删除备份', { type: 'warning' })
  await http.delete(`/backup/${encodeURIComponent(b.filename)}`)
  ElMessage.success('已删除')
  backups.value = await http.get('/backup/list')
}

async function restore(b: any) {
  await ElMessageBox.confirm(
    `用「${b.filename}」恢复数据库？当前数据库会先自动备份一次，文件不会被覆盖。`,
    '恢复备份', { type: 'warning' },
  )
  const r: any = await http.post('/backup/restore', { filename: b.filename })
  ElMessage.success(`已恢复（${r.restored ?? ''}）`)
  await load()
}

onMounted(load)
onActivated(load)
</script>

<template>
  <div v-if="s" class="settings">
    <el-tabs v-model="tab" class="page-card">
      <el-tab-pane label="目录与归档" name="dirs">
        <el-form label-position="top" size="default">
          <div class="split split-2">
            <el-form-item label="归档根目录">
              <div class="row">
                <el-input v-model="s.document_root" />
                <el-button v-if="desktop.available" @click="pickDir('document_root')">浏览…</el-button>
              </div>
            </el-form-item>
            <el-form-item label="待整理目录（inbox）">
              <div class="row">
                <el-input v-model="s.inbox_root" />
                <el-button v-if="desktop.available" @click="pickDir('inbox_root')">浏览…</el-button>
              </div>
            </el-form-item>
          </div>
          <el-form-item label="归档结构模板">
            <el-input v-model="s.archive_template" />
            <div class="hint">可用 {category} {year} {month} {day} {company} {contract_no} 等；空段自动跳过。例：{category}/{year}/{month}</div>
          </el-form-item>
          <el-form-item label="默认重命名模板">
            <el-input v-model="s.rename_template" />
            <div class="hint">支持 {date} {underscore} {contract_no} {title} {index} 等字段段</div>
          </el-form-item>
          <div class="split split-2">
            <el-form-item label="回收站保留天数（0=永不自动清理）">
              <el-input-number v-model="s.recycle_retention_days" :min="0" :max="3650" />
            </el-form-item>
            <el-form-item label="并发识别线程数">
              <el-input-number v-model="s.max_workers" :min="1" :max="16" />
            </el-form-item>
            <el-form-item label="单文件上传上限 MB">
              <el-input-number v-model="s.max_upload_mb" :min="10" :max="2048" />
            </el-form-item>
            <el-form-item label="允许覆盖同名文件">
              <el-switch v-model="s.allow_overwrite" />
              <span class="hint" style="margin-left: 10px">关闭时重名自动 _001 编号，最安全</span>
            </el-form-item>
          </div>
        </el-form>
      </el-tab-pane>

      <el-tab-pane label="置信度与阈值" name="threshold">
        <div class="split split-2">
          <div>
            <el-form label-position="top">
              <el-form-item label="自动归档阈值（≥ 直接归档）">
                <el-slider v-model="s.auto_archive_threshold" :min="0.5" :max="1" :step="0.01" show-input />
              </el-form-item>
              <el-form-item label="送审下限（< 直接留待整理）">
                <el-slider v-model="s.review_threshold" :min="0.3" :max="1" :step="0.01" show-input />
              </el-form-item>
            </el-form>
            <el-alert :closable="false" type="info"
                      title="介于两者之间的文档进入人工审核队列；看板里的「置信度分布」是调参依据。" />
          </div>
          <el-form label-position="top">
            <el-form-item label="规则权重"><el-slider v-model="s.weights.rule" :min="0" :max="1" :step="0.05" show-input /></el-form-item>
            <el-form-item label="字段权重"><el-slider v-model="s.weights.field" :min="0" :max="1" :step="0.05" show-input /></el-form-item>
            <el-form-item label="关键词权重"><el-slider v-model="s.weights.keyword" :min="0" :max="1" :step="0.05" show-input /></el-form-item>
            <el-form-item label="AI 权重"><el-slider v-model="s.weights.ai" :min="0" :max="1" :step="0.05" show-input /></el-form-item>
          </el-form>
        </div>
        <div class="hint">未启用 AI 时，剩余权重自动归一化，纯规则 + OCR + 样例照样能自动归档。</div>
      </el-tab-pane>

      <el-tab-pane label="无人值守" name="auto">
        <div class="split split-2">
          <el-card class="sub" shadow="never">
            <template #header>
              目录监控 <el-switch v-model="s.watch.enabled" style="margin-left: 8px" />
              <span class="muted hint">轮询 {{ s.watch.interval }} 秒</span>
            </template>
            <el-input v-model="watchDirsText" type="textarea" :rows="4" placeholder="每行一个目录，如 D:\共享\incoming" />
            <div class="hint">新文件写完（稳定性窗口）后自动整理；系统忙时自动跳过本轮。</div>
            <div class="row" style="margin-top: 8px">
              <span class="muted">间隔秒数</span>
              <el-input-number v-model="s.watch.interval" size="small" :min="5" :max="600" />
              <el-tag size="small" :type="components?.watcher.running ? 'success' : 'info'">
                {{ components?.watcher.running ? '运行中' : '未运行' }}
              </el-tag>
            </div>
          </el-card>
          <el-card class="sub" shadow="never">
            <template #header>
              邮箱接收 <el-switch v-model="s.email.enabled" style="margin-left: 8px" />
              <el-button size="small" style="margin-left: 8px" :loading="emailTesting" @click="testEmail">立即收取</el-button>
            </template>
            <div class="split split-2">
              <el-input v-model="s.email.host" placeholder="imap.example.com" />
              <el-input v-model.number="s.email.port" placeholder="993" />
              <el-input v-model="s.email.user" placeholder="邮箱账号" />
              <el-input v-model="s.email.password_input" type="password" show-password
                        :placeholder="s.email.password_set ? '已保存（输入新值覆盖）' : '授权码/密码'" />
            </div>
            <div class="row" style="margin-top: 8px">
              <el-checkbox v-model="s.email.ssl">SSL</el-checkbox>
              <span class="muted">轮询</span>
              <el-input-number v-model="s.email.poll_interval" size="small" :min="30" :max="3600" />
              <el-tag size="small" :type="components?.email.running ? 'success' : 'info'">
                {{ components?.email.running ? '运行中' : '未运行' }}
              </el-tag>
            </div>
            <el-input v-model="s.email.sender_whitelist" size="small" style="margin-top: 8px"
                      placeholder="发件人白名单（逗号分隔，空=不限）" />
            <el-input v-model="s.email.subject_prefix" size="small" style="margin-top: 6px"
                      placeholder="主题路由前缀，如「整理」→ 邮件主题「整理[发票]: xx」" />
          </el-card>
          <el-card class="sub" shadow="never">
            <template #header>
              台账对账巡检 <el-switch v-model="s.recon.enabled" style="margin-left: 8px" />
            </template>
            <div class="row">
              <span class="muted">每</span>
              <el-input-number v-model="s.recon.interval_minutes" size="small" :min="10" :max="10080" />
              <span class="muted">分钟自动比对一次</span>
            </div>
            <div class="hint">上次巡检：{{ components?.recon.last_run || '尚未运行' }}</div>
          </el-card>
          <el-card class="sub" shadow="never">
            <template #header>
              文档归集 <el-switch v-model="s.bundle.merge_suggest_enabled" style="margin-left: 8px" />
            </template>
            <div class="hint" style="margin-bottom: 8px">
              同一合同号的销售合同、发票、结算单、提货单会自动归进一个业务包；这里控制「销售⇄采购」成对的合并建议。
            </div>
            <div class="row">
              <span class="muted">金额相对差 ≤</span>
              <el-input-number v-model="s.bundle.amount_tolerance_pct" size="small" :min="0" :max="50"
                               :step="0.5" /> 
              <span class="muted">%，签订日期相距 ≤</span>
              <el-input-number v-model="s.bundle.date_window_days" size="small" :min="0" :max="365" />
              <span class="muted">天才给建议</span>
            </div>
            <div class="hint">必备单据清单在<el-button link type="primary" size="small" @click="router.push('/rules')">规则中心 → 归集与必备单据</el-button>里维护。</div>
          </el-card>
          <el-card class="sub" shadow="never">
            <template #header>
              盖章检测 <el-switch v-model="s.seal_detect_enabled" style="margin-left: 8px" />
            </template>
            <div class="hint">本地 HSV 算法识别红色印章，不上传、不联网。</div>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane label="AI 与 OCR" name="ai">
        <div class="split split-2">
          <el-form label-position="top">
            <el-form-item label="启用 AI 辅助"><el-switch v-model="s.ai.enabled" /></el-form-item>
            <el-form-item label="Base URL（OpenAI 兼容）"><el-input v-model="s.ai.base_url" placeholder="https://api.example.com/v1" /></el-form-item>
            <el-form-item label="模型"><el-input v-model="s.ai.model" placeholder="gpt-4o-mini" /></el-form-item>
            <el-form-item label="API Key">
              <el-input v-model="s.ai.api_key_input" type="password" show-password
                        :placeholder="s.ai.api_key_set ? '已保存（输入新值覆盖）' : '留空=纯规则模式'" />
            </el-form-item>
            <el-form-item label="超时（秒）"><el-input-number v-model="s.ai.timeout" :min="5" :max="300" /></el-form-item>
            <el-button :loading="aiTesting" @click="testAi">测试连接</el-button>
          </el-form>
          <el-form label-position="top">
            <el-form-item label="启用 OCR（扫描件/图片）"><el-switch v-model="s.ocr_enabled" /></el-form-item>
            <el-form-item label="单文件最多识别页数">
              <el-input-number v-model="s.ocr_max_pages" :min="1" :max="200" />
              <div class="hint">RapidOCR 本地 ONNX 推理，模型随应用打包，首次使用加载。</div>
            </el-form-item>
            <el-alert :closable="false" type="info" title="OCR 关闭时，纯文本 PDF 与已入库样例仍可正常自动归档。" />
          </el-form>
        </div>
      </el-tab-pane>

      <el-tab-pane label="安全与网络" name="security">
        <el-form label-position="top">
          <el-form-item label="监听地址">
            <el-radio-group v-model="s.host">
              <el-radio-button value="127.0.0.1">仅本机</el-radio-button>
              <el-radio-button value="0.0.0.0">局域网</el-radio-button>
            </el-radio-group>
            <div class="hint">桌面版默认仅本机；改局域网需重启应用，并且必须设置访问密码。</div>
          </el-form-item>
          <el-form-item label="访问密码">
            <el-input v-model="newPwd" style="max-width: 320px" type="password" show-password
                      :placeholder="s.access_password_set ? '已设置（留空不修改）' : '建议局域网模式设置'" />
          </el-form-item>
        </el-form>
        <el-alert :closable="false" type="success"
                  title="本版安全默认：SPA 静态托管带路径守卫；备份恢复防 zip-slip；会话令牌随机可过期；登录限速 8 次/分钟；预览只允许受管目录内的文件。" />
      </el-tab-pane>

      <el-tab-pane label="系统自检" name="check">
        <SelfCheckPanel />
      </el-tab-pane>

      <el-tab-pane label="备份与维护" name="backup">
        <div class="row" style="margin-bottom: 10px">
          <el-button type="primary" plain @click="createBackup">立即备份数据库</el-button>
          <span class="muted hint">
            自动备份：{{ s.auto_backup.enabled ? `开启（保留 ${s.auto_backup.keep} 份）` : '关闭' }}
          </span>
        </div>
        <el-table :data="backups" size="small" max-height="320">
          <el-table-column prop="filename" label="备份文件" min-width="220" />
          <el-table-column label="时间" width="170" class-name="col-lg" label-class-name="col-lg">
            <template #default="{ row }"><span class="mono muted">{{ fmtTime(row.created_at) }}</span></template>
          </el-table-column>
          <el-table-column label="大小" width="110" class-name="col-md" label-class-name="col-md">
            <template #default="{ row }">{{ (row.size / 1024 / 1024).toFixed(2) }} MB</template>
          </el-table-column>
          <el-table-column label="操作" width="200" fixed="right">
            <template #default="{ row }">
              <el-button size="small" text @click="restore(row)">恢复</el-button>
              <el-button size="small" text @click="downloadBackup(row)">下载</el-button>
              <el-button size="small" text type="danger" @click="removeBackup(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="关于" name="about">
        <el-descriptions v-if="info" :column="2" border size="small">
          <el-descriptions-item label="版本">{{ info.version }}</el-descriptions-item>
          <el-descriptions-item label="运行形态">{{ info.frozen ? '桌面版 DocFlow.exe' : '源码运行' }}</el-descriptions-item>
          <el-descriptions-item label="系统">{{ info.os }}</el-descriptions-item>
          <el-descriptions-item label="Python">{{ info.python }}</el-descriptions-item>
          <el-descriptions-item label="数据目录" :span="2"><span class="mono">{{ info.data_dir }}</span></el-descriptions-item>
          <el-descriptions-item label="资源目录" :span="2"><span class="mono">{{ info.resource_dir }}</span></el-descriptions-item>
          <el-descriptions-item label="配置文件" :span="2"><span class="mono">{{ info.env_file }}</span></el-descriptions-item>
          <el-descriptions-item label="界面资源">{{ info.frontend_bundled ? '已内置' : '缺失' }}</el-descriptions-item>
          <el-descriptions-item label="磁盘剩余">{{ (info.data_free_mb / 1024).toFixed(1) }} GB</el-descriptions-item>
        </el-descriptions>
        <div class="hint" style="margin-top: 10px">
          DocFlow 完全本地运行：文档不出机器；AI 仅在你填写 Base URL 时才会调用外部接口。
        </div>
      </el-tab-pane>
    </el-tabs>

    <div class="save-bar">
      <el-button size="large" type="primary" :loading="saving" @click="save">保存设置</el-button>
    </div>
  </div>
</template>

<style scoped>
.row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.hint { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.6; }
.sub { margin-bottom: 10px; }
.settings { padding-bottom: 30px; }
.save-bar { position: sticky; bottom: 0; display: flex; justify-content: flex-end; gap: 10px;
  padding: 10px 0 14px; background: linear-gradient(transparent, var(--df-bg) 40%); }
.split-2 { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
@media (max-width: 900px) { .split-2 { grid-template-columns: minmax(0, 1fr); } }
</style>