<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const sec = ref(1800)
const tz = ref('Asia/Shanghai')
const msg = ref('')
const tzMsg = ref('')
const err = ref('')
const tzErr = ref('')

const COMMON_ZONES = [
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Asia/Singapore',
  'Europe/London',
  'Europe/Berlin',
  'America/New_York',
  'America/Los_Angeles',
  'UTC',
]

onMounted(async () => {
  try {
    const s = await api.settings()
    sec.value = s.idle_timeout_sec
    tz.value = s.calendar_timezone
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function save() {
  err.value = ''
  msg.value = ''
  try {
    const res = await fetch('/api/settings/idle-timeout', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ idle_timeout_sec: Number(sec.value) }),
    })
    if (!res.ok) throw new Error(await res.text())
    const data = await res.json()
    sec.value = data.idle_timeout_sec
    msg.value = '已保存（基座装载尚未按此时切开）'
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function saveTz() {
  tzErr.value = ''
  tzMsg.value = ''
  try {
    const data = await api.saveCalendarTimezone(tz.value.trim())
    tz.value = data.calendar_timezone
    tzMsg.value = '已保存；只影响后续日界裁决，既有会话与日界边按快照核证、不回溯'
  } catch (e) {
    tzErr.value = String(e.message || e)
  }
}
</script>

<template>
  <section class="panel">
    <h1>空闲切窗</h1>
    <p class="muted">配置写入 settings；真正按间隙切会话由功能题空桩实现。</p>
    <label>
      秒数
      <input v-model.number="sec" type="number" min="60" step="60" class="mono" />
    </label>
    <p>
      <button class="btn" @click="save">保存</button>
    </p>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="err">{{ err }}</p>
  </section>

  <section class="panel" style="margin-top: 1rem">
    <h1>日历时区</h1>
    <p class="muted">
      日界强制切开按此时区把 event_ts 投影为日历日。同一批点间隙未超 idle，只要日历日不同也会切开。
    </p>
    <label>
      IANA 时区
      <input v-model="tz" list="tz-options" class="mono" placeholder="Asia/Shanghai" />
      <datalist id="tz-options">
        <option v-for="z in COMMON_ZONES" :key="z" :value="z" />
      </datalist>
    </label>
    <p>
      <button class="btn" @click="saveTz">保存时区</button>
    </p>
    <p v-if="tzMsg" class="ok">{{ tzMsg }}</p>
    <p v-if="tzErr" class="err">{{ tzErr }}</p>
  </section>
</template>
