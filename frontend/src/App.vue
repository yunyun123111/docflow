<script setup lang="ts">
import { FolderOpened, InfoFilled, Moon, Search, Sunny } from '@element-plus/icons-vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import http, { TOKEN_KEY, desktop } from './api/client'
import SelfCheckPanel from './components/SelfCheckPanel.vue'
import SideMenu from './components/SideMenu.vue'
import { useResponsive } from './composables/useResponsive'

const router = useRouter()
const route = useRoute()
const { bp, isCompact, width } = useResponsive()

const collapsed = ref(width.value < 1280)
const navOpen = ref(false)
const loginVisible = ref(false)
const password = ref('')
const loginError = ref('')
const badges = ref<any>({ review: 0 })
const job = ref<any>(null)
const status = ref<any>(null)
const info = ref<any>(null)
const dark = ref(document.documentElement.classList.contains('dark'))
const helpOpen = ref(false)
const statusOpen = ref(false)
const search = ref('')
const searchEl = ref<HTMLInputElement>()
let timers: number[] = []

const title = computed(() => (route.meta.title as string) || '')
const subtitle = computed(() => (route.meta.sub as string) || '机器处理确定的事，人处理不确定的事')
const statusLevel = computed(() => status.value?.level || 'ok')
const statusText = computed(() => {
  if (!status.value) return '自检中…'
  if (status.value.level === 'error') return `${status.value.summary?.error || 0} 项异常`
  if (status.value.level === 'warn') return `${status.value.summary?.warn || 0} 项注意`
  return '状态正常'
})
const jobPercent = computed(() => (job.value ? Math.round((job.value.processed / Math.max(1, job.value.total)) * 100) : 0))
const runtimeLabel = computed(() => {
  if (!info.value) return ''
  return info.value.frozen ? `桌面版 v${info.value.version}` : `源码版 v${info.value.version}`
})

async function checkAuth() {
  try {
    const st: any = await http.get('/auth/status')
    if (st.password_required && !localStorage.getItem(TOKEN_KEY)) loginVisible.value = true
  } catch { /* 服务还没起来时静默重试 */ }
}

async function doLogin() {
  loginError.value = ''
  try {
    const r: any = await http.post('/auth/login', { password: password.value })
    if (r.token) localStorage.setItem(TOKEN_KEY, r.token)
    loginVisible.value = false
    password.value = ''
    await refreshAll()
  } catch (e: any) {
    loginError.value = e?.response?.data?.detail || '登录失败'
  }
}

async function logout() {
  try { await http.post('/auth/logout') } catch { /* 忽略 */ }
  localStorage.removeItem(TOKEN_KEY)
  loginVisible.value = true
}

async function refreshBadges() {
  try {
    const ov: any = await http.get('/dashboard/overview')
    badges.value = { review: ov.need_review ?? 0 }
  } catch { /* 未登录时忽略 */ }
}

async function refreshJob() {
  try {
    const c: any = await http.get('/system/components')
    job.value = c.active_job
  } catch { job.value = null }
}

async function refreshStatus() {
  try { status.value = await http.get('/system/status') } catch { /* 忽略 */ }
}

async function refreshAll() {
  await Promise.all([checkAuth(), refreshBadges(), refreshJob(), refreshStatus()])
  try { info.value = await http.get('/system/info') } catch { /* 忽略 */ }
}

function toggleTheme() {
  dark.value = !dark.value
  document.documentElement.classList.toggle('dark', dark.value)
  localStorage.setItem('docflow_theme', dark.value ? 'dark' : 'light')
  // 图表配色读的是 CSS 变量，切主题后要重画一次才不会黑字压黑底
  window.dispatchEvent(new CustomEvent('df-theme'))
}

function doSearch() {
  const q = search.value.trim()
  if (!q) return
  router.push({ path: '/library', query: { q } })
}

function focusSearch() {
  if (isCompact()) { router.push('/library'); return }
  nextTick(() => searchEl.value?.focus())
}

function onKey(e: KeyboardEvent) {
  const t = e.target as HTMLElement
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(t?.tagName)) return
  if (e.key === '?') helpOpen.value = true
  else if (e.key === 'b' && !e.ctrlKey && !e.metaKey) collapsed.value = !collapsed.value
  else if (e.key === 'k' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault()
    focusSearch()
  }
}

watch(bp, (v) => { collapsed.value = v !== 'xl' && v !== 'lg' })
watch(() => route.path, () => { navOpen.value = false })

onMounted(() => {
  refreshAll()
  timers = [
    window.setInterval(refreshBadges, 6000),
    window.setInterval(refreshJob, 2500),
    window.setInterval(refreshStatus, 60000),
  ]
  window.addEventListener('docflow-unauthorized', () => { loginVisible.value = true })
  window.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  timers.forEach((t) => window.clearInterval(t))
  window.removeEventListener('keydown', onKey)
})
</script>

<template>
  <el-config-provider>
    <div class="shell">
      <aside v-if="!isCompact()" class="aside" :class="{ mini: collapsed }">
        <div class="brand" @click="collapsed = !collapsed" :title="collapsed ? '展开导航' : '收起导航'">
          <span class="brand-mark"><el-icon :size="17"><FolderOpened /></el-icon></span>
          <span v-if="!collapsed" class="brand-name">DocFlow <small>文流</small></span>
        </div>
        <SideMenu :collapsed="collapsed" :badges="badges" />
        <div v-if="!collapsed" class="aside-foot">
          <div class="help-card">
            <span class="help-chip">?</span>
            <div class="help-title">需要帮忙？</div>
            <div class="help-sub">快捷键、运行信息和配置自检都收在这里。</div>
            <button class="help-btn" @click="helpOpen = true">查看快捷键</button>
          </div>
          <div class="aside-ver muted">{{ runtimeLabel }}</div>
        </div>
        <div v-else class="aside-ver mini" :title="runtimeLabel"><span class="ver-dot" /></div>
      </aside>

      <el-container class="main-wrap">
        <el-header class="header" height="84px">
          <el-button v-if="isCompact()" text circle class="hamburger" @click="navOpen = true">☰</el-button>
          <div class="head-text">
            <h1 class="title">{{ title }}</h1>
            <p class="sub">{{ subtitle }}</p>
          </div>
          <div class="grow" />

          <label v-if="!isCompact()" class="search">
            <el-icon :size="15"><Search /></el-icon>
            <input ref="searchEl" v-model="search" placeholder="搜索文档 / 合同号 / 正文" @keyup.enter="doSearch" />
            <kbd v-if="!isCompact()">Ctrl K</kbd>
          </label>

          <el-tooltip v-if="job" :content="`${job.kind} 任务：${job.processed}/${job.total}`">
            <div class="job">
              <el-progress :percentage="jobPercent" :stroke-width="12" :show-text="false" style="width: 88px" />
              <span class="mono job-num">{{ job.processed }}/{{ job.total }}</span>
            </div>
          </el-tooltip>

          <el-popover v-model:visible="statusOpen" placement="bottom-end" :width="isCompact() ? 320 : 560" trigger="click">
            <template #reference>
              <button class="pill" :class="statusLevel" title="配置状态自检">
                <span class="dot" />{{ statusText }}
              </button>
            </template>
            <SelfCheckPanel dense @changed="refreshStatus" />
          </el-popover>

          <button class="icon-btn" :title="dark ? '切到浅色' : '切到深色'" @click="toggleTheme">
            <el-icon :size="16"><Sunny v-if="dark" /><Moon v-else /></el-icon>
          </button>

          <el-popover placement="bottom-end" :width="320" trigger="click">
            <template #reference>
              <button class="avatar" title="运行信息"><el-icon :size="16"><InfoFilled /></el-icon></button>
            </template>
            <div v-if="info" class="runtime">
              <div><b>{{ runtimeLabel }}</b><span class="muted"> · {{ info.os }}</span></div>
              <div class="muted hint">形态：{{ info.frozen ? '打包桌面版（DocFlow.exe）' : '源码运行' }}</div>
              <div class="muted hint mono break">{{ info.data_dir }}</div>
              <div class="muted hint">界面资源：{{ info.frontend_bundled ? '已内置' : '缺失（请重新构建）' }}</div>
              <div class="muted hint">监听：{{ info.host }} · 已运行 {{ Math.floor(info.uptime_seconds / 60) }} 分钟</div>
              <div class="op">
                <el-button size="small" @click="helpOpen = true">快捷键</el-button>
                <el-button v-if="desktop.available" size="small" @click="desktop.quit()">退出应用</el-button>
                <el-button size="small" text type="danger" @click="logout">退出登录</el-button>
              </div>
            </div>
          </el-popover>

          <el-dialog v-model="helpOpen" title="快捷键" width="440px" append-to-body>
            <el-table size="small" :data="[
              { k: '/', v: '文档库搜索框聚焦' },
              { k: 'Ctrl/⌘ + K', v: '聚焦顶部搜索' },
              { k: 'B', v: '收起/展开左侧导航' },
              { k: 'J / K', v: '审核队列上下切换' },
              { k: 'A 或 Enter', v: '审核：确认归档' },
              { k: 'S', v: '审核：跳过' },
              { k: '← →', v: '预览翻页 / 切换上一篇' },
              { k: '+ -', v: '预览缩放' },
              { k: 'R', v: '预览旋转' },
              { k: 'F', v: '预览全屏' },
              { k: '?', v: '打开本帮助' },
            ]">
              <el-table-column prop="k" label="按键" width="120" />
              <el-table-column prop="v" label="作用" />
            </el-table>
          </el-dialog>
        </el-header>

        <el-main class="main">
          <router-view v-slot="{ Component }">
            <keep-alive :max="6">
              <component :is="Component" :key="route.path" />
            </keep-alive>
          </router-view>
        </el-main>
      </el-container>

      <el-drawer v-model="navOpen" direction="ltr" size="238px" :with-header="false" class="nav-drawer">
        <div class="brand" @click="navOpen = false">
          <span class="brand-mark"><el-icon :size="17"><FolderOpened /></el-icon></span>
          <span class="brand-name">DocFlow <small>文流</small></span>
        </div>
        <SideMenu :badges="badges" @navigate="navOpen = false" />
      </el-drawer>

      <el-dialog v-model="loginVisible" title="DocFlow 登录" width="360px" :close-on-click-modal="false"
                 :show-close="false">
        <el-input v-model="password" type="password" placeholder="访问密码" show-password @keyup.enter="doLogin" />
        <div v-if="loginError" class="err">{{ loginError }}</div>
        <template #footer><el-button type="primary" @click="doLogin">进入</el-button></template>
      </el-dialog>
    </div>
  </el-config-provider>
</template>

<style>
.shell { display: flex; height: 100vh; overflow: hidden; position: relative; z-index: 1; gap: 0; }

/* 侧栏：白色面板 + 右侧极细分隔，参考版式里的左列 */
.aside { flex: 0 0 auto; width: var(--df-side-w); background: var(--df-surface); color: var(--df-ink-2);
  transition: width .18s ease; display: flex; flex-direction: column; overflow: hidden;
  box-shadow: 14px 0 40px -30px rgba(26, 38, 54, .5); z-index: 2; }
.aside.mini { width: var(--df-side-w-mini); }
.brand { display: flex; align-items: center; gap: 10px; padding: 20px 18px 14px; cursor: pointer;
  white-space: nowrap; }
.brand-mark { flex: 0 0 32px; width: 32px; height: 32px; border-radius: 11px; display: grid; place-items: center;
  background: var(--df-green); color: #fff; box-shadow: 0 10px 18px -10px rgba(62, 183, 90, .9); }
.aside.mini .brand { justify-content: center; padding: 20px 0 14px; }
.brand-name { font-weight: 700; font-size: 15.5px; color: var(--df-ink); letter-spacing: .2px; }
.brand-name small { opacity: .5; font-weight: 400; font-size: 12px; }
.aside-foot { margin-top: auto; padding: 12px 14px 16px; }
.aside-ver { font-size: 11.5px; white-space: nowrap; padding-top: 10px; }
.aside-ver.mini { margin-top: auto; text-align: center; padding: 0 0 16px; }
.ver-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--df-tint-2); box-shadow: 0 0 0 3px var(--df-tint); }

/* 底部帮助卡：参考版式的绿色渐变块 */
.help-card { position: relative; border-radius: 18px; background: var(--df-grad); color: var(--df-green-ink);
  padding: 20px 14px 14px; box-shadow: var(--df-shadow-sm); overflow: visible; }
.help-chip { position: absolute; top: -13px; left: 50%; transform: translateX(-50%); width: 26px; height: 26px;
  border-radius: 50%; background: var(--df-green); color: #fff; display: grid; place-items: center;
  font-size: 13px; font-weight: 700; box-shadow: 0 6px 14px -6px rgba(62, 183, 90, .9); }
.help-title { font-size: 13.5px; font-weight: 700; }
.help-sub { font-size: 11.5px; line-height: 1.6; opacity: .78; margin: 3px 0 10px; }
.help-btn { display: block; width: 100%; border: none; cursor: pointer; background: var(--df-surface);
  color: var(--df-green-ink); border-radius: 999px; padding: 7px 10px; font-size: 12px; font-weight: 600;
  box-shadow: var(--df-shadow-sm); }
.help-btn:hover { background: #fff; }
html.dark .help-card { color: #dff3e3; }
html.dark .help-btn { background: #eef7e6; color: #12281a; }

.main-wrap { flex: 1 1 auto; min-width: 0; }

/* 顶栏：标题 + 副标题 + 搜索胶囊 + 图标按钮 */
.header { display: flex; align-items: center; gap: 12px; padding: 16px 26px; background: transparent;
  border: none; flex-wrap: wrap; }
.head-text { min-width: 0; }
.header .title { margin: 0; font-size: 21px; font-weight: 700; letter-spacing: .2px; color: var(--df-ink);
  line-height: 1.25; }
.header .sub { margin: 2px 0 0; font-size: 12.5px; color: var(--el-text-color-secondary); }
.hamburger { background: var(--df-surface); box-shadow: var(--df-shadow-sm); }

.search { display: flex; align-items: center; gap: 8px; background: var(--df-surface); border-radius: 999px;
  padding: 8px 14px; min-width: 260px; max-width: 380px; box-shadow: var(--df-shadow-sm); color: var(--df-muted);
  border: 1px solid transparent; }
.search:focus-within { border-color: var(--df-green); }
.search input { border: none; outline: none; background: transparent; flex: 1 1 auto; min-width: 0;
  font-size: 13px; color: var(--df-ink); font-family: inherit; }
.search input::placeholder { color: var(--el-text-color-placeholder); }
.search kbd { font-size: 10.5px; color: var(--df-muted); background: var(--df-surface-2);
  border-radius: 6px; padding: 2px 5px; white-space: nowrap; }

.job { display: flex; align-items: center; gap: 8px; background: var(--df-surface); border-radius: 999px;
  padding: 7px 13px; box-shadow: var(--df-shadow-sm); }
.job-num { font-size: 12px; color: var(--el-text-color-secondary); }
.pill { display: flex; align-items: center; gap: 7px; border: none; background: var(--df-surface);
  color: var(--el-text-color-regular); border-radius: 999px; padding: 8px 14px; font-size: 12.5px;
  cursor: pointer; box-shadow: var(--df-shadow-sm); font-family: inherit; }
.pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--el-color-success); }
.pill.warn .dot { background: var(--el-color-warning); }
.pill.error .dot { background: var(--el-color-danger); }
.pill.info .dot { background: var(--el-color-info); }
.pill:hover { color: var(--df-green-deep); }
.icon-btn { width: 36px; height: 36px; border-radius: 50%; border: none; cursor: pointer; display: grid;
  place-items: center; background: var(--df-surface); color: var(--el-text-color-regular);
  box-shadow: var(--df-shadow-sm); }
.icon-btn:hover { color: var(--df-green-deep); }
.avatar { width: 38px; height: 38px; border-radius: 50%; border: none; cursor: pointer; display: grid;
  place-items: center; color: #fff; background: var(--df-green);
  box-shadow: 0 10px 20px -10px rgba(62, 183, 90, .95); }
.avatar:hover { background: var(--df-green-deep); }

.runtime { display: flex; flex-direction: column; gap: 5px; font-size: 13px; }
.break { word-break: break-all; }
.op { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
.err { color: var(--el-color-danger); margin-top: 8px; font-size: 13px; }

.main { background: transparent; padding: 4px 26px 26px; overflow: auto; }
.nav-drawer .el-drawer__body { padding: 0; background: var(--df-surface); height: 100%; }
.nav-drawer .brand { border-bottom: 1px solid var(--df-line); }

@media (max-width: 1080px) {
  .header { padding: 12px 14px; }
  .main { padding: 2px 14px 16px; }
  .search { min-width: 180px; }
}
@media (max-width: 640px) {
  .main { padding: 0 9px 12px; }
  .header { padding: 10px 12px; gap: 8px; }
  .header .title { font-size: 18px; }
  .header .sub { display: none; }
}
</style>