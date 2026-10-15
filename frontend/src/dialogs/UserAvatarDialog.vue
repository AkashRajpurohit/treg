<script>
import { useDashboard } from '../state/context'
import Avatar from '../components/ui/Avatar.vue'
import { IMAGE_RULES, imageProblem, uploadImage } from '../images'

// Your own picture, from the account menu: upload stages it, PUT /me/avatar makes it yours.
export default {
  components: { Avatar },
  setup: useDashboard,
  data: () => ({ uaBusy: false, uaErr: '', uaOff: false, uaHint: IMAGE_RULES.user_avatar.hint }),
  methods: {
    async uaSet(key) {
      const r = await this.api('/me/avatar', { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ key }) })
      this.meAvatar = r.avatar_url || null
    },
    async uaUpload(event) {
      const input = event.target, file = input.files && input.files[0]
      input.value = ''
      if (!file) return
      this.uaErr = imageProblem('user_avatar', file)
      if (this.uaErr) return
      this.uaBusy = true
      try { await this.uaSet((await uploadImage('user_avatar', file, this.headers())).key) }
      catch (e) { if (e.status === 503) this.uaOff = true; else this.uaErr = e.detail || 'The picture could not be saved.' }
      finally { this.uaBusy = false }
    },
    async uaRemove() {
      this.uaBusy = true; this.uaErr = ''
      try { await this.uaSet(null) }
      catch (e) { this.uaErr = e.detail || 'The picture could not be removed.' }
      finally { this.uaBusy = false }
    },
  },
}
</script>

<template>
<div class="scrim" role="dialog" aria-labelledby="user-avatar-title" aria-modal="true" v-dialog="() => { avatarDialog=false }" @click.self="avatarDialog=false">
  <div class="modal" style="width:min(420px,92vw)">
    <div class="hd"><b id="user-avatar-title">Your picture</b><button class="btn sm ico" aria-label="Close" @click="avatarDialog=false">✕</button></div>
    <div style="padding:18px">
      <div style="display:flex;gap:14px;align-items:center">
        <Avatar :src="meAvatar" :name="me" :size="64" round />
        <p class="sub" style="margin:0;font-size:12px">Shown in the top bar and in your teams' member lists. Without one, your initials are shown.</p>
      </div>
      <p v-if="!uploadsOn || uaOff" class="sub" style="margin:14px 0 0">Picture uploads are not enabled on this registry.</p>
      <template v-else>
        <p class="sub" style="margin:14px 0 8px;font-size:12px">{{uaHint}}</p>
        <input ref="uaInput" type="file" accept="image/png,image/jpeg,image/webp" style="display:none" aria-label="Picture file" @change="uaUpload">
        <div style="display:flex;gap:10px">
          <button class="btn primary" :disabled="uaBusy" @click="$refs.uaInput.click()">{{uaBusy ? 'Saving…' : 'Upload picture'}}</button>
          <button v-if="meAvatar" class="btn" :disabled="uaBusy" @click="uaRemove">Remove picture</button>
        </div>
        <p v-if="uaErr" class="banner" role="alert" style="margin:12px 0 0">{{uaErr}}</p>
      </template>
    </div>
  </div>
</div>
</template>
