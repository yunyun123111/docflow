<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onActivated, onMounted, ref } from 'vue'
import http from '../api/client'
import { fmtTime } from '../utils/format'
import { opText } from '../utils/labels'

const rows = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const onlyError = ref(false)
const opFilter = ref('')

async function load() {
  const r: any = await http.get('/logs', { params: { only_error: onlyError.value, op: opFilter.value || undefined, limit: 100, offset: (page.value - 1) * 100 } })
  rows.value = r.items
  total.value = r.total
}
async function undo(row: any) {
  await ElMessageBox.confirm(`撤销文档 #${row.document_id} 的整理结果（文件移回原位）？`, '撤销')
  try {
    const r: any = await http.post(`/logs/${row.id}/undo`)
    ElMessage.success(`已撤销 ${r.steps} 步`)
    load()
  } catch { /* toast */ }
}
onMounted(load)
onActivated(load)
</script>

<template>
  <el-card>
    <div style="display: flex; gap: 12px; margin-bottom: 10px">
      <el-select v-model="opFilter" placeholder="操作类型" clearable style="width: 160px" @change="load">
        <el-option v-for="k in ['import','archive','rename','delete','restore','purge','undo','approve']" :key="k" :label="opText(k)" :value="k" />
      </el-select>
      <el-checkbox v-model="onlyError" @change="load">只看错误</el-checkbox>
    </div>
    <el-table :data="rows" size="small">
      <el-table-column prop="op_label" label="操作" width="100" />
      <el-table-column prop="document_id" label="文档" width="70" />
      <el-table-column label="路径变化" min-width="320">
        <template #default="{ row }">
          <div class="path">{{ row.old_path }}</div>
          <div class="path arrow" v-if="row.new_path">↓ {{ row.new_path }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="detail" label="备注" width="140" show-overflow-tooltip />
      <el-table-column label="结果" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.result === 'ok' ? 'success' : 'danger'">{{ row.result === 'ok' ? '成功' : '失败' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="时间" width="132">
        <template #default="{ row }"><span class="mono muted">{{ fmtTime(row.created_at) }}</span></template>
      </el-table-column>
      <el-table-column label="" width="80">
        <template #default="{ row }">
          <el-button v-if="row.document_id && !row.undone && row.result === 'ok'" size="small" text @click="undo(row)">撤销</el-button>
          <el-tag v-else-if="row.undone" size="small">已撤销</el-tag>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination style="margin-top: 10px" layout="total, prev, pager, next" :total="total" :page-size="100" v-model:current-page="page" @current-change="load" />
  </el-card>
</template>

<style scoped>
.path { font-size: 12px; color: var(--el-text-color-regular); word-break: break-all; }
.arrow { color: var(--el-color-primary); }
</style>