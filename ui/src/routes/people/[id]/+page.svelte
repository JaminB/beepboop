<script lang="ts">
	// Profile tab (WEB_UI §5.4): how I talk to you, approving sensitive actions, sign-in.
	import { invalidateAll } from '$app/navigation';
	import { api, toProblem } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { errorText, t, tx } from '#lib/i18n.ts';

	let { data } = $props();
	const p = $derived(data.person);
	const self = $derived(session.me?.profile_id === p.id);
	const canEdit = $derived(session.isAdmin || self);

	let rate = $state(1);
	let verbosity = $state<'brief' | 'normal' | 'chatty'>('normal');
	let voice = $state('');
	let pin = $state('');
	let pinOpen = $state(false);
	let currentPw = $state('');
	let newPw = $state('');
	let error = $state('');

	$effect(() => {
		rate = p.speech_rate;
		verbosity = p.verbosity as 'brief' | 'normal' | 'chatty';
		voice = p.tts_voice ?? '';
	});

	async function patch(body: Record<string, unknown>, undo?: Record<string, unknown>) {
		error = '';
		const { error: err, response } = await api.PATCH('/api/profiles/{profile_id}', {
			params: { path: { profile_id: p.id } },
			body
		});
		if (!response.ok) {
			error = errorText(toProblem(err, response).code);
			return;
		}
		await invalidateAll();
		toast.show(t('common.saved'), undo ? () => patch(undo) : undefined);
	}

	async function savePin(e: SubmitEvent) {
		e.preventDefault();
		const { error: err, response } = await api.PUT('/api/profiles/{profile_id}/pin', {
			params: { path: { profile_id: p.id } },
			body: { pin }
		});
		pin = '';
		if (!response.ok) {
			error = errorText(toProblem(err, response).code);
			return;
		}
		pinOpen = false;
		await invalidateAll();
		toast.show(t('pin.saved'));
	}
	async function clearPin() {
		await api.DELETE('/api/profiles/{profile_id}/pin', { params: { path: { profile_id: p.id } } });
		await invalidateAll();
		toast.show(t('pin.removed'));
	}
	async function changePassword(e: SubmitEvent) {
		e.preventDefault();
		const { error: err, response } = await api.POST('/api/auth/password', {
			body: { current_password: currentPw, new_password: newPw }
		});
		currentPw = newPw = '';
		if (!response.ok) error = errorText(toProblem(err, response).code);
		else toast.show(t('password.changed'));
	}
	async function signOutEverywhere() {
		await api.POST('/api/profiles/{profile_id}/sign-out-everywhere', { params: { path: { profile_id: p.id } } });
		toast.show(t('sessions.signed_out'));
		if (self) location.assign('/login');
	}
</script>

{#if error}<p class="error-text" role="alert">{error}</p>{/if}

<section class="card">
	<h2>{t('profile.talk')}</h2>
	<label class="field"><span class="label">{t('profile.voice')}</span>
		<input bind:value={voice} disabled={!canEdit} placeholder="en_US-lessac-medium" onchange={() => patch({ tts_voice: voice || null })} /></label>
	<label class="field"><span class="label">{t('profile.speed')}</span>
		<input type="range" min="0.5" max="2" step="0.1" bind:value={rate} disabled={!canEdit}
			onchange={() => patch({ speech_rate: rate }, { speech_rate: p.speech_rate })} aria-valuetext={`${rate}×`} />
		<span class="help">{t('profile.speed_value', { rate })}</span></label>
	<fieldset class="row" disabled={!canEdit}>
		<legend>{t('profile.verbosity')}</legend>
		{#each ['brief', 'normal', 'chatty'] as const as v (v)}
			<label class="row"><input type="radio" bind:group={verbosity} value={v} onchange={() => patch({ verbosity: v }, { verbosity: p.verbosity })} /> {tx(`verbosity.${v}`)}</label>
		{/each}
	</fieldset>
	<label class="field"><span class="label">{t('profile.language')}</span><select disabled><option>English (US)</option></select></label>
	<a href="/integrations/persona">{t('profile.persona')}</a>
</section>

{#if p.kind === 'admin' || p.kind === 'member'}
	<section class="card">
		<h2>{t('profile.approving')}</h2>
		<p>{t('profile.approving_help')}</p>
		{#if p.pin_set}
			<p>{t('pin.is_set')}{p.pin_locked ? ` · ${t('pin.locked')}` : ''}</p>
			{#if canEdit}<div class="row"><button onclick={() => (pinOpen = true)}>{t('pin.change')}</button><button onclick={clearPin}>{t('pin.remove')}</button></div>{/if}
		{:else if canEdit}
			<button onclick={() => (pinOpen = true)}>{t('pin.enable')}</button>
		{/if}
		{#if pinOpen}
			<form onsubmit={savePin} class="stack">
				<p class="banner-warn">{t('pin.warning')}</p>
				<label class="field"><span class="label">{t('pin.label')}</span>
					<input type="password" inputmode="numeric" autocomplete="off" minlength="4" maxlength="8" pattern="[0-9]{'{'}4,8{'}'}" bind:value={pin} required /></label>
				<button class="primary">{t('common.save')}</button>
			</form>
		{/if}
	</section>
{/if}

<section class="card">
	<h2>{t('profile.signin')}</h2>
	{#if p.has_login}
		<p>{t('profile.signin_line', { username: p.username ?? '', totp: p.totp_on ? t('common.on') : t('common.off') })}</p>
		{#if self}
			<details>
				<summary>{t('password.change')}</summary>
				<form onsubmit={changePassword}>
					<label class="field"><span class="label">{t('password.current')}</span><input type="password" bind:value={currentPw} autocomplete="current-password" required /></label>
					<label class="field"><span class="label">{t('password.new')}</span><input type="password" bind:value={newPw} autocomplete="new-password" minlength="12" required /></label>
					<button class="primary">{t('password.change')}</button>
				</form>
			</details>
		{/if}
		{#if canEdit}<button onclick={signOutEverywhere}>{t('sessions.everywhere')}</button>{/if}
	{:else}
		<p class="muted">{t('profile.no_login')}</p>
	{/if}
</section>

{#if session.isAdmin && !self && p.kind !== 'guest'}
	<section class="card">
		<h2>{t('profile.admin')}</h2>
		<label class="field"><span class="label">{t('profile.ceiling')}</span>
			<select value={p.risk_ceiling} onchange={(e) => patch({ risk_ceiling: (e.currentTarget as HTMLSelectElement).value }, { risk_ceiling: p.risk_ceiling })}>
				{#each ['read', 'act', 'personal', 'critical'] as r (r)}<option value={r}>{r}</option>{/each}
			</select></label>
		<label class="row"><input type="checkbox" checked={p.disabled} onchange={(e) => patch({ disabled: (e.currentTarget as HTMLInputElement).checked })} /> {t('profile.disabled')}</label>
	</section>
{/if}

<p><a href="/integrations">{t('profile.connections')}</a></p>
<details>
	<summary>{t('common.advanced')}</summary>
	<p class="help">{t('profile.thresholds', { kind: tx(`kind.${p.kind}`) })}</p>
</details>
