<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const edges = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await api.proofs()
    edges.value = await api.dayBoundaries()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function failedChecks(b) {
  return Object.entries(b.checks || {})
    .filter(([, ok]) => !ok)
    .map(([k]) => k)
}
</script>

<template>
  <section class="panel">
    <h1>会话核证</h1>
    <p class="muted">会话击点集合与起止须与织机同源可核。</p>
    <p v-if="err" class="err">{{ err }}</p>
    <table class="data">
      <thead>
        <tr><th>会话</th><th>访客</th><th>点数</th><th>起止</th><th>击点集</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td class="mono">{{ r.session_id }}</td>
          <td>{{ r.visitor_id }}</td>
          <td>{{ r.hit_count }}</td>
          <td class="mono">{{ r.started_at }} → {{ r.ended_at }}</td>
          <td class="mono">{{ r.hit_ids_json }}</td>
        </tr>
      </tbody>
    </table>
  </section>

  <section class="panel" style="margin-top: 1rem">
    <h1>日界核证</h1>
    <p class="muted">
      每条日界边按裁决时的时区快照复算，不随当前设置漂移；任一项校验不过即标红。
    </p>
    <p v-if="!edges.length" class="muted">暂无日界边。</p>
    <table v-else class="data">
      <thead>
        <tr>
          <th>结果</th><th>访客</th><th>旧 → 新会话</th><th>D0 → D1</th>
          <th>时区快照</th><th>触发键</th><th>校验</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="b in edges" :key="b.id">
          <td :class="b.ok ? 'ok' : 'err'"><b>{{ b.ok ? '通过' : '失败' }}</b></td>
          <td>{{ b.visitor_id }}</td>
          <td class="mono">{{ b.from_session_id }} → {{ b.to_session_id }}</td>
          <td class="mono">{{ b.from_day }} → {{ b.to_day }}</td>
          <td class="mono">
            {{ b.timezone }}
            <span v-if="b.timezone_drift?.differs" class="drift">
              当前 {{ b.timezone_drift.current }}
            </span>
          </td>
          <td class="mono">{{ b.trigger_hit_id }} · {{ b.trigger_event_ts }}</td>
          <td class="mono">
            <span v-if="b.ok" class="ok">全项一致</span>
            <span v-else class="err">{{ failedChecks(b).join('、') }}</span>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
