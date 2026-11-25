<script setup lang="ts">
import { Check, Close, Warning } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import http from '../api/client'
import { fmtTime } from '../utils/format'

/** 配置状态自检面板：逐项体检 + 一键修复，桌面版/浏览器版通用。 */
const props = withDefaults(defineProps<{ dense?: boolean; autoRun?: boolean }>(), {
  dense: false, autoRun: true,
})
const emit = defineEmits<{ (e: 'changed'): void }>()

const result = ref<any>(null)
const loading = ref(false)
const fixing = ref('')
const showOk = ref(false)

const LEVEL: Record<string, { color: string; text: string; icon: any }> = {
  ok: { color: 'var(--el-color-success)', text: '正常', icon: Check },
  info: { color: 'var(--el-color-info)', text: '提示', icon: Warning },
  warn: { color: 'var(--el-color-warning)', text: '注意', icon: Warning },
  error: { color: 'var(--el-color-danger)', text: '异常', icon: Close },
}

const items = computed<any[]>(() => (result.value?.items || []).filter((i: any) => showOk.value || i.level !== 'ok'))
const hiddenOk = computed(() => (result.value?.items || []).filter((i: any) => i.level === 'ok').length)
const fixable = computed<string[]>(() => [...new Set((result.value?.items || [])
  .filter((i: any) => i.fix).map((i: any) => String(i.fix)))] as string[])

async function run(quick = false) {
  loading.value = true
  try {
    result.value = await http.get('/system/selfcheck', { params: { quick } })
  } finally {
    loading.value = false
  }
}

async function repair(action: string) {
  fixing.value = action
  try {
    const r: any = await http.post(`/system/repair/${action}`)
    ElMessage.success(r.detail || '已修复')
    await run(false)
    emit('changed')
  } finally {
    fixing.value = ''
  }
}

async function repairAll() {
  for (const a of fixable.value) await repair(a)
}

onMounted(() => { if (props.autoRun) run(false) })
defineExpose({ run })
</script>

<template>
  <div class="sc" v-loading="loading">
    <div class="toolbar">
      <el-tag v-if="result" type="success" effect="dark">{{ result.summary?.ok ?? 0 }} 正常</el-tag>
      <el-tag v-if="result" :type="result.summary?.error ? 'danger' : 'success'" effect="dark">{{ result.summary?.error ?? 0 }} 异常</el-tag>
      <el-tag v-if="result" :type="result.summary?.warn ? 'warning' : 'info'" effect="dark">{{ result.summary?.warn ?? 0 }} 注意</el-tag>
      <el-tag v-if="result" type="info" effect="plain">{{ result.summary?.info ?? 0 }} 提示</el-tag>
      <span v-if="result" class="muted hint">{{ fmtTime(result.checked_at) }} 检查</span>
      <div class="grow" />
      <el-checkbox v-if="!dense" v-model="showOk" size="small">显示正常项（{{ hiddenOk }}）</el-checkbox>
      <el-button size="small" @click="run(true)">快速检查</el-button>
      <el-button size="small" type="primary" plain @click="run(false)">完整检查</el-button>
      <el-button v-if="fixable.length" size="small" type="warning" :loading="!!fixing" @click="repairAll()">
        一键修复全部（{{ fixable.length }}）
      </el-button>
    </div>

    <el-empty v-if="result && !items.length && !showOk" description="全部关键项正常 🎉" :image-size="80" />

    <div v-for="it in items" :key="it.key" class="sc-item">
      <el-icon :size="15" :style="{ color: LEVEL[it.level].color }"><component :is="LEVEL[it.level].icon" /></el-icon>
      <div class="sc-main">
        <div class="sc-line">
          <b>{{ it.label }}</b>
          <span class="muted mono lvl">{{ LEVEL[it.level].text }}</span>
          <span class="sc-msg">{{ it.message }}</span>
        </div>
        <div v-if="it.detail" class="muted hint">{{ it.detail }}</div>
      </div>
      <el-button v-if="it.fix" size="small" type="warning" plain :loading="fixing === it.fix"
                 @click="repair(it.fix)">修复</el-button>
    </div>
  </div>
</template>

<style scoped>
.sc { display: flex; flex-direction: column; gap: 8px; }
.sc-item { display: flex; align-items: flex-start; gap: 10px; padding: 11px 13px;
  border-radius: 14px; background: var(--df-surface-2); transition: background .14s; }
.sc-item:hover { background: var(--df-tint); }
.sc-main { flex: 1 1 auto; min-width: 0; }
.sc-line { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.sc-msg { font-size: 13px; }
.lvl { font-size: 11px; opacity: .75; }
</style>