<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import {
  previewName, previewSegments, unknownTokens,
} from '../utils/templates'
import type { TemplateMode, TemplatePreset, TemplateToken } from '../utils/templates'

/** 模板编辑器：预设一键选 + 字段点选 + 固定样例实时示例，原始文本框收进「高级」。 */
const props = withDefaults(defineProps<{
  modelValue: string
  mode: TemplateMode
  presets: TemplatePreset[]
  tokens: TemplateToken[]
}>(), { modelValue: '' })

const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>()

const rawInput = ref<any>(null)
const advanced = ref<string[]>([])

const value = computed({
  get: () => props.modelValue || '',
  set: (v: string) => emit('update:modelValue', v),
})

const isPreset = computed(() => props.presets.some((p) => p.value === value.value))
const unknown = computed(() => unknownTokens(value.value))
const segments = computed(() => previewSegments(value.value))
const previewText = computed(() => {
  if (props.mode === 'path') {
    return segments.value.length ? segments.value.join('  /  ') : '（不分层，直接放在归档根目录下）'
  }
  return value.value.trim() ? previewName(value.value) : '（沿用上传时的原文件名）'
})

// 值不是任何预设时自动展开「高级」，免得用户找不到自己写的那串占位符
watch(value, () => { if (!isPreset.value) advanced.value = ['raw'] }, { immediate: true })

function applyPreset(p: TemplatePreset) {
  value.value = p.value
  advanced.value = []
}

function insertToken(tok: string) {
  const el: HTMLInputElement | undefined = rawInput.value?.input
  const cur = value.value
  const sep = props.mode === 'path' && cur && !cur.endsWith('/') ? '/' : ''
  if (el && document.activeElement === el) {
    const start = el.selectionStart ?? cur.length
    const end = el.selectionEnd ?? start
    value.value = cur.slice(0, start) + sep + tok + cur.slice(end)
    nextTick(() => {
      const pos = start + sep.length + tok.length
      el.focus()
      el.setSelectionRange(pos, pos)
    })
    return
  }
  value.value = cur + sep + tok
}

const NOTE_PATH = '用 / 分层；某段用到的字段缺失时，整段自动跳过。{year} {month} {day} 按归档当天，{date} 按文档上的日期。'
const NOTE_NAME = '分隔符直接写 _ 或 - 就行；字段缺失的那段会自动消失，多余的下划线会被折叠。'
</script>

<template>
  <div class="tpl">
    <div class="tpl-row">
      <el-button v-for="p in presets" :key="p.value" size="small"
                 :type="p.value === value ? 'primary' : 'default'"
                 :plain="p.value !== value" @click="applyPreset(p)">{{ p.label }}</el-button>
    </div>
    <div class="tpl-row tpl-tokens">
      <span class="tpl-cap">插入字段</span>
      <el-tag v-for="t in tokens" :key="t.token" class="tpl-token" type="info" effect="plain"
              @click="insertToken(t.token)">{{ t.label }}</el-tag>
    </div>
    <div class="tpl-preview">
      <span class="tpl-cap">{{ mode === 'path' ? '归档到' : '文件名' }}</span>
      <code class="tpl-sample">{{ previewText }}</code>
    </div>
    <div v-if="unknown.length" class="tpl-warn">
      未知占位符 {{ unknown.join(' ') }}：会渲染成空。点上面的字段标签，或在「高级」里改成正确写法。
    </div>
    <div class="tpl-note">{{ mode === 'path' ? NOTE_PATH : NOTE_NAME }}</div>
    <el-collapse v-model="advanced" class="tpl-adv">
      <el-collapse-item title="高级：直接编辑模板" name="raw">
        <el-input ref="rawInput" v-model="value" :placeholder="mode === 'path' ? '如 {category}/{year}/{month}' : '如 {date}_{contract_no}_{title}'" />
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<style scoped>
.tpl { width: 100%; display: flex; flex-direction: column; gap: 8px; }
.tpl-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tpl-row :deep(.el-button + .el-button) { margin-left: 0; }
.tpl-cap { font-size: 12px; color: var(--df-muted); }
.tpl-tokens { gap: 6px; }
.tpl-token { cursor: pointer; }
.tpl-preview { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
  padding: 8px 12px; border-radius: var(--df-radius-sm); background: var(--df-tint);
  border: 1px solid var(--df-line); }
.tpl-sample { font-size: 13px; color: var(--df-green-ink); word-break: break-all; }
.tpl-warn { font-size: 12px; color: var(--el-color-warning); line-height: 1.6; }
.tpl-note { font-size: 12px; color: var(--df-ink-2); line-height: 1.6; }
.tpl-adv :deep(.el-collapse-item__header) { font-size: 12px; color: var(--df-ink-2); height: 30px; line-height: 30px; border: none; }
.tpl-adv :deep(.el-collapse-item__wrap) { border: none; }
</style>
