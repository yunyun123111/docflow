<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onActivated, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { UploadFilled } from '@element-plus/icons-vue'
import http from '../api/client'
import DocThumb from '../components/DocThumb.vue'
import PreviewDialog from '../components/PreviewDialog.vue'

const router = useRouter()
const uploading = ref(false)
const progress = ref(0)
const dragging = ref(false)
const dirSource = ref('')
const recursive = ref(true)
const job = ref<any>(null)
const results = ref<any[]>([])
const inbox = ref<any>({})
const components = ref<any>({})
const previewOpen = ref(false)
const previewId = ref<number | null>(null)
let timer: number | undefined
let compTimer: number | undefined

const STATUS: Record<string, [string, string]> = {
  pending: ['待整理', 'info'], processing: ['识别中', 'primary'], archived: ['已归档', 'success'],
  need_review: ['待审核', 'warning'], failed: ['失败', 'danger'], duplicate: ['重复', 'info'],
  recycled: ['回收站', 'info'],
}
const ids = computed(() => results.value.map((r) => r.id))
const percent = computed(() => (job.value?.total ? Math.round((job.value.processed / job.value.total) * 100) : 0))

async function refreshInbox() { inbox.value = await http.get('/processing/inbox/check') }

async function pollJob() {
  const cur: any = await http.get('/processing/jobs/current')
  const wasRunning = job.value?.status === 'running'
  job.value = cur
  if (cur) {
    results.value = await http.get(`/processing/jobs/${cur.id}/documents`)
    return
  }
  if (wasRunning) ElMessage.success('整理任务已完成')
  if (!results.value.length) {
    const last: any = await http.get('/processing/jobs', { params: { limit: 1 } })
    if (last?.length) results.value = await http.get(`/processing/jobs/${last[0].id}/documents`)
  }
}

async function start() {
  try {
    const r: any = await http.post('/processing/jobs', { source_dir: dirSource.value || null, recursive: recursive.value })
    ElMessage.success(`任务 #${r.id} 已启动（${r.total} 个文件）`)
    job.value = r
    await refreshInbox()
    await pollJob()
  } catch { /* 拦截器已提示 */ }
}

async function cancel() {
  if (!job.value) return
  await http.post(`/processing/jobs/${job.value.id}/cancel`)
  job.value = null
}

async function retryFailed() {
  const r: any = await http.post('/processing/retry-failed')
  ElMessage.success(`重试任务 #${r.id} 启动`)
  job.value = r
}

async function onDrop(ev: DragEvent) {
  dragging.value = false
  ev.preventDefault()
  const files = (ev as any).dataTransfer?.files
  if (files?.length) await doUpload(Array.from(files))
}

async function pickFiles(e: Event) {
  const files = (e.target as HTMLInputElement).files
  if (files?.length) await doUpload(Array.from(files))
}

async function doUpload(list: File[]) {
  uploading.value = true
  progress.value = 0
  const fd = new FormData()
  list.forEach((f) => fd.append('files', f))
  try {
    const r: any = await http.post('/documents/upload', fd, {
      onUploadProgress: (e) => { progress.value = Math.round((e.loaded / (e.total || 1)) * 100) },
    })
    if (r.rejected?.length) ElMessage.warning(`${r.saved.length} 个入库；${r.rejected.length} 个被拒（类型不支持或超限）`)
    else ElMessage.success(`${r.saved.length} 个文件已入库并开始识别`)
    job.value = r.job
    await refreshInbox()
    await pollJob()
  } finally {
    uploading.value = false
  }
}

async function refreshComponents() { components.value = await http.get('/system/components') }

function openPreview(row: any) {
  previewId.value = row.id
  previewOpen.value = true
}

async function toReview() {
  const n = job.value?.need_review ?? results.value.filter((r) => r.status === 'need_review').length
  if (!n) { ElMessage.info('本次没有待审核文档'); return }
  await router.push('/review')
}

onMounted(() => {
  refreshInbox(); pollJob(); refreshComponents()
  timer = window.setInterval(() => { pollJob(); refreshInbox() }, 2500)
  compTimer = window.setInterval(refreshComponents, 15000)
})
onActivated(() => { pollJob(); refreshInbox() })
onBeforeUnmount(() => {
  window.clearInterval(timer)
  window.clearInterval(compTimer)
})
</script>

<template>
  <div class="org">
    <div class="split org-grid">
      <el-card class="page-card" header="拖入文件即整理">
        <div class="drop" :class="{ over: dragging }" @dragover.prevent="dragging = true"
             @dragleave.prevent="dragging = false" @drop="onDrop">
          <div class="drop-chip"><el-icon :size="25"><UploadFilled /></el-icon></div>
          <div class="drop-title">把文件拖到这里</div>
          <div class="drop-sub">支持多选，PDF、Word、Excel、图片都能识</div>
          <label class="link"><input type="file" multiple hidden @change="pickFiles" />也可以点击选择文件</label>
          <el-progress v-if="uploading" :percentage="progress" style="width: 70%; margin: 10px auto 0" />
        </div>
        <div class="inbox-line muted">
          待整理区：{{ inbox.count ?? 0 }} 个文件 · {{ ((inbox.bytes || 0) / 1048576).toFixed(1) }} MB
        </div>
      </el-card>

      <div>
        <el-card class="page-card" header="按目录整理">
          <el-input v-model="dirSource" placeholder="留空 = 待整理区；也可填绝对路径，如 D:\待整理" clearable />
          <div class="row" style="margin: 10px 0">
            <el-switch v-model="recursive" active-text="递归子目录" />
            <el-button type="primary" :disabled="!!job" @click="start">开始整理</el-button>
            <el-button @click="refreshInbox">刷新状态</el-button>
            <el-button size="small" @click="retryFailed">重试失败</el-button>
          </div>
          <el-alert v-if="job" :closable="false" type="primary">
            <div class="row-between">
              <span>任务 #{{ job.id }}（{{ job.kind }}）· {{ job.status }}</span>
              <el-button size="small" text @click="cancel">取消</el-button>
            </div>
            <el-progress :percentage="percent" :indeterminate="job.status === 'running' && !job.total" />
            <div class="row job-stat">
              <el-tag size="small" type="success">归档 {{ job.archived }}</el-tag>
              <el-tag size="small" type="warning">待审 {{ job.need_review }}</el-tag>
              <el-tag size="small" type="info">重复 {{ job.duplicate }}</el-tag>
              <el-tag size="small" type="danger">失败 {{ job.failed }}</el-tag>
              <div class="grow" />
              <el-button link type="primary" size="small" @click="toReview">去审核 →</el-button>
            </div>
          </el-alert>
          <div v-else class="muted hint">
            任务空闲。监控目录/邮箱收件会在后台自动整理，这里也可以手动指定一个目录。
          </div>
        </el-card>

        <el-card class="page-card" header="后台组件">
          <div class="comp-grid">
            <span>目录监控<el-tag size="small" :type="components.watcher?.running ? 'success' : 'info'">{{ components.watcher?.running ? `${components.watcher?.dirs?.length || 0} 个目录` : '未启用' }}</el-tag></span>
            <span>邮箱接收<el-tag size="small" :type="components.email?.running ? 'success' : 'info'">{{ components.email?.last_new ?? 0 }} 新附件</el-tag></span>
            <span>对账巡检<el-tag size="small" :type="components.recon?.running ? 'success' : 'info'">{{ components.recon?.running ? '运行中' : '关闭' }}</el-tag></span>
            <span>识别线程<el-tag size="small" :type="components.pipeline_running ? 'warning' : 'info'">{{ components.pipeline_running ? '忙碌' : '空闲' }}</el-tag></span>
          </div>
        </el-card>
      </div>
    </div>

    <el-card v-if="results.length" class="page-card" header="本次整理结果（点一下即可预览原件）">
      <div class="res-grid">
        <div v-for="r in results" :key="r.id" class="res" @click="openPreview(r)">
          <DocThumb :doc-id="r.id" :file-type="r.file_type" :size="64" :exists="r.file_exists !== false"
                  class="res-thumb" />
          <div class="res-main">
            <div class="res-name">{{ r.current_filename }}</div>
            <div class="res-meta">
              <el-tag size="small" :type="STATUS[r.status]?.[1] as any">{{ STATUS[r.status]?.[0] || r.status }}</el-tag>
              <span class="muted">{{ r.document_type || '—' }}</span>
              <span class="mono muted">{{ r.confidence != null ? Math.round(r.confidence * 100) + '%' : '' }}</span>
            </div>
            <div v-if="r.error" class="err">{{ r.error }}</div>
          </div>
        </div>
      </div>
    </el-card>

    <PreviewDialog v-model="previewOpen" v-model:doc-id="previewId" :ids="ids"
                   :title="results.find((r) => r.id === previewId)?.current_filename || ''" />
  </div>
</template>

<style scoped>
.org-grid { grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); }
.drop { border: 1.5px dashed var(--df-line); border-radius: 18px; padding: 28px 18px; text-align: center;
  color: var(--el-text-color-secondary); background: var(--df-surface-2); transition: .18s; cursor: pointer; }
.drop.over, .drop:hover { border-color: var(--df-green); background: var(--df-tint); }
.drop-chip { width: 56px; height: 56px; margin: 0 auto 10px; border-radius: 50%; display: grid; place-items: center;
  background: var(--df-surface); color: var(--df-green); box-shadow: var(--df-shadow-sm); }
.drop.over .drop-chip, .drop:hover .drop-chip { background: var(--df-green); color: #fff; }
.drop-title { font-size: 14.5px; font-weight: 650; color: var(--df-ink); }
.drop-sub { font-size: 12.5px; margin-top: 3px; }
.link { color: var(--el-color-primary); cursor: pointer; display: inline-block; margin-top: 6px; font-size: 13px; }
.inbox-line { margin-top: 8px; font-size: 12.5px; }
.row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.row-between { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.job-stat { margin-top: 8px; }
.comp-grid { display: flex; gap: 16px; flex-wrap: wrap; font-size: 13px; }
.comp-grid span { display: flex; align-items: center; gap: 6px; }
.res-grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }
.res { display: flex; gap: 10px; align-items: center; padding: 10px; border-radius: 16px; cursor: pointer;
  background: var(--df-surface); box-shadow: var(--df-shadow-sm); transition: box-shadow .15s, transform .15s; }
.res:hover { box-shadow: var(--df-shadow); transform: translateY(-2px); }
.res-thumb { flex: 0 0 64px; }
.res-main { min-width: 0; }
.res-name { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.res-meta { display: flex; gap: 6px; align-items: center; font-size: 12px; margin-top: 3px; flex-wrap: wrap; }
.err { color: var(--el-color-danger); font-size: 12px; margin-top: 2px; }
@media (max-width: 1080px) { .org-grid { grid-template-columns: minmax(0, 1fr); } }
</style>