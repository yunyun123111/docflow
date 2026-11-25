import { createRouter, createWebHistory } from 'vue-router'

const routes: any[] = [
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', component: () => import('../views/DashboardView.vue'), meta: { title: '看板', sub: '今天也交给机器跑确定性的部分，你只看需要判断的' } },
  { path: '/organize', component: () => import('../views/OrganizeView.vue'), meta: { title: '整理', sub: '拖进来就能归档：识别、改名、分类全自动' } },
  { path: '/library', component: () => import('../views/LibraryView.vue'), meta: { title: '文档库', sub: '所有已入库文档，搜索、预览、修正、导出' } },
  { path: '/review', component: () => import('../views/ReviewView.vue'), meta: { title: '人工审核', sub: '机器没把握的都在这里，一次确认一个' } },
  { path: '/reconciliation', component: () => import('../views/ReconciliationView.vue'), meta: { title: '台账对账', sub: '台账与档案双向核对，差异一眼看到' } },
  { path: '/bundles', component: () => import('../views/BundleView.vue'), meta: { title: '文档归集', sub: '一份业务的合同、发票、结算单、提货单，自动归成一个包' } },
  { path: '/rules', component: () => import('../views/RulesView.vue'), meta: { title: '规则中心', sub: '识别规则、重命名模板与学习建议' } },
  { path: '/recycle', component: () => import('../views/RecycleView.vue'), meta: { title: '回收站', sub: '软删除都在这里，保留期内可恢复' } },
  { path: '/logs', component: () => import('../views/LogsView.vue'), meta: { title: '操作日志', sub: '每一步操作都留了痕' } },
  { path: '/settings', component: () => import('../views/SettingsView.vue'), meta: { title: '设置', sub: '目录、阈值、OCR/AI、自检与维护' } },
]

export default createRouter({ history: createWebHistory(), routes })