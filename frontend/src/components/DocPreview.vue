<script setup lang="ts">
import { Loading } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import http, { authHeaders, desktop, mediaUrl } from '../api/client'
import { fmtTime } from '../utils/format'

/**
 * 统一预览组件：文档库与人工审核共用。
 * - PDF：pdf.js 逐页渲染 + 可选中文本层（离线资源在 /pdfjs/，桌面版不联网）
 * - 降级：pdf.js 不可用时自动改用服务端 PyMuPDF 位图（/api/preview/{id}/page/N）
 * - 图片：缩放/旋转；表格：csv/xlsx 结构化单元格；文本：分片加载 + 命中高亮
 */
const props = withDefaults(defineProps<{ docId?: number | null; pane?: boolean; height?: string; showName?: boolean }>(), {
  docId: null, pane: false, height: '100%', showName: true,
})

const PDF_BASE = new URL('pdfjs/', document.baseURI).href
let pdfjsLib: any = null
let workerUrl: string | null = null

async function ensurePdfjs() {
  if (pdfjsLib) return pdfjsLib
  const mod: any = await import('pdfjs-dist')
  const worker: any = await import('pdfjs-dist/build/pdf.worker.min.mjs?url')
  workerUrl = worker.default
  mod.GlobalWorkerOptions.workerSrc = workerUrl
  pdfjsLib = mod
  return mod
}

const meta = ref<any>(null)
const loading = ref(false)
const error = ref('')
const tab = ref<'view' | 'text' | 'info'>('view')
const page = ref(1)
const pages = ref(0)
const zoom = ref<'fit' | number>('fit')
const rotation = ref(0)
const rendering = ref(false)
const pageInput = ref(1)
const fallbackMode = ref(false)
const fallbackUrl = ref('')
const imageUrl = ref('')
const query = ref('')
const hitCount = ref(0)
const activeHit = ref(0)
const textBody = ref('')
const textTotal = ref(0)
const textHasMore = ref(false)
const docInfo = ref<any>(null)

const stage = ref<HTMLElement>()
const canvas = ref<HTMLCanvasElement>()
const textLayerEl = ref<HTMLElement>()
const pdfDoc = shallowRef<any>(null)
const pdfPage = shallowRef<any>(null)
let renderTask: any = null

const kind = computed(() => meta.value?.kind || '')
const canPaging = computed(() => kind.value === 'pdf' || kind.value === 'image')
const zoomLabel = computed(() => (zoom.value === 'fit' ? '适应宽度' : `${Math.round((zoom.value as number) * 100)}%`))
const imgStyle = computed(() => ({
  transform: `rotate(${rotation.value}deg) scale(${zoom.value === 'fit' ? 1 : zoom.value})`,
  transition: 'transform .12s ease',
}))

/** 表格预览转成 el-table 需要的对象行（列名做 key）。 */
const tableRows = computed(() => {
  const cols: string[] = meta.value?.table?.columns || []
  const rows: any[][] = meta.value?.table?.rows || []
  return rows.map((r) => {
    const o: Record<string, string> = {}
    cols.forEach((c, i) => { o[c] = r[i] ?? '' })
    return o
  })
})

function reset() {
  renderTask?.cancel?.()
  renderTask = null
  pdfDoc.value?.destroy?.()
  pdfDoc.value = null
  pdfPage.value = null
  meta.value = null
  error.value = ''
  fallbackUrl.value = ''
  imageUrl.value = ''
  textBody.value = ''
  docInfo.value = null
  page.value = 1
  pageInput.value = 1
  rotation.value = 0
  zoom.value = 'fit'
  fallbackMode.value = false
  tab.value = 'view'
}

async function load() {
  reset()
  if (!props.docId) return
  loading.value = true
  try {
    meta.value = await http.get(`/preview/${props.docId}`)
    pages.value = meta.value.pages || 0
    error.value = meta.value.reason || ''
    if (error.value) return
    if (kind.value === 'pdf') await openPdf()
    else if (kind.value === 'image') {
      imageUrl.value = await mediaUrl(meta.value.file_url)
      pages.value = 1
    }
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '预览失败'
  } finally {
    loading.value = false
  }
}

async function openPdf() {
  if (fallbackMode.value) { await loadFallbackPage(); return }
  try {
    const lib = await ensurePdfjs()
    const task = lib.getDocument({
      url: meta.value.file_url,
      httpHeaders: authHeaders(),
      cMapUrl: `${PDF_BASE}cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${PDF_BASE}standard_fonts/`,
      disableAutoFetch: true,
      isEvalSupported: false,
    })
    pdfDoc.value = await task.promise
    pages.value = pdfDoc.value.numPages
    if (page.value > pages.value) page.value = pages.value
    await renderPdf()
  } catch (e: any) {
    console.warn('pdf.js 不可用，改用服务端渲染：', e?.message || e)
    fallbackMode.value = true
    await loadFallbackPage()
  }
}

async function loadFallbackPage() {
  if (!meta.value) return
  const base = (stage.value?.clientWidth || 900) * (zoom.value === 'fit' ? 1.4 : (zoom.value as number) * 1.4)
  const width = Math.max(500, Math.min(2200, Math.round(base)))
  fallbackUrl.value = await mediaUrl(`${meta.value.page_url}${page.value}?width=${width}`)
}

async function renderPdf() {
  if (!pdfDoc.value || tab.value !== 'view') return
  rendering.value = true
  try {
    const p = await pdfDoc.value.getPage(page.value)
    if (pdfPage.value && pdfPage.value !== p) pdfPage.value.cleanup?.()
    pdfPage.value = p
    const rot = ((p.rotate || 0) + rotation.value) % 360
    const base = p.getViewport({ scale: 1, rotation: rot })
    const avail = Math.max(240, (stage.value?.clientWidth || 800) - 34)
    const scale = zoom.value === 'fit' ? Math.min(avail / base.width, 3) : zoom.value as number
    const vp = p.getViewport({ scale, rotation: rot })
    await nextTick()
    const c = canvas.value
    if (!c) return
    const ctx = c.getContext('2d')
    if (!ctx) return
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    c.width = Math.floor(vp.width * dpr)
    c.height = Math.floor(vp.height * dpr)
    c.style.width = `${Math.round(vp.width)}px`
    c.style.height = `${Math.round(vp.height)}px`
    renderTask?.cancel?.()
    renderTask = p.render({ canvasContext: ctx, viewport: vp, transform: [dpr, 0, 0, dpr, 0, 0] })
    await renderTask.promise
    await renderTextLayer(p, vp)
  } catch (e: any) {
    if (e?.name !== 'RenderingCancelledException') {
      console.warn('页面渲染失败，改用服务端渲染：', e?.message || e)
      fallbackMode.value = true
      await loadFallbackPage()
    }
  } finally {
    rendering.value = false
  }
}

async function renderTextLayer(p: any, vp: any) {
  const el = textLayerEl.value
  if (!el || !pdfjsLib?.TextLayer) return
  el.innerHTML = ''
  el.style.width = `${Math.round(vp.width)}px`
  el.style.height = `${Math.round(vp.height)}px`
  try {
    const layer = new pdfjsLib.TextLayer({
      textContentSource: p.streamTextContent(), container: el, viewport: vp,
    })
    await layer.render()
  } catch { /* 文本层是增强项，失败不影响看图 */ }
}

async function applyPage() {
  if (!meta.value) return
  if (fallbackMode.value) await loadFallbackPage()
  else if (kind.value === 'pdf') await renderPdf()
}

async function goPage(n: number) {
  if (!pages.value) return
  page.value = Math.min(pages.value, Math.max(1, Math.round(n)))
  pageInput.value = page.value
  await applyPage()
}

async function step(delta: number) { await goPage(page.value + delta) }

function effectiveScale(): number {
  if (zoom.value !== 'fit') return zoom.value as number
  const w = Math.max(240, (stage.value?.clientWidth || 800) - 34)
  const base = pdfPage.value?.getViewport?.({ scale: 1, rotation: rotation.value % 360 })
  return base ? Math.min(w / base.width, 3) : 1
}

async function setZoom(z: number | 'fit') {
  zoom.value = z
  if (kind.value === 'pdf' && !fallbackMode.value) await renderPdf()
  else await loadFallbackPage()
}

async function zoomBy(factor: number) {
  const cur = effectiveScale()
  await setZoom(Math.max(0.25, Math.min(6, cur * factor)))
}

async function rotate() {
  rotation.value = (rotation.value + 90) % 360
  await applyPage()
}

async function loadText(append = false) {
  if (!props.docId) return
  const start = append ? textBody.value.length : 0
  const r: any = await http.get(`/preview/${props.docId}/text`, { params: { start, length: 30000 } })
  textBody.value = append ? textBody.value + r.body : r.body
  textTotal.value = r.total_length
  textHasMore.value = r.has_more
}

async function openTab(next: 'view' | 'text' | 'info') {
  tab.value = next
  if (next === 'text' && !textBody.value) await loadText()
  if (next === 'info' && !docInfo.value && props.docId) docInfo.value = await http.get(`/documents/${props.docId}`)
  if (next === 'view') await applyPage()
}

async function download() {
  if (!meta.value) return
  const href = await mediaUrl(`${meta.value.file_url}?download=1`)
  const a = document.createElement('a')
  a.href = href
  a.download = meta.value.name || 'document'
  a.click()
}

async function reveal() {
  if (!props.docId) return
  if (!desktop.available) { ElMessage.info('浏览器模式不支持调用资源管理器，请用桌面版 DocFlow.exe'); return }
  const ok = await desktop.revealDocument(props.docId)
  if (ok) ElMessage.success('已在资源管理器中定位')
}

function openNewTab() {
  if (meta.value) window.open(meta.value.file_url, '_blank')
}

const segments = computed(() => {
  const body = textBody.value || ''
  const q = query.value.trim()
  if (!q) return [{ text: body, hit: false }]
  const out: { text: string; hit: boolean }[] = []
  let i = 0
  const lower = body.toLowerCase()
  const needle = q.toLowerCase()
  let n = 0
  while (true) {
    const at = lower.indexOf(needle, i)
    if (at < 0) { out.push({ text: body.slice(i), hit: false }); break }
    if (at > i) out.push({ text: body.slice(i, at), hit: false })
    out.push({ text: body.slice(at, at + q.length), hit: true })
    i = at + q.length
    n += 1
    if (n > 5000) break
  }
  hitCount.value = n
  return out
})

async function jumpHit(delta: number) {
  if (!hitCount.value) return
  activeHit.value = ((activeHit.value + delta - 1 + hitCount.value) % hitCount.value) + 1
  await nextTick()
  document.querySelectorAll('.pv-text mark')[activeHit.value - 1]?.scrollIntoView({ block: 'center' })
}

watch(() => props.docId, load, { immediate: true })
onBeforeUnmount(() => { renderTask?.cancel?.(); pdfDoc.value?.destroy?.() })

defineExpose({ goPage, step, zoomBy, rotate, setZoom, load, tab, reload: load })
</script>

<template>
  <div class="pv" :class="{ pane }" :style="{ height }">
    <div class="pv-bar">
      <div class="pv-title">
        <b v-if="showName">{{ meta?.name || '预览' }}</b>
        <el-tag v-if="meta" size="small" effect="plain">{{ meta.file_type || '未知' }}</el-tag>
        <el-tag v-if="meta?.document_type" size="small" type="info" effect="plain">{{ meta.document_type }}</el-tag>
        <span v-if="pages > 1" class="muted mono">{{ pages }} 页</span>
        <span v-if="meta?.size" class="muted mono">{{ (meta.size / 1024).toFixed(1) }} KB</span>
      </div>
      <div class="pv-tools">
        <el-button-group v-if="canPaging && pages > 1">
          <el-button size="small" :disabled="page <= 1" @click="step(-1)">◀</el-button>
          <el-input v-model.number="pageInput" size="small" class="page-box mono" label="页码"
                    @keyup.enter="goPage(pageInput)">
            <template #suffix><span class="muted mono">/{{ pages }}</span></template>
          </el-input>
          <el-button size="small" :disabled="page >= pages" @click="step(1)">▶</el-button>
        </el-button-group>
        <template v-if="kind === 'pdf' || kind === 'image'">
          <el-button size="small" @click="zoomBy(1 / 1.2)">－</el-button>
          <el-button size="small" class="zoom-box" @click="setZoom('fit')">{{ zoomLabel }}</el-button>
          <el-button size="small" @click="zoomBy(1.2)">＋</el-button>
          <el-button size="small" @click="rotate()">旋转</el-button>
        </template>
        <el-radio-group v-model="tab" size="small" @change="(v: any) => openTab(v)">
          <el-radio-button value="view">看原件</el-radio-button>
          <el-radio-button value="text">正文</el-radio-button>
          <el-radio-button value="info">信息</el-radio-button>
        </el-radio-group>
        <el-button size="small" @click="download">下载</el-button>
        <el-button v-if="desktop.available" size="small" @click="reveal">资源管理器</el-button>
        <el-button v-if="!pane" size="small" text @click="openNewTab">新窗口</el-button>
      </div>
    </div>

    <div v-loading="loading" class="pv-body">
      <el-alert v-if="error" :closable="false" type="warning" :title="error" class="pv-alert" />

      <template v-if="!error">
        <div v-show="tab === 'view'" ref="stage" class="pv-stage">
          <div v-if="kind === 'pdf' && !fallbackMode" class="pdf-sheet">
            <canvas ref="canvas" class="pdf-canvas" />
            <div ref="textLayerEl" class="text-layer" />
          </div>
          <img v-else-if="kind === 'pdf' && fallbackMode" :src="fallbackUrl" class="pv-img" alt="PDF 页面" />
          <img v-else-if="kind === 'image'" :src="imageUrl" class="pv-img" :style="imgStyle" alt="图片" />
          <div v-else-if="kind === 'table' && meta?.table" class="pv-table">
            <div v-if="meta.table.sheets?.length" class="muted hint">工作表：{{ meta.table.sheets.join(' · ') }}</div>
            <el-table v-if="meta.table.columns?.length" :data="tableRows" size="small" height="100%" border>
              <el-table-column v-for="c in meta.table.columns" :key="c" :prop="c" :label="c" min-width="120" show-overflow-tooltip />
            </el-table>
            <pre v-else-if="meta.table.text_body" class="pv-text">{{ meta.table.text_body }}</pre>
            <el-empty v-else description="表格为空" :image-size="70" />
            <div v-if="meta.table.truncated" class="muted hint">仅显示前 {{ meta.table.rows.length }} 行</div>
          </div>
          <pre v-else-if="tab === 'view' && kind === 'text' && meta?.text" class="pv-text">{{ meta.text.body }}</pre>
          <el-empty v-else-if="kind === 'binary'" description="该类型没有可视化预览，可下载原件" :image-size="90">
            <el-button type="primary" @click="download">下载原件</el-button>
          </el-empty>
          <div v-if="rendering" class="pv-spin"><el-icon class="is-loading"><Loading /></el-icon></div>
        </div>

        <div v-show="tab === 'text'" class="pv-pane">
          <div class="toolbar" style="margin-bottom: 8px">
            <el-input v-model="query" size="small" placeholder="在正文中查找" clearable style="max-width: 240px"
                      @keyup.enter="jumpHit(1)" />
            <span class="muted mono">{{ hitCount ? `${activeHit || 1}/${hitCount}` : '0 处' }}</span>
            <el-button size="small" :disabled="!hitCount" @click="jumpHit(-1)">上一个</el-button>
            <el-button size="small" :disabled="!hitCount" @click="jumpHit(1)">下一个</el-button>
            <div class="grow" />
            <el-button v-if="textHasMore" size="small" @click="loadText(true)">再读 3 万字</el-button>
            <span class="muted hint">已加载 {{ textBody.length }} / {{ textTotal }} 字</span>
          </div>
          <pre class="pv-text"><span v-for="(s, i) in segments" :key="i"><mark v-if="s.hit" class="df-hit">{{ s.text }}</mark><template v-else>{{ s.text }}</template></span></pre>
        </div>

        <div v-show="tab === 'info'" class="pv-pane">
          <el-descriptions v-if="docInfo" :column="2" size="small" border>
            <el-descriptions-item label="文件名">{{ docInfo.current_filename }}</el-descriptions-item>
            <el-descriptions-item label="原始名">{{ docInfo.original_filename }}</el-descriptions-item>
            <el-descriptions-item label="状态">{{ docInfo.status }}</el-descriptions-item>
            <el-descriptions-item label="类型">{{ docInfo.document_type || '—' }}</el-descriptions-item>
            <el-descriptions-item label="置信度">{{ docInfo.confidence }}</el-descriptions-item>
            <el-descriptions-item label="盖章">{{ docInfo.has_seal === true ? '有' : docInfo.has_seal === false ? '无' : '未检测' }}</el-descriptions-item>
            <el-descriptions-item label="大小">{{ ((docInfo.file_size || 0) / 1024).toFixed(1) }} KB</el-descriptions-item>
            <el-descriptions-item label="SHA256">{{ (docInfo.fingerprint || '').slice(0, 24) }}</el-descriptions-item>
            <el-descriptions-item label="入库">{{ fmtTime(docInfo.created_at) }}</el-descriptions-item>
            <el-descriptions-item label="归档">{{ docInfo.archived_at ? fmtTime(docInfo.archived_at) : '—' }}</el-descriptions-item>
            <el-descriptions-item label="路径" :span="2"><span class="mono">{{ docInfo.path }}</span></el-descriptions-item>
          </el-descriptions>
          <h4>识别字段</h4>
          <el-table v-if="docInfo?.fields?.length" :data="docInfo.fields" size="small" max-height="240">
            <el-table-column prop="name" label="字段" width="140" />
            <el-table-column prop="value" label="值" show-overflow-tooltip />
            <el-table-column prop="source" label="来源" width="90" />
            <el-table-column prop="confidence" label="可信度" width="90" />
          </el-table>
          <div v-else class="muted hint">没有识别到结构化字段</div>
        </div>
      </template>
    </div>

    <div class="pv-foot">
      <span class="muted hint">
        快捷键：← → 翻页 · + - 缩放 · R 旋转 · Esc 关闭
        <template v-if="fallbackMode"> · 已启用服务端渲染（pdf.js 不可用时的自动降级）</template>
      </span>
    </div>
  </div>
</template>

<style scoped>
.pv { display: flex; flex-direction: column; min-height: 0; background: var(--df-surface); }
.pv-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 8px 12px;
  border-bottom: 1px solid var(--df-line); }
.pv-title { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1 1 260px; }
.pv-title b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 42vw; }
.pv-tools { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.page-box { width: 96px; }
.zoom-box { min-width: 66px; }
.pv-body { flex: 1 1 auto; min-height: 0; overflow: auto; position: relative; }
.pv-alert { margin: 12px; }
.pv-stage { min-height: 100%; padding: 16px; display: flex; flex-direction: column; align-items: center; gap: 12px;
  background: var(--df-well); }
.pdf-sheet { position: relative; box-shadow: 0 2px 14px rgba(0, 0, 0, .16); background: #fff; }
.pdf-canvas { display: block; }
.text-layer { position: absolute; inset: 0; overflow: hidden; line-height: 1; opacity: .95; }
.text-layer :deep(span) { position: absolute; white-space: pre; color: transparent; cursor: text; }
.text-layer :deep(::selection) { background: rgba(34, 211, 238, .45); }
.pv-img { max-width: 100%; object-fit: contain; background: #fff; box-shadow: 0 2px 14px rgba(0, 0, 0, .12); }
.pv-table { width: 100%; height: 100%; display: flex; flex-direction: column; gap: 8px; padding: 10px; }
.pv-table :deep(.el-table) { flex: 1 1 auto; }
.pv-pane { padding: 12px 14px; height: 100%; display: flex; flex-direction: column; min-height: 0; }
.pv-text { flex: 1 1 auto; overflow: auto; margin: 0; white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 12.5px; line-height: 1.75;
  background: var(--df-surface); padding: 10px 12px; border-radius: 8px; border: 1px solid var(--df-line); }
.pv-foot { padding: 6px 12px; border-top: 1px solid var(--df-line); }
.pv-spin { position: absolute; right: 18px; top: 18px; color: var(--el-color-primary); }
.pv.pane { border: 1px solid var(--df-line); border-radius: var(--df-radius); overflow: hidden; }
</style>