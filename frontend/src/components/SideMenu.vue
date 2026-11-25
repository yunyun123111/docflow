<script setup lang="ts">
import {
  Collection, DataLine, Delete, Document, FolderOpened, Money,
  Setting, Tickets, Tools, Box,
} from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'

defineProps<{ collapsed?: boolean; badges?: any }>()
const emit = defineEmits<{ (e: 'navigate'): void }>()
const router = useRouter()

const GROUPS = [
  {
    caption: '',
    items: [
      { path: '/dashboard', label: '看板', icon: DataLine },
      { path: '/organize', label: '整理', icon: Tools },
      { path: '/library', label: '文档库', icon: Collection },
      { path: '/review', label: '人工审核', icon: Tickets, badge: 'review' },
      { path: '/reconciliation', label: '台账对账', icon: Money },
    ],
  },
  {
    caption: '维护',
    items: [
      { path: '/bundles', label: '文档归集', icon: Box },
      { path: '/rules', label: '规则中心', icon: FolderOpened },
      { path: '/recycle', label: '回收站', icon: Delete },
      { path: '/logs', label: '操作日志', icon: Document },
    ],
  },
  { caption: '设置', items: [{ path: '/settings', label: '设置', icon: Setting }] },
]
</script>

<template>
  <nav class="nav">
    <template v-for="(g, gi) in GROUPS" :key="gi">
      <div v-if="g.caption && !collapsed" class="nav-cap">{{ g.caption }}</div>
      <div v-else-if="g.caption && collapsed" class="nav-sep" />
      <router-link v-for="it in g.items" :key="it.path" :to="it.path" custom v-slot="{ navigate, isExactActive }">
        <a class="nav-item" :class="{ on: isExactActive }" :title="it.label"
           @click="() => { navigate(); emit('navigate') }">
          <span class="nav-ico"><el-icon :size="16"><component :is="it.icon" /></el-icon></span>
          <span v-if="!collapsed" class="nav-label">{{ it.label }}</span>
          <span v-if="!collapsed && it.badge && badges?.[it.badge]" class="nav-badge">{{ badges[it.badge] }}</span>
          <span v-if="collapsed && it.badge && badges?.[it.badge]" class="nav-dot" />
        </a>
      </router-link>
    </template>
  </nav>
</template>

<style scoped>
.nav { display: flex; flex-direction: column; gap: 2px; padding: 6px 12px 12px; }
.nav-cap { font-size: 10.5px; font-weight: 700; letter-spacing: 1.1px; color: var(--df-muted);
  text-transform: uppercase; padding: 14px 10px 6px; }
.nav-sep { height: 1px; background: var(--df-line); margin: 10px 8px; }
.nav-item { display: flex; align-items: center; gap: 11px; padding: 9px 11px; border-radius: 12px;
  color: var(--el-text-color-regular); cursor: pointer; text-decoration: none; font-size: 13.5px;
  white-space: nowrap; overflow: hidden; position: relative; transition: background .14s, color .14s; }
.nav-ico { flex: 0 0 22px; width: 22px; height: 22px; border-radius: 8px; display: grid; place-items: center;
  color: var(--df-muted); transition: color .14s, background .14s; }
.nav-item:hover { background: var(--df-tint); color: var(--df-ink); }
.nav-item:hover .nav-ico { color: var(--df-green-deep); }
.nav-item.on { background: var(--df-tint); color: var(--df-green-deep); font-weight: 650; }
.nav-item.on .nav-ico { background: var(--df-green); color: #fff; box-shadow: 0 8px 14px -8px rgba(62, 183, 90, .9); }
/* 左侧绿色指示条，参考版式的选中态 */
.nav-item.on::before { content: ""; position: absolute; left: -12px; top: 8px; bottom: 8px; width: 3px;
  border-radius: 0 3px 3px 0; background: var(--df-green); }
.nav-label { flex: 1 1 auto; }
.nav-badge { background: var(--df-green); color: #fff; border-radius: 999px; padding: 1px 8px;
  font-size: 11px; font-weight: 700; }
.nav-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--df-green); margin-left: 2px; }
</style>