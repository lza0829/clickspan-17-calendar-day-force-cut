<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const result = ref(null)
const err = ref('')
const busy = ref(false)

async function run() {
  busy.value = true
  err.value = ''
  result.value = null
  try {
    result.value = await api.ingest()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="panel">
    <h1>收流击点</h1>
    <p class="muted">
      读取夹具 ingest_batch.json，灌入正式通道。跨自然日的点强制切开（未超 idle 也切），同日仍交给空闲切窗。
    </p>
    <button class="btn" :disabled="busy" @click="run">{{ busy ? '收流中…' : '跑一批收流' }}</button>
    <p v-if="err" class="err">{{ err }}</p>

    <template v-if="result">
      <p v-if="!result.ok" class="err">{{ result.error }}</p>
      <template v-else>
        <div class="stats" style="margin-top: 0.8rem">
          <div class="stat"><b>{{ result.report.written }}</b><span>新写入</span></div>
          <div class="stat"><b>{{ result.report.replayed }}</b><span>重放跳过</span></div>
          <div class="stat"><b>{{ result.report.rejected.length }}</b><span>解析拒收</span></div>
          <div class="stat"><b>{{ result.report.cut_edges.length }}</b><span>日界切开</span></div>
          <div class="stat"><b class="mono">{{ result.report.timezone }}</b><span>裁决时区</span></div>
        </div>
        <h2>日界切开</h2>
        <p v-if="!result.report.cut_edges.length" class="muted">本批无跨日切口。</p>
        <table v-else class="data">
          <thead>
            <tr><th>访客</th><th>旧 → 新会话</th><th>D0 → D1</th><th>时区</th><th>触发键</th></tr>
          </thead>
          <tbody>
            <tr v-for="e in result.report.cut_edges" :key="e.trigger_hit_id">
              <td>{{ e.visitor_id }}</td>
              <td class="mono">{{ e.from_session_id }} → {{ e.to_session_id }}</td>
              <td class="mono">{{ e.from_day }} → {{ e.to_day }}</td>
              <td class="mono">{{ e.timezone }}</td>
              <td class="mono">{{ e.trigger_hit_id }}</td>
            </tr>
          </tbody>
        </table>
        <template v-if="result.report.rejected.length">
          <h2>解析拒收</h2>
          <pre class="mono">{{ result.report.rejected }}</pre>
        </template>
      </template>
    </template>
  </section>
</template>
