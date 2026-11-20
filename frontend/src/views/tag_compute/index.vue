<template>
  <section class="page" data-module="tag_compute">
    <header class="page-head">
      <div>
        <h2>标签计算</h2>
        <p class="page-desc">
          维护标签批量计算的任务编排、分区依赖与执行追踪；每次计算绑定实际启用的字段版本，
          旧任务按当时口径回查，切换到新版代码失败时整轮不生效。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记计算任务</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
      </div>
    </header>

    <div class="filter-bar">
      <label><span>开始日期</span><input v-model="dateFrom" type="date" /></label>
      <label><span>结束日期</span><input v-model="dateTo" type="date" /></label>
      <button class="btn" type="button" data-testid="query" @click="applyFilter">查询</button>
    </div>

    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.task_id }} · {{ item.tag_id }}</span>
        <div class="timeline-track">
          <div
            class="timeline-bar"
            :class="`status-${barState(item)}`"
            :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }"
            :data-testid="`bar-${item.id}`"
            :title="`${item.task_id}：${item.exec_state}`"
            @click="selectItem(item)"
          >{{ item.exec_state }}</div>
        </div>
        <span class="timeline-meta">
          {{ item.mode === 'replay' ? '回查' : item.mode === 'switch' ? '切换' : '首轮' }}
          · 字段版本 {{ versionText(item.field_versions) }}
        </span>
      </div>
      <p v-if="!timelineItems.length" class="empty-state">暂无作业记录</p>
    </div>

    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.task_id }}</strong> — 关联标签 {{ selectedItem.tag_id }} · 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">实际启用字段版本：{{ versionText(selectedItem.field_versions) }}</div>
      <div>目标字段版本：{{ versionText(selectedItem.requested_field_versions) || '—' }}</div>
      <div>计算范围：{{ selectedItem.compute_scope }} · 结果条数：{{ selectedItem.result_count }}</div>
      <div v-if="selectedItem.error_log" class="run-error">{{ selectedItem.error_log }}</div>
      <div style="margin-top:6px;">{{ selectedItem.logs || '暂无日志' }}</div>
    </div>

    <section class="version-panel" data-testid="version-panel">
      <h3>字段版本与回查</h3>
      <div class="version-row">
        <label>
          <span>任务</span>
          <select v-model="runTaskId" data-testid="run-task">
            <option v-for="task in rows" :key="task.task_id" :value="task.task_id">
              {{ task.task_id }} · {{ task.tag_id }}
            </option>
          </select>
        </label>
        <button class="btn" type="button" data-testid="replay-task" :disabled="!runTaskId" @click="replaySelected">
          按当时口径回查
        </button>
        <button class="btn primary" type="button" data-testid="switch-task" :disabled="!runTaskId" @click="switchSelected">
          切换到字段第 2 版
        </button>
      </div>
      <div v-if="runMessage" class="version-row">
        <span :class="runFailed ? 'error-text' : 'panel-meta'" data-testid="run-message">{{ runMessage }}</span>
      </div>
      <div v-if="runResult" class="version-row" data-testid="run-result">
        <span class="version-badge">模式 {{ runResult.mode }}</span>
        <span class="version-badge">实际版本 {{ versionText(runResult.field_versions) }}</span>
        <span class="version-badge">命中 {{ runResult.matched_count }}</span>
        <span class="panel-meta">条件：{{ runResult.condition_used }}</span>
      </div>
      <div v-if="runResult?.resolutions?.length" class="version-row">
        <span v-for="item in runResult.resolutions" :key="`${item.tag_id}-${item.from}`" class="version-badge">
          {{ item.attr_id }} {{ item.from }} → {{ item.to }}
        </span>
      </div>
      <div v-if="runIssues.length" class="run-log" data-testid="run-issues">
        <div v-for="issue in runIssues" :key="issue.code + (issue.from ?? '')">
          [{{ issue.code }}] {{ issue.reason }}
        </div>
      </div>
      <div v-if="runSnapshots.length" class="run-log" data-testid="run-log" style="margin-top:8px">
        <div v-for="snap in runSnapshots" :key="`${snap.task_id}-${snap.compute_round}`">
          第 {{ snap.compute_round }} 轮 · {{ snap.exec_state }} · 模式 {{ snap.mode }} · 字段版本 {{ versionText(snap.field_versions) }}
        </div>
      </div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记计算任务</h3>
        <label v-for="field in columns.slice(0, 3)" :key="field.key" class="create-field">
          <span>{{ field.label }}</span>
          <input v-model="createValues[field.key]" :placeholder="`请输入${field.label}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/tag_compute'
const columns = [
  { label: '任务编号', key: 'task_id' },
  { label: '关联标签', key: 'tag_id' },
  { label: '计算范围', key: 'compute_scope' },
  { label: '计算引擎', key: 'engine' },
  { label: '输入分区', key: 'input_partitions' },
  { label: '输出分区', key: 'output_partition' },
  { label: '并行度', key: 'parallelism' },
  { label: '执行状态', key: 'exec_state' },
]

const rows = ref<Row[]>([])
const timelineItems = ref<Row[]>([])
const selectedItem = ref<Row | null>(null)
const dateFrom = ref('')
const dateTo = ref('')
const errorMessage = ref('')
const showCreate = ref(false)
const createValues = ref<Record<string, string>>({})

const runTaskId = ref('')
const runMessage = ref('')
const runFailed = ref(false)
const runResult = ref<Row | null>(null)
const runIssues = ref<Row[]>([])
const runSnapshots = ref<Row[]>([])

const allItems = computed(() => rows.value)

function versionText(versions: Record<string, number> | undefined) {
  if (!versions || !Object.keys(versions).length) return '—'
  return Object.entries(versions).map(([attr, version]) => `${attr}=v${version}`).join(' ')
}

function barState(item: Row) {
  const state = item.exec_state
  return state === 'success' ? '已完成' : state === 'running' ? '运行中' : state === 'failed' ? '失败' : '待执行'
}

function axisRange() {
  const items = allItems.value
  const min = 0
  const max = Math.max(items.length - 1, 1)
  return { min, max, range: max - min }
}

function barLeft(item: Row) {
  const index = allItems.value.findIndex((row) => row.id === item.id)
  const { min, range } = axisRange()
  return ((Math.max(index, 0) - min) / range) * 92
}

function barWidth(item: Row) {
  const { range } = axisRange()
  return Math.max(8, (1 / range) * 92)
}

function selectItem(item: Row) {
  selectedItem.value = selectedItem.value?.id === item.id ? null : item
}

function applyFilter() {
  let items = allItems.value
  if (dateFrom.value) items = items.filter((item) => String(item.output_partition ?? '') >= dateFrom.value)
  if (dateTo.value) items = items.filter((item) => String(item.output_partition ?? '') <= dateTo.value)
  timelineItems.value = items
  selectedItem.value = null
}

async function reload() {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT)
    if (!response.ok) throw new Error('计算任务读取失败')
    const data = await response.json()
    rows.value = data.items ?? []
    if (!runTaskId.value && rows.value.length) runTaskId.value = rows.value[0].task_id
    applyFilter()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '加载失败'
  }
}

async function replaySelected() {
  runMessage.value = ''
  runFailed.value = false
  runIssues.value = []
  try {
    const response = await request(`${ENDPOINT}/${runTaskId.value}/replay`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok) {
      runFailed.value = true
      runIssues.value = payload.detail?.issues ?? []
      throw new Error(payload.detail?.message || '回查失败')
    }
    runResult.value = payload.entry
    runMessage.value = '已按当时口径回查'
    await loadSnapshots()
  } catch (error) {
    runMessage.value = error instanceof Error ? error.message : '回查失败'
  }
}

async function switchSelected() {
  runMessage.value = ''
  runFailed.value = false
  runIssues.value = []
  const taskId = runTaskId.value
  if (!taskId) return
  try {
    const response = await request(`${ENDPOINT}/${taskId}/run`, {
      method: 'POST',
      body: JSON.stringify({ field_versions: { '*': 2 } }),
    })
    const payload = await response.json()
    if (!response.ok) {
      runFailed.value = true
      runIssues.value = payload.detail?.issues ?? []
      throw new Error(payload.detail?.message || '切换失败')
    }
    runResult.value = payload.entry
    runMessage.value = '已切换到第 2 版代码'
  } catch (error) {
    runMessage.value = error instanceof Error ? error.message : '切换失败'
  } finally {
    await loadSnapshots()
    await reloadRows()
  }
}

async function reloadRows() {
  const response = await request(ENDPOINT)
  if (!response.ok) return
  const data = await response.json()
  rows.value = data.items ?? []
  applyFilter()
}

async function loadSnapshots() {
  if (!runTaskId.value) return
  const response = await request(`${ENDPOINT}/runs?task_id=${runTaskId.value}`)
  if (!response.ok) return
  const data = await response.json()
  runSnapshots.value = data.items ?? []
}

function openCreate() {
  createValues.value = {}
  showCreate.value = true
}

async function submitCreate() {
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify({ values: createValues.value }),
    })
    if (!response.ok) throw new Error('创建失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '创建失败'
  }
}

function exportRows() {
  const header = columns.map((column) => column.label)
  const lines = [header.join(',')]
  for (const row of rows.value) {
    lines.push(columns.map((column) => {
      const value = row[column.key]
      const text = Array.isArray(value) ? value.join('|') : String(value ?? '')
      return `"${text.replace(/"/g, '""')}"`
    }).join(','))
  }
  const blob = new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'tag_compute.csv'
  link.click()
  URL.revokeObjectURL(url)
}

watch(runTaskId, () => {
  runResult.value = null
  runMessage.value = ''
  runIssues.value = []
  void loadSnapshots()
})

onMounted(async () => {
  await reload()
  await loadSnapshots()
})
</script>
