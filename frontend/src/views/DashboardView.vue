<script setup lang="ts">
import { Collection, Select, Tickets, TrendCharts } from '@element-plus/icons-vue'
import * as echarts from 'echarts'
import { computed, onActivated, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useRouter } from 'vue-router'
import http from '../api/client'
import DocThumb from '../components/DocThumb.vue'
import PreviewDialog from '../components/PreviewDialog.vue'
import { useResponsive } from '../composables/useResponsive'

const router = useRouter()
const { bp } = useResponsive()
const ov = ref<any>({})
const quality = ref<any>({})
const ops = ref<any[]>([])
const recent = ref<any[]>([])
const status = ref<any>(null)
const info = ref<any>(null)
const failed = ref<string[]>([])
const loading = ref(true)
const trendEl = ref<HTMLElement>()
const confEl = ref<HTMLElement>()
const trendChart = shallowRef<echarts.ECharts>()
const confChart = shallowRef<echarts.ECharts>()
const days = ref(30)
const trendData = ref<any[]>([])

const previewOpen = ref(false)
const previewId = ref<number | null>(null)
const ids = computed(() => recent.value.map((r: any) => r.id))

const monthArchived = computed(() => trendData.value.reduce((s, t) => s + (t.archived || 0), 0))
const ratio = computed(() => (quality.value.auto_archive_ratio_30d != null
  ? `${Math.round(quality.value.auto_archive_ratio_30d * 100)}%` : '—'))

const kpis = computed(() => [
  { label: '文档总数', value: ov.value.total ?? '-', sub: `今日新增 ${ov.value.today_new ?? 0}`,
    icon: Collection, tone: '' },
  { label: '已归档', value: ov.value.archived ?? '-', sub: `今日归档 ${ov.value.today_archived ?? 0}`,
    icon: Select, tone: '' },
  { label: '待审核', value: ov.value.need_review ?? '-',
    sub: quality.value.backlog_over_7d ? `积压>7天 ${quality.value.backlog_over_7d}` : '无长期积压',
    icon: Tickets, tone: 'warn' },
  { label: '30 天自动化率', value: ratio.value, sub: `失败 ${quality.value.failed_30d ?? 0}`,
    icon: TrendCharts, tone: 'calm' },
])

const OP_LABEL: Record<string, string> = {
  upload: '入库', archive: '归档', rename: '重命名', move: '移动', delete: '移入回收站',
  restore: '恢复', purge: '彻底删除', review: '人工审核', undo: '撤销整理', fix: '修正',
}
const OP_GLYPH: Record<string, string> = {
  upload: '↑', archive: '✓', rename: '✎', move: '⇄', delete: '⌫',
  restore: '↺', purge: '✕', review: '☑', undo: '↩', fix: '⚙',
}
function opLabel(op: string) { return OP_LABEL[op] || op }
const STATUS_CN: Record<string, string> = {
  pending: '待整理', processing: '识别中', archived: '已归档', need_review: '待审核',
  failed: '失败', duplicate: '重复', recycled: '回收站',
}
function statusCn(s: string) { return STATUS_CN[s] || s }
function ago(iso: string) {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso.slice(5, 16)
  const m = Math.max(0, Math.round((Date.now() - t) / 60000))
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  if (m < 60 * 24) return `${Math.round(m / 60)} 小时前`
  return `${Math.round(m / 1440)} 天前`
}

/**
 * 每个区块独立请求：任一接口失败只影响它自己，不会像串行 await 那样
 * 把整页拖成「全是占位符」。桌面版窗口被系统遮挡时偶发请求中断，这点尤其重要。
 */
async function grab(key: string, url: string, apply: (v: any) => void, config?: any) {
  try {
    apply(await http.get(url, config))
    failed.value = failed.value.filter((f) => f !== key)
  } catch {
    if (!failed.value.includes(key)) failed.value = [...failed.value, key]
  }
}

async function load() {
  await Promise.all([
    grab('overview', '/dashboard/overview', (v) => { ov.value = v }),
    grab('quality', '/dashboard/quality', (v) => { quality.value = v }),
    grab('ops', '/dashboard/recent-operations', (v) => { ops.value = v as any }),
    grab('recent', '/documents', (v) => { recent.value = v.items || [] }, { params: { limit: 6 } }),
    grab('status', '/system/status', (v) => { status.value = v }),
    grab('info', '/system/info', (v) => { info.value = v }),
  ])
  loading.value = false
  await drawTrend()
  drawConf()
}

/** 图表配色跟着主题走，深色下坐标轴才不会「黑字压黑底」。 */
function cssVar(name: string, fallback: string) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}
function chartTheme() {
  return {
    ink: cssVar('--df-ink', '#16211c'),
    muted: cssVar('--df-muted', '#97a0af'),
    line: cssVar('--df-line', '#eceef4'),
    green: cssVar('--df-green', '#3eb75a'),
    greenSoft: '#8fd39b',
  }
}

/** 容器尺寸为 0（窗口尚未显示/被折叠）时 ECharts 画不出东西，等有尺寸再建画布。 */
function ensureCharts() {
  if (!trendChart.value && trendEl.value?.clientWidth) trendChart.value = echarts.init(trendEl.value)
  if (!confChart.value && confEl.value?.clientWidth) confChart.value = echarts.init(confEl.value)
}

async function drawTrend() {
  ensureCharts()
  if (!trendChart.value) return
  const trend: any = await http.get('/dashboard/trend', { params: { days: days.value } }).catch(() => null)
  if (!trend) return
  trendData.value = trend
  const th = chartTheme()
  const axis = {
    axisLine: { lineStyle: { color: th.line } },
    axisLabel: { color: th.muted, fontSize: 11 },
    splitLine: { lineStyle: { color: th.line, type: 'dashed' as const } },
  }
  trendChart.value.setOption({
    color: [th.green, th.greenSoft, '#ef5f5f'],
    tooltip: { trigger: 'axis' },
    legend: { data: ['新增', '归档', '失败'], top: 0, right: 0, icon: 'circle', itemWidth: 8,
      textStyle: { color: th.muted, fontSize: 12 } },
    grid: { left: 40, right: 14, top: 38, bottom: 24 },
    xAxis: { type: 'category', boundaryGap: false, data: trend.map((t: any) => t.date.slice(5)), ...axis },
    yAxis: { type: 'value', minInterval: 1, ...axis },
    series: [
      { name: '新增', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
        lineStyle: { width: 3 }, areaStyle: { opacity: .1 }, data: trend.map((t: any) => t.new) },
      { name: '归档', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
        lineStyle: { width: 3 }, areaStyle: { opacity: .1 }, data: trend.map((t: any) => t.archived) },
      { name: '失败', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
        lineStyle: { width: 2, type: 'dashed' }, data: trend.map((t: any) => t.failed) },
    ],
  }, true)
  trendChart.value.resize()
}

function drawConf() {
  ensureCharts()
  if (!confChart.value) return
  const buckets: number[] = quality.value.confidence_buckets || []
  const th = chartTheme()
  const axis = {
    axisLine: { lineStyle: { color: th.line } },
    axisLabel: { color: th.muted, fontSize: 11 },
    splitLine: { lineStyle: { color: th.line, type: 'dashed' as const } },
  }
  confChart.value.setOption({
    tooltip: {}, grid: { left: 40, right: 14, top: 20, bottom: 24 },
    xAxis: { type: 'category', data: buckets.map((_: any, i: number) => `${(i / 10).toFixed(1)}-${((i + 1) / 10).toFixed(1)}`), ...axis },
    yAxis: { type: 'value', minInterval: 1, ...axis },
    series: [{
      type: 'bar', barMaxWidth: 26, itemStyle: { borderRadius: [6, 6, 0, 0] },
      data: buckets.map((v: number, i: number) => ({
        value: v, itemStyle: { color: i >= 8 ? th.green : i >= 6 ? '#f0a53c' : '#b8c1cd' },
      })),
    }],
  }, true)
  confChart.value.resize()
}

function resize() {
  ensureCharts()
  trendChart.value?.resize()
  confChart.value?.resize()
}

// 窗口从后台切回前台时补一次：桌面版被遮挡期间可能有请求被浏览器掐掉。
let lastResume = 0
function onResume() {
  if (document.visibilityState === 'hidden') return
  const now = Date.now()
  if (now - lastResume < 4000) return
  lastResume = now
  load()
}

function onTheme() { drawTrend(); drawConf() }

let ro: ResizeObserver | null = null

onMounted(() => {
  ensureCharts()
  load()
  window.addEventListener('resize', resize)
  window.addEventListener('focus', onResume)
  window.addEventListener('df-theme', onTheme)
  document.addEventListener('visibilitychange', onResume)
  ro = new ResizeObserver(() => { if (!trendChart.value || !confChart.value) ensureCharts(); resize() })
  if (trendEl.value) ro.observe(trendEl.value)
  if (confEl.value) ro.observe(confEl.value)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  window.removeEventListener('focus', onResume)
  window.removeEventListener('df-theme', onTheme)
  document.removeEventListener('visibilitychange', onResume)
  ro?.disconnect()
  trendChart.value?.dispose()
  confChart.value?.dispose()
  trendChart.value = undefined
  confChart.value = undefined
})
onActivated(() => { load(); resize() })
watch(bp, () => setTimeout(resize, 60))

function openDoc(d: any) {
  previewId.value = d.id
  previewOpen.value = true
}
</script>

<template>
  <div class="dash">
    <el-alert v-if="failed.length" type="warning" :closable="false" class="dash-retry"
              title="部分数据没取到，界面可能显示为占位符">
      <template #default>
        <span class="muted">窗口切回前台会自动重试，也可以手动</span>
        <el-button size="small" type="primary" text @click="load">重新加载</el-button>
      </template>
    </el-alert>

    <div class="split hero-row">
      <div class="hero">
        <div class="hero-kicker">DocFlow · 本地文档流水线</div>
        <h2>把散落的合同、单据和发票，变成能搜的档案库</h2>
        <p>识别、改名、分类、去重全自动；机器没把握的自动进人工审核，台账差异一键对出来。全程离线，数据不出这台机器。</p>
        <div class="hero-btns">
          <el-button type="primary" @click="router.push('/organize')">开始整理</el-button>
          <el-button class="hero-ghost" @click="router.push('/review')">
            处理待办{{ ov.need_review ? ` · ${ov.need_review}` : '' }}
          </el-button>
        </div>
      </div>

      <div class="promo stats-card">
        <div class="promo-kicker">近 30 天</div>
        <div class="promo-title">自动化归档率 {{ ratio }}</div>
        <div class="stat-row">
          <div>
            <div class="stat-lbl">今日新增</div>
            <div class="stat-num">{{ ov.today_new ?? 0 }} 件</div>
          </div>
          <div>
            <div class="stat-lbl">归档</div>
            <div class="stat-num">{{ monthArchived }} 件</div>
          </div>
        </div>
        <div class="stat-foot">
          <span>待审 {{ ov.need_review ?? 0 }} · 待整理 {{ ov.pending ?? 0 }} · 磁盘可用
            {{ ((info?.data_free_mb || 0) / 1024).toFixed(0) }} GB</span>
          <a class="promo-link" @click="router.push('/library')">查看文档库 →</a>
        </div>
      </div>
    </div>

    <div class="kpi-grid">
      <el-card v-for="k in kpis" :key="k.label" class="page-card" :body-style="{ padding: '16px 18px' }">
        <div class="kpi-card">
          <span class="kpi-chip" :class="k.tone"><el-icon :size="19"><component :is="k.icon" /></el-icon></span>
          <div>
            <div class="kpi-num">{{ k.value }}</div>
            <div class="kpi-label">{{ k.label }}</div>
            <div class="hint">{{ k.sub }}</div>
          </div>
        </div>
      </el-card>
    </div>

    <div class="sec-head">
      <span class="sec-title green">最近入库</span>
      <a class="sec-link" @click="router.push('/library')">查看全部 →</a>
    </div>
    <div class="doc-grid">
      <div v-for="d in recent" :key="d.id" class="tile" @click="openDoc(d)">
        <DocThumb :doc-id="d.id" :file-type="d.file_type" :size="124"
                  :exists="d.file_exists !== false" />
        <div class="tile-cap">{{ d.current_filename }}</div>
        <div class="hint tile-meta">{{ statusCn(d.status) }} · {{ ago(d.created_at) }}</div>
        <span class="tile-go">↗</span>
      </div>
      <el-card v-if="!recent.length && !loading" class="page-card empty-tile">
        <el-empty description="还没有入库的文档" :image-size="70" />
      </el-card>
    </div>

    <el-card class="page-card">
      <template #header>
        <div class="row-between">
          <span>近{{ days }}天趋势</span>
          <el-radio-group v-model="days" size="small" @change="drawTrend">
            <el-radio-button :value="7">7 天</el-radio-button>
            <el-radio-button :value="30">30 天</el-radio-button>
            <el-radio-button :value="90">90 天</el-radio-button>
          </el-radio-group>
        </div>
      </template>
      <div ref="trendEl" class="chart chart-lg"></div>
    </el-card>

    <div class="split dash-bottom">
      <el-card class="page-card">
        <template #header>
          <div class="row-between">
            <span>运行状态</span>
            <span class="muted hint">{{ info?.frozen ? '桌面版' : '源码版' }} v{{ info?.version }} · 数据目录
              {{ (info?.data_dir || '').split('\\').pop() }}</span>
          </div>
        </template>
        <div class="health-grid">
          <div class="h-item" :class="status?.level">
            <div class="h-title">配置自检</div>
            <div class="h-value">{{ status?.level === 'error' ? '有异常' : status?.level === 'warn' ? '需注意' : status?.level === 'info' ? '提示' : '正常' }}</div>
            <div class="h-sub muted">{{ status ? `${status.summary?.ok ?? 0} 项通过 · ${status.summary?.warn ?? 0} 注意 · ${status.summary?.error ?? 0} 异常` : '检查中…' }}</div>
            <el-button size="small" @click="router.push('/settings')">去处理</el-button>
          </div>
          <div class="h-item">
            <div class="h-title">待办</div>
            <div class="h-value">{{ ov.need_review ?? 0 }} 件待审</div>
            <div class="h-sub muted">失败 {{ quality.failed_30d ?? 0 }} · 重复 {{ ov.duplicate ?? 0 }}</div>
            <el-button size="small" @click="router.push('/review')">开始审核</el-button>
          </div>
          <div class="h-item">
            <div class="h-title">整理入口</div>
            <div class="h-value">{{ ov.pending ?? 0 }} 件待整理</div>
            <div class="h-sub muted">拖拽/目录/监控/邮箱四种入口</div>
            <el-button size="small" @click="router.push('/organize')">去整理</el-button>
          </div>
          <div class="h-item">
            <div class="h-title">存储</div>
            <div class="h-value">{{ ((info?.data_free_mb || 0) / 1024).toFixed(0) }} GB 可用</div>
            <div class="h-sub muted">归档 {{ ov.archived ?? 0 }} 个文件</div>
            <el-button size="small" @click="router.push('/library')">文档库</el-button>
          </div>
        </div>
        <div v-if="status?.issues?.length" class="issues">
          <div v-for="i in status.issues" :key="i.key" class="issue" :class="i.level">
            <b>{{ i.label }}</b><span>{{ i.message }}</span>
          </div>
        </div>
      </el-card>

      <el-card class="page-card">
        <template #header>
          <div class="row-between">
            <span>最近操作</span>
            <a class="sec-link" @click="router.push('/logs')">全部日志 →</a>
          </div>
        </template>
        <div class="activity">
          <div v-for="(o, idx) in ops.slice(0, 8)" :key="idx" class="act-row">
            <span class="act-avatar" :class="{ plain: !OP_GLYPH[o.op] }">{{ OP_GLYPH[o.op] || '·' }}</span>
            <div class="act-main">
              <div class="act-title">{{ o.detail || '—' }}</div>
              <div class="act-sub">{{ opLabel(o.op) }}{{ o.result ? ` · ${o.result}` : '' }}</div>
            </div>
            <span class="act-time">{{ ago(o.created_at) }}</span>
          </div>
          <el-empty v-if="!ops.length && !loading" description="暂无操作" :image-size="60" />
        </div>
      </el-card>
    </div>

    <el-card class="page-card">
      <template #header><span>置信度分布（调阈值的依据）</span></template>
      <div ref="confEl" class="chart"></div>
    </el-card>

    <PreviewDialog v-model="previewOpen" v-model:doc-id="previewId" :ids="ids"
                   :title="recent.find((d: any) => d.id === previewId)?.current_filename || ''" />
  </div>
</template>

<style scoped>
.dash { display: flex; flex-direction: column; }
.hero-row { grid-template-columns: minmax(0, 1.9fr) minmax(260px, 1fr); align-items: stretch; }
.hero { display: flex; flex-direction: column; justify-content: center; }
.hero-kicker { font-size: 11.5px; letter-spacing: 1.2px; text-transform: uppercase; color: #7fd694;
  margin-bottom: 10px; }
.stats-card { display: flex; flex-direction: column; justify-content: space-between; }
.stat-foot { display: flex; flex-direction: column; gap: 8px; font-size: 12px; opacity: .85; }
.promo-link { font-weight: 650; cursor: pointer; font-size: 12.5px; }
.promo-link:hover { text-decoration: underline; }

.row-between { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
.health-grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(186px, 1fr)); }
.h-item { border-radius: 16px; padding: 13px 15px; display: flex; gap: 4px; align-items: flex-start;
  flex-direction: column; background: var(--df-surface-2); box-shadow: var(--df-shadow-sm); }
.h-item.warn { background: var(--el-color-warning-light-9); }
.h-item.error { background: var(--el-color-danger-light-9); }
.h-title { font-size: 12px; color: var(--el-text-color-secondary); }
.h-value { font-size: 17px; font-weight: 700; }
.h-sub { font-size: 12px; }
.issues { margin-top: 12px; display: flex; flex-direction: column; gap: 6px; }
.issue { display: flex; gap: 8px; font-size: 12.5px; padding: 8px 12px; border-radius: 12px;
  background: var(--el-color-warning-light-9); }
.issue.error { background: var(--el-color-danger-light-9); }
.issue b { color: var(--df-ink); }
.dash-retry { margin-bottom: 12px; border-radius: var(--df-radius-sm); }

.tile { padding: 12px 12px 10px; }
.tile-meta { text-align: center; margin-top: 2px; }
.empty-tile { grid-column: 1 / -1; box-shadow: var(--df-shadow-sm) !important; }

.chart { height: 220px; }
.chart-lg { height: 280px; }
.dash-bottom { grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); }
@media (max-width: 1180px) {
  .hero-row, .dash-bottom { grid-template-columns: minmax(0, 1fr); }
}
</style>