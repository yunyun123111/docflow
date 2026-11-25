<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import DocPreview from './DocPreview.vue'
import { useResponsive } from '../composables/useResponsive'

/** 预览弹窗：在结果集里连续翻页（← →）、全屏、Esc 关闭。 */
const modelValue = defineModel<boolean>({ required: true })
const docId = defineModel<number | null>('docId', { default: null })

const props = withDefaults(defineProps<{ ids?: number[]; title?: string }>(), {
  ids: () => [], title: '',
})

const { dialogWidth, isCompact } = useResponsive()
const preview = ref<InstanceType<typeof DocPreview>>()
const full = ref(false)
const index = computed(() => (docId.value ? props.ids.indexOf(docId.value) : -1))

function move(delta: number) {
  if (!props.ids.length) return
  const next = Math.min(props.ids.length - 1, Math.max(0, index.value + delta))
  if (next !== index.value && props.ids[next] !== undefined) docId.value = props.ids[next]
}

function onKey(e: KeyboardEvent) {
  if (!modelValue.value) return
  const t = e.target as HTMLElement
  if (t && ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName)) return
  if (e.key === 'ArrowRight') { e.preventDefault(); move(1) }
  else if (e.key === 'ArrowLeft') { e.preventDefault(); move(-1) }
  else if (e.key === 'f' || e.key === 'F') { e.preventDefault(); full.value = !full.value }
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
watch(modelValue, (v) => { if (!v) full.value = false })
</script>

<template>
  <el-dialog v-model="modelValue" :width="full ? '100%' : dialogWidth('1080px')"
             :top="full ? '0' : '4vh'" :fullscreen="full" :show-close="false"
             :close-on-click-modal="isCompact()" destroy-on-close class="pv-dialog">
    <template #header>
      <div class="pv-head">
        <span class="pv-name">{{ title }}</span>
        <span v-if="ids.length > 1 && index >= 0" class="muted mono">{{ index + 1 }} / {{ ids.length }}</span>
        <div class="grow" />
        <el-button v-if="ids.length > 1" size="small" :disabled="index <= 0" @click="move(-1)">上一个</el-button>
        <el-button v-if="ids.length > 1" size="small" :disabled="index >= ids.length - 1" @click="move(1)">下一个</el-button>
        <el-button size="small" text @click="full = !full">{{ full ? '退出全屏' : '全屏' }}</el-button>
        <el-button size="small" text @click="modelValue = false">关闭 ✕</el-button>
      </div>
    </template>
    <DocPreview v-if="modelValue" ref="preview" :doc-id="docId" :pane="false" :show-name="false"
                :height="full ? 'calc(100vh - 108px)' : '72vh'" />
    <div class="pv-tip muted hint">← → 在结果中切换 · F 全屏 · Esc 关闭</div>
  </el-dialog>
</template>

<style scoped>
.pv-head { display: flex; align-items: center; gap: 8px; }
.pv-name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 46vw; }
.pv-tip { text-align: right; padding-top: 4px; }
</style>

<style>
.pv-dialog .el-dialog__header { padding: 10px 16px; margin-right: 0; border-bottom: 1px solid var(--df-line); }
.pv-dialog .el-dialog__body { padding: 0; }
</style>