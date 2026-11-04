<script lang="ts">
	// Password → TOTP → session (WEB_UI §6.1). Lockouts are stated honestly.
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, toProblem } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { live } from '#lib/live.svelte.ts';
	import { errorText, t } from '#lib/i18n.ts';

	let username = $state('');
	let password = $state('');
	let totp = $state('');
	let recovery = $state('');
	let step = $state<'password' | 'totp'>('password');
	let useRecovery = $state(false);
	let error = $state('');
	let busy = $state(false);

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		error = '';
		const { data, error: err, response } = await api.POST('/api/auth/login', {
			body: {
				username,
				password,
				totp: step === 'totp' && !useRecovery ? totp : null,
				recovery_code: step === 'totp' && useRecovery ? recovery : null
			}
		});
		busy = false;
		if (response.ok && data) {
			session.set(data);
			live.start();
			const next = page.url.searchParams.get('next');
			await goto(next && next.startsWith('/') ? next : '/');
			return;
		}
		const p = toProblem(err, response);
		if (p.code === 'totp_required') {
			step = 'totp';
			return;
		}
		error =
			p.code === 'too_many_attempts'
				? t('login.locked', { minutes: Math.max(1, Math.ceil((p.retry_after ?? 60) / 60)) })
				: errorText(p.code);
	}
</script>

<h1>{t('login.title')}</h1>
<form class="card" onsubmit={submit}>
	{#if step === 'password'}
		<label class="field"><span class="label">{t('login.username')}</span>
			<input bind:value={username} autocomplete="username" autocapitalize="none" required /></label>
		<label class="field"><span class="label">{t('login.password')}</span>
			<input type="password" bind:value={password} autocomplete="current-password" required /></label>
	{:else if !useRecovery}
		<label class="field"><span class="label">{t('login.totp')}</span>
			<input bind:value={totp} inputmode="numeric" autocomplete="one-time-code" maxlength="6" required /></label>
		<button type="button" class="link" onclick={() => (useRecovery = true)}>{t('login.use_recovery')}</button>
	{:else}
		<label class="field"><span class="label">{t('login.recovery')}</span>
			<input bind:value={recovery} autocomplete="off" required /></label>
	{/if}
	{#if error}<p class="error-text" role="alert">{error}</p>{/if}
	<button class="primary wide" disabled={busy}>{step === 'password' ? t('common.continue') : t('login.sign_in')}</button>
</form>
