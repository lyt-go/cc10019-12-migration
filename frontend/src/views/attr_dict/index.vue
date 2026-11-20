<template>
  <section class="page" data-module="attr_dict">
    <header class="page-head">
      <div>
        <h2>属性字典管理</h2>
        <p class="page-desc">
          维护用户属性字段的字典定义、枚举管理与跨源归因配置；枚举字段按兼容映射升级到第 2 版代码，
          存量值逐条给出可复核的迁移结果。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记属性定义</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出属性字典清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field.key" class="filter-item">
        <span>{{ field.label }}</span>
        <input v-model="filters[field.key]" :data-testid="`filter-${field.label}`" :placeholder="`按${field.label}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column.key">{{ column.label }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column.key">{{ displayValue(row, column.key) }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`migration-${row.id}`"
              @click="openMigration(row)"
            >
              迁移明细
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无属性字典数据，可先登记属性定义</td>
        </tr>
      </tbody>
    </table>

    <section v-if="migration" class="migration-panel" data-testid="migration-panel">
      <header class="panel-head">
        <h3>迁移明细 · {{ migration.attr_id }} {{ migration.attr_name }}</h3>
        <span class="panel-meta">
          第 {{ migration.from_field_version }} 版 → 第 {{ migration.target_field_version }} 版
        </span>
      </header>
      <div class="stat-row">
        <article v-for="item in migrationStats" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>样本编号</th><th>用户</th><th>存量显示值</th><th>采集来源</th>
            <th>迁移结果</th><th>第2版代码</th><th>依据</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in migration.details" :key="item.sample_id" :data-testid="`migration-row-${item.sample_id}`" :data-status="item.status">
            <td>{{ item.sample_id }}</td>
            <td>{{ item.user_id }}</td>
            <td>{{ item.raw_value }}</td>
            <td>{{ item.source || '缺来源' }}</td>
            <td>{{ migrationStatusLabel(item.status) }}</td>
            <td>{{ item.code ?? '—' }}</td>
            <td>{{ item.reason }}</td>
          </tr>
        </tbody>
      </table>
      <div class="page-actions" style="margin-top:12px">
        <button
          class="btn primary"
          type="button"
          data-testid="apply-migration"
          :disabled="!migration.can_apply || migration.result === 'applied'"
          @click="applyMigration"
        >
          {{ migration.result === 'applied' ? '已升级到第 2 版' : '按明细升级字段定义' }}
        </button>
        <span v-if="!migration.can_apply" class="error-text">
          存在无法唯一解释的样本，迁移已阻断
        </span>
        <span v-if="migration.result === 'applied'" class="panel-meta">
          已迁移 {{ migration.migrated_sample_ids.length }} 条；未迁移 {{ migration.unmigrated_sample_ids.length }} 条
        </span>
      </div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记属性定义</h3>
        <label v-for="field in columns" :key="field.key" class="create-field">
          <span>{{ field.label }}</span>
          <input v-model="createValues[field.key]" :data-testid="`create-${field.label}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条属性字典记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
type Column = { label: string; key: string }

const ENDPOINT = '/api/attr_dict'
const columns: Column[] = [
  { label: '属性编号', key: 'attr_id' },
  { label: '属性名称', key: 'attr_name' },
  { label: '数据类型', key: 'data_type' },
  { label: '枚举值集', key: 'enum_values' },
  { label: '数据来源', key: 'source' },
  { label: '更新频率', key: 'update_freq' },
  { label: '归因方式', key: 'attribution' },
  { label: '属性版本', key: 'field_version' },
]
const actions = ['提交校验', '标记生效', '标记废弃']
const statuses = ['草稿', '待校验', '已生效', '已废弃']
const metricNames = ['属性定义总数', '待校验属性', '已生效属性']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const migration = ref<Row | null>(null)

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[2]).length },
])

const migrationStats = computed(() => {
  const counts = migration.value?.counts ?? {}
  return [
    { label: '可迁移样本', value: counts.migrated ?? 0 },
    { label: '缺来源未迁移', value: counts.blocked_missing_source ?? 0 },
    { label: '无法解释', value: (counts.unmapped ?? 0) + (counts.ambiguous ?? 0) },
    { label: '人工排除', value: counts.excluded ?? 0 },
  ]
})

function displayValue(row: Row, key: string) {
  const value = row[key]
  if (Array.isArray(value)) return value.length ? value.join('、') : '—'
  return value ?? '—'
}

function migrationStatusLabel(status: string) {
  return ({
    migrated: '已迁移',
    blocked_missing_source: '缺来源未迁移',
    ambiguous: '同源多码',
    unmapped: '无兼容映射',
    excluded: '人工排除',
  } as Record<string, string>)[status] ?? status
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
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
    link.download = 'attr_dict.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field.key, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  const values: Record<string, any> = { ...createValues.value }
  values.field_version = Number(values.field_version) || 1
  values.enum_values = values.enum_values ? String(values.enum_values).split(/[、,，]/).filter(Boolean) : []
  values.legacy_enum_values = []
  values.compat_map = []
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('属性字典动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '属性字典操作失败'
  }
}

async function openMigration(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.attr_id}/migration`)
    if (!response.ok) throw new Error('迁移明细读取失败')
    migration.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移明细读取失败'
  }
}

async function applyMigration() {
  if (!migration.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${migration.value.attr_id}/migration/apply`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.detail || payload.message || '迁移失败')
    }
    await openMigration({ attr_id: migration.value.attr_id })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('属性定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '属性字典列表读取失败'
  }
}

onMounted(reload)
</script>
