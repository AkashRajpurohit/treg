<script>
import { useDashboard } from '../../state/context'
import Avatar from '../../components/ui/Avatar.vue'
import { IMAGE_RULES, imageProblem, uploadImage } from '../../images'
import { CATEGORIES, MAX_APPS, MAX_SHOTS, blankDraft, draftOf, moved, revisionOf, slotsUsed, statusOf } from './showcase'

// The team's entries on the public showcase (/showcase): apply, edit, withdraw. Owners and admins
// only (TeamPage shows the tab on canAdmin). Every name here starts with `sc` so it never shadows
// the dashboard state this component also reads (useDashboard).
const CONSENTS = [
  ['show_calls', 'Call tier', 'A rounded band of monthly calls, and a place on the Most active and Rising boards.'],
  ['show_streak', 'Streak', 'How many days in a row the app has made calls.'],
  ['show_providers', 'Providers', 'How many providers the app uses.'],
  ['show_categories', 'Categories', 'Its top capability categories.'],
]

export default {
  components: { Avatar },
  setup: useDashboard,
  data: () => ({
    CATEGORIES, CONSENTS, MAX_APPS, MAX_SHOTS, IMAGE_RULES,
    scApps: [], scLoaded: false, scClosed: false, scErr: '', scMsg: '',
    scForm: null, scFormErr: '', scStale: false, scSaving: false,
    scUpBusy: '', scUpErr: '', scUploadsOff: false, scConfirm: null,
  }),
  computed: {
    scUsed() { return slotsUsed(this.scApps) },
    scFull() { return this.scUsed >= MAX_APPS },
    scCanUpload() { return this.uploadsOn && !this.scUploadsOff },
  },
  watch: { activeOrgId() { this.scForm = null; this.scLoad() } },
  mounted() { this.scLoad() },
  methods: {
    scStatus: statusOf,
    async scLoad() {
      const id = this.activeOrgId
      if (!id) return
      this.scErr = ''
      try {
        const apps = await this.api('/showcase/teams/' + id + '/apps')
        if (id !== this.activeOrgId) return
        this.scApps = apps; this.scClosed = false
      } catch (e) {
        if (id !== this.activeOrgId) return
        this.scApps = []
        if (e.status === 404) this.scClosed = true
        else this.scErr = e.detail || 'Could not load the showcase (' + e.status + ').'
      } finally { this.scLoaded = true }
    },
    scNew() {
      this.scMsg = ''; this.scFormErr = ''; this.scUpErr = ''; this.scStale = false
      this.scForm = { id: null, version: 0, live: false, draft: blankDraft() }
    },
    scEdit(app) {
      this.scMsg = ''; this.scFormErr = ''; this.scUpErr = ''; this.scStale = false
      this.scForm = { id: app.id, version: app.version, live: !!app.approved_at, draft: draftOf(app) }
    },
    // After "this app changed": start again from what the server holds now.
    async scReloadForm() {
      const id = this.scForm && this.scForm.id
      await this.scLoad()
      const app = this.scApps.find(a => a.id === id)
      if (app) this.scEdit(app); else this.scForm = null
    },
    async scSave() {
      const f = this.scForm
      if (!f || this.scSaving) return
      this.scSaving = true; this.scFormErr = ''; this.scStale = false
      const fields = revisionOf(f.draft)
      const json = { 'content-type': 'application/json' }
      try {
        if (f.id === null) {
          await this.api('/showcase/teams/' + this.activeOrgId + '/apps', { method: 'POST', headers: json, body: JSON.stringify(fields) })
          this.scMsg = 'Sent for review. Nothing is public until treg approves it.'
        } else {
          await this.api('/showcase/apps/' + f.id, { method: 'PATCH', headers: json, body: JSON.stringify({ expected_version: f.version, fields }) })
          this.scMsg = f.live ? 'Change sent for review. The live version stays until treg approves the change.'
            : 'Change sent for review.'
        }
        this.scForm = null
        await this.scLoad()
      } catch (e) {
        this.scFormErr = e.detail || 'Could not save (' + e.status + ').'
        this.scStale = e.status === 409 && /changed/.test(this.scFormErr)
      } finally { this.scSaving = false }
    },
    async scWithdraw(app) {
      if (this.scConfirm !== app.id) { this.scConfirm = app.id; return }
      this.scConfirm = null; this.scErr = ''; this.scMsg = ''
      try {
        await this.api('/showcase/apps/' + app.id, { method: 'DELETE' })
        if (this.scForm && this.scForm.id === app.id) this.scForm = null
        await this.scLoad()
      } catch (e) { this.scErr = e.detail || 'Could not withdraw (' + e.status + ').' }
    },
    scPick(ref) { const input = this.$refs[ref]; if (input) input.click() },
    async scUpload(kind, event) {
      const input = event.target, file = input.files && input.files[0]
      input.value = ''
      if (!file || !this.scForm) return
      this.scUpErr = imageProblem(kind, file)
      if (this.scUpErr) return
      this.scUpBusy = kind
      try {
        const staged = await uploadImage(kind, file, this.headers())
        const pic = { key: staged.key, url: staged.preview_url }
        const d = this.scForm.draft
        if (kind === 'showcase_icon') d.icon = pic
        else if (d.shots.length < MAX_SHOTS && !d.shots.some(s => s.key === pic.key)) d.shots = [...d.shots, pic]
      } catch (e) {
        if (e.status === 503) this.scUploadsOff = true
        else this.scUpErr = e.detail || 'The picture could not be uploaded.'
      } finally { this.scUpBusy = '' }
    },
    scMove(i, by) { this.scForm.draft.shots = moved(this.scForm.draft.shots, i, by) },
    scDropShot(i) { this.scForm.draft.shots = this.scForm.draft.shots.filter((_, j) => j !== i) },
  },
}
</script>

<template>
<section class="sc" aria-labelledby="sc-title">
  <div class="sc-head">
    <div>
      <h2 id="sc-title" class="lbl" style="margin:0">Showcase</h2>
      <p class="sub" style="margin:4px 0 0;font-size:12px;max-width:640px">
        List an app your team built with treg on the public showcase. Nothing is public until treg approves it.
        An edit to a live app is reviewed too; the live version stays until the change is approved.
      </p>
    </div>
    <div v-if="!scClosed" class="sc-slots">
      <span class="sub" style="margin:0">{{scUsed}} of {{MAX_APPS}} used</span>
      <button class="btn primary sm" :disabled="scFull || !scLoaded" @click="scNew">＋ Apply with an app</button>
    </div>
  </div>

  <p v-if="scClosed" class="sub">The showcase is not open on this registry.</p>
  <template v-else>
    <p v-if="scFull" class="tut-notice" role="status" style="margin:10px 0 0">Your team uses all {{MAX_APPS}} places. Withdraw an entry to apply with another. Rejected entries do not count.</p>
    <div v-if="scErr" class="banner" role="alert" style="margin-top:10px">{{scErr}}</div>
    <div v-if="scMsg" class="tut-notice" role="status" style="margin-top:10px;display:flex;justify-content:space-between;gap:12px">
      <span>{{scMsg}}</span><button class="btn sm ico" aria-label="Dismiss" @click="scMsg=''">✕</button>
    </div>

    <p v-if="!scLoaded" class="muted" style="margin-top:12px">Loading…</p>
    <p v-else-if="!scApps.length && !scForm" class="sub" style="margin-top:12px">Your team has no apps on the showcase yet.</p>
    <ul v-else class="sc-list" aria-label="Your showcase entries">
      <li v-for="a in scApps" :key="a.id" class="card sc-app">
        <Avatar :src="(a.pending||a.live||{}).icon_url" :name="(a.pending||a.live||{}).name||'?'" :size="40" />
        <div class="sc-app-main">
          <div class="sc-app-name"><b>{{(a.pending||a.live||{}).name}}</b>
            <span class="chip sc-chip" :class="scStatus(a).tone">{{scStatus(a).text}}</span></div>
          <div class="sub" style="margin:2px 0 0;font-size:12px">{{(a.pending||a.live||{}).tagline}}</div>
          <div v-if="a.status!=='rejected' && a.approved_at && a.reject_reason && !a.pending" class="sub" style="margin:4px 0 0;font-size:12px">Your last change was not approved: {{a.reject_reason}}</div>
        </div>
        <div class="row-actions sc-app-actions">
          <button class="btn sm" @click="scEdit(a)">Edit</button>
          <button v-if="!a.approved_at || a.pending" class="btn sm" :class="{danger:scConfirm===a.id}" @click="scWithdraw(a)">
            {{scConfirm===a.id ? 'Confirm withdraw' : (a.approved_at ? 'Withdraw change' : 'Withdraw')}}</button>
        </div>
      </li>
    </ul>

    <form v-if="scForm" class="card sc-form" :aria-label="scForm.id===null?'Apply with an app':'Edit showcase entry'" @submit.prevent="scSave">
      <h3 style="margin:0 0 4px;font-size:15px">{{scForm.id===null ? 'Apply with an app' : 'Edit ' + (scForm.draft.name || 'entry')}}</h3>
      <p v-if="scForm.live" class="sub" style="margin:0 0 8px;font-size:12px">This app is live. Your change is reviewed before it replaces the live version.</p>
      <div class="sc-grid">
        <label class="sc-field"><span class="lbl">App name</span><input v-model="scForm.draft.name" maxlength="60" required></label>
        <label class="sc-field"><span class="lbl">Link (https)</span><input v-model="scForm.draft.url" type="url" placeholder="https://" required></label>
        <label class="sc-field sc-wide"><span class="lbl">Tagline</span><input v-model="scForm.draft.tagline" maxlength="80" required placeholder="What it does, in one line"></label>
        <label class="sc-field sc-wide"><span class="lbl">Description <span class="muted">(optional, up to 500 characters)</span></span>
          <textarea v-model="scForm.draft.description" maxlength="500" rows="4"></textarea></label>
        <label class="sc-field"><span class="lbl">Category</span>
          <select v-model="scForm.draft.category" class="msel" required>
            <option value="" disabled>Pick a category</option>
            <option v-for="[v,l] in CATEGORIES" :key="v" :value="v">{{l}}</option>
          </select></label>
        <label class="sc-field"><span class="lbl">Maker name <span class="muted">(optional)</span></span><input v-model="scForm.draft.maker_name" maxlength="60"></label>
        <label class="sc-field"><span class="lbl">X handle <span class="muted">(optional)</span></span><input v-model="scForm.draft.x_handle" maxlength="16" placeholder="@handle"></label>
        <label class="sc-field"><span class="lbl">Launch post <span class="muted">(optional)</span></span><input v-model="scForm.draft.launch_post_url" type="url" placeholder="https://"></label>
      </div>

      <div class="lbl" style="margin-top:14px">Pictures</div>
      <p v-if="!scCanUpload" class="sub sc-off" role="status" style="font-size:12px">Picture uploads are not enabled on this registry. You can apply without pictures.</p>
      <template v-else>
        <div class="sc-pics">
          <div>
            <div class="sub" style="margin:0 0 6px;font-size:12px">Icon. {{IMAGE_RULES.showcase_icon.hint}}</div>
            <div style="display:flex;gap:10px;align-items:center">
              <img v-if="scForm.draft.icon" class="sc-icon" :src="scForm.draft.icon.url" alt="App icon">
              <input ref="scIconInput" type="file" accept="image/png,image/jpeg,image/webp" style="display:none" @change="scUpload('showcase_icon',$event)">
              <button type="button" class="btn sm" :disabled="!!scUpBusy" @click="scPick('scIconInput')">{{scUpBusy==='showcase_icon'?'Uploading…':(scForm.draft.icon?'Replace icon':'Upload icon')}}</button>
              <button v-if="scForm.draft.icon" type="button" class="btn sm" @click="scForm.draft.icon=null">Remove icon</button>
            </div>
          </div>
          <div>
            <div class="sub" style="margin:0 0 6px;font-size:12px">Screenshots, up to {{MAX_SHOTS}}. The first is the cover. {{IMAGE_RULES.showcase_shot.hint}}</div>
            <ol class="sc-shots" aria-label="Screenshots">
              <li v-for="(s,i) in scForm.draft.shots" :key="s.key">
                <img :src="s.url" :alt="'Screenshot '+(i+1)">
                <span v-if="i===0" class="chip">Cover</span>
                <span class="row-actions">
                  <button type="button" class="btn sm" :disabled="i===0" :aria-label="'Move screenshot '+(i+1)+' up'" @click="scMove(i,-1)">↑</button>
                  <button type="button" class="btn sm" :disabled="i===scForm.draft.shots.length-1" :aria-label="'Move screenshot '+(i+1)+' down'" @click="scMove(i,1)">↓</button>
                  <button type="button" class="btn sm" :aria-label="'Remove screenshot '+(i+1)" @click="scDropShot(i)">Remove</button>
                </span>
              </li>
            </ol>
            <input ref="scShotInput" type="file" accept="image/png,image/jpeg,image/webp" style="display:none" @change="scUpload('showcase_shot',$event)">
            <button v-if="scForm.draft.shots.length<MAX_SHOTS" type="button" class="btn sm" :disabled="!!scUpBusy" @click="scPick('scShotInput')">{{scUpBusy==='showcase_shot'?'Uploading…':'Add screenshot'}}</button>
          </div>
        </div>
        <p v-if="scUpErr" class="banner" role="alert" style="margin:8px 0 0">{{scUpErr}}</p>
      </template>

      <fieldset class="sc-consent">
        <legend class="lbl">What the showcase may show</legend>
        <p class="sub" style="margin:0 0 6px;font-size:12px">Each one is off until you turn it on. Only rounded figures are shown, never exact counts, spend or your team's name.</p>
        <label v-for="[k,title,text] in CONSENTS" :key="k" class="sc-check">
          <input v-model="scForm.draft[k]" type="checkbox"> <span><b>{{title}}</b> <span class="muted">{{text}}</span></span>
        </label>
      </fieldset>

      <div v-if="scFormErr" class="banner" role="alert" style="margin-top:10px">{{scFormErr}}
        <button v-if="scStale" type="button" class="btn sm" style="margin-left:8px" @click="scReloadForm">Reload this app</button></div>
      <div style="display:flex;gap:10px;margin-top:14px">
        <button type="submit" class="btn primary" :disabled="scSaving || !!scUpBusy">{{scSaving ? 'Sending…' : 'Send for review'}}</button>
        <button type="button" class="btn" @click="scForm=null">Cancel</button>
      </div>
    </form>
  </template>
</section>
</template>

<style>
.sc-head { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; flex-wrap:wrap; }
.sc-slots { display:flex; gap:12px; align-items:center; }
.sc-list { list-style:none; padding:0; margin:14px 0 0; display:grid; gap:10px; }
.sc-app { display:flex; gap:12px; align-items:center; padding:12px 14px; }
.sc-app-main { flex:1; min-width:0; }
.sc-app-name { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.sc-app-actions { display:flex; gap:6px; flex-wrap:wrap; }
.sc-chip.ok { color:var(--green); border-color:color-mix(in srgb,var(--green) 30%,transparent); background:none; }
.sc-chip.review { color:var(--amber); border-color:color-mix(in srgb,var(--amber) 30%,transparent); background:none; }
.sc-chip.bad { color:var(--red); border-color:color-mix(in srgb,var(--red) 30%,transparent); background:none; white-space:normal; }
.sc-chip.off { color:var(--muted); border-color:var(--line2); background:none; }
.sc-form { margin-top:14px; padding:16px 18px; }
.sc-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px 14px; }
.sc-field { display:flex; flex-direction:column; gap:4px; min-width:0; }
.sc-field .lbl { margin:0; }
.sc-field input, .sc-field textarea, .sc-field select { width:100%; box-sizing:border-box; }
.sc-field textarea { background:var(--bg); border:1px solid var(--line); color:var(--ink); border-radius:8px; padding:8px 10px; font:inherit; resize:vertical; }
.sc-wide { grid-column:1 / -1; }
.sc-pics { display:grid; gap:14px; }
.sc-icon { width:64px; height:64px; border-radius:12px; object-fit:cover; border:1px solid var(--line); }
.sc-shots { list-style:none; padding:0; margin:0 0 8px; display:grid; gap:8px; }
.sc-shots li { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.sc-shots img { width:160px; height:auto; aspect-ratio:1280/800; object-fit:cover; border-radius:8px; border:1px solid var(--line); }
.sc-consent { border:0; padding:0; margin:14px 0 0; }
.sc-check { display:flex; gap:8px; align-items:flex-start; margin:6px 0; font-size:13px; }
@media (max-width:700px) { .sc-grid { grid-template-columns:1fr; } .sc-app { flex-wrap:wrap; } }
</style>
