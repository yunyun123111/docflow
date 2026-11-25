<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, onMounted, ref } from 'vue'
import http from '../api/client'
import DocThumb from '../components/DocThumb.vue'
import PreviewDialog from '../components/PreviewDialog.vue'

const items = ref<any[]>([])
const loading = ref(false)
const previewOpen = ref(false)
const previewId = ref<number | null>(null)
const ids = computed(() => items.value.map((i) => i.document_id).filter(Boolean))

async function load() {
  loading.value = true
  try { items.value = await http.get('/recycle') } finally { loading.value = false }
}

function remain(it: any): string {
  if (!it.expires_at) return '永不清理'
  const hours = (new Date(it.expires_at).getTime() - Date.now()) / 3600000
  if (hours <= 0) return '已到期（下次维护清理）'
  if (hours < 48) return `${Math.round(hours)} 小时后清理`
  return `${Math.round(hours / 24)} 天后清理`
}

async function restore(it: any) {
  if (it.file_missing) {
    ElMessage.warning('该记录的文件已被删除，没有可恢复的文件；如需清掉记录请用「彻底删除」')
    return
  }
  await http.post(`/recycle/${it.id}/restore`)
  ElMessage.success('已恢复到原位置')
  load()
}

async function purge(it: any) {
  await ElMessageBox.confirm(`彻底删除「${it.name}」？此操作不可恢复！`, '危险', { type: 'error' })
  await http.post(`/recycle/${it.id}/purge`, null, { params: { confirm: true } })
  ElMessage.success('已删除')
  load()
}

async function empty() {
  await ElMessageBox.confirm('清空整个回收站？此操作不可恢复！', '危险', { type: 'error' })
  const r: any = await http.post('/recycle/empty', null, { params: { confirm: true } })
  ElMessage.success(`已清除 ${r.purged} 项`)
  load()
}

function openPreview(it: any) {
  if (!it.document_id) { ElMessage.info('该文件没有对应记录，无法预览'); return }
  previewId.value = it.document_id
  previewOpen.value = true
}

onMounted(load)
onActivated(load)
</script>

<template>
  <el-card v-loading="loading">
    <div class="toolbar" style="margin-bottom: 10px">
      <el-button type="danger" :disabled="!items.length" @click="empty">清空回收站</el-button>
      <span class="muted hint">到期项由后台自动清理（保留期在「设置 → 目录与归档」调整，0 = 永不自动删）</span>
      <div class="grow" />
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-table :data="items" size="small" @row-dblclick="openPreview">
      <el-table-column label="文件" min-width="260">
        <template #default="{ row }">
          <div class="name-row" @click="openPreview(row)">
            <DocThumb v-if="row.document_id" :doc-id="row.document_id" :file-type="row.file_type"
                    :exists="!row.file_missing" :size="30" class="thumb-xs" />
            <a class="fname">{{ row.name }}</a>
            <el-tag v-if="row.file_missing" size="small" type="warning" class="miss-tag"
                    title="磁盘文件已丢失，仅保留记录用于追溯">文件已丢失</el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="reason" label="原因" width="130" class-name="col-lg" label-class-name="col-lg" />
      <el-table-column prop="original_path" label="原位置" min-width="220" show-overflow-tooltip
                       class-name="col-xl" label-class-name="col-xl" />
      <el-table-column label="到期" width="150">
        <template #default="{ row }">{{ remain(row) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="openPreview(row)">预览</el-button>
          <el-button size="small" type="primary" :disabled="row.file_missing" @click="restore(row)">恢复</el-button>
          <el-button size="small" text type="danger" @click="purge(row)">彻底删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-if="!items.length && !loading" description="回收站是空的" />

    <PreviewDialog v-model="previewOpen" v-model:doc-id="previewId" :ids="ids"
                   :title="items.find((i) => i.document_id === previewId)?.name || ''" />
  </el-card>
</template>

<style scoped>
.name-row { display: flex; align-items: center; gap: 8px; cursor: pointer; min-width: 0; }
.thumb-xs { flex: 0 0 30px; }
.fname { color: var(--el-color-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.miss-tag { flex: 0 0 auto; }
</style>