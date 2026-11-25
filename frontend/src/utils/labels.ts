/** 操作名的中文映射：后端枚举不再直接露到界面上。 */
export const OP_CN: Record<string, string> = {
  import: '导入', upload: '入库', archive: '归档', rename: '重命名', move: '移动',
  delete: '删除(回收站)', restore: '恢复', purge: '彻底删除', undo: '撤销',
  approve: '审核确认', review: '人工审核', fix: '修正', patch: '修正',
  bundle_create: '新建业务包', bundle_delete: '解散业务包', bundle_merge: '合并业务包',
  bundle_rebuild: '重建归集', bundle_requirements: '调整必备单据清单',
}

export function opText(op?: string | null): string {
  return (op && OP_CN[op]) || op || ''
}
