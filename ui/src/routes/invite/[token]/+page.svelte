<script lang="ts">
	// Accepting an invite (WEB_UI §5.1b): password + TOTP, then voice, then approvals.
	import { page } from '$app/state';
	import { api, toProblem } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { live } from '#lib/live.svelte.ts';
	import { errorText, t } from '#lib/i18n.ts';
	import { groupKey, qrDataUrl } from '#lib/qr.ts';
	import { MIN_PASSWORD } from '#lib/password.ts';

	const token = page.params.token ?? '';
	let invite = $state<{ name: string; kind: string; totp_required: boolean } | null>(null);
	let invalid = $state(false);
	let step = $state<1 | 2 | 3>(1);
	let username = $state('');
	let password = $state('');
	let secret = $state('');
	let uri = $state('');
	let code = $state('');
	let error = $state('');
	let codes = $state<string[]>([]);

	$effect(() => {
		void (async () => {
			const { data, response } = await api.GET('/api/invites/{token}', { params: { path: { token } } });
			if (!response.ok || !data) {
				invalid = true;
				return;
			}
			invite = data;
			const tot = await api.GET('/api/auth/totp/new', { params: { query: { username: data.name.toLowerCase() } } });
			secret = tot.data?.secret ?? '';
			uri = tot.data?.uri ?? '';
		})();
	});

	async function accept(e: SubmitEvent) {
		e.preventDefault();
		const { data, error: err, response } = await api.POST('/api/invites/{token}/accept', {
			params: { path: { token } },
			body: { username, password, totp_secret: code ? secret : null, totp_code: code || null }
		});
		if (!response.ok || !data) {
			error = errorText(toProblem(err, response).code);
			return;
		}
		codes = data.recovery_codes;
		session.set(data.me);
		live.start();
		step = 2;
	}

	async function practice() {
		await api.POST('/api/approvals/practice');
		await live.poll();
	}
</script>

{#if invalid}
	<div class="card"><p>{t('invite.invalid')}</p></div>
{:else if invite && step === 1}
	<h1>{t('invite.hi', { name: invite.name })}</h1>
	<form class="card" onsubmit={accept}>
		<label class="field"><span class="label">{t('login.username')}</span><input bind:value={username} required autocapitalize="none" autocomplete="username" /></label>
		<label class="field"><span class="label">{t('login.password')}</span><input type="password" bind:value={password} required minlength={MIN_PASSWORD} autocomplete="new-password" /></label>
		<fieldset>
			<legend>{t('onboarding.totp')}{invite.totp_required ? '' : ` (${t('common.optional')})`}</legend>
			{#if uri}<img src={qrDataUrl(uri)} alt={t('onboarding.totp_qr')} width="180" height="180" />{/if}
			<p class="help">{t('onboarding.totp_key')} <span class="mono">{groupKey(secret)}</span></p>
			<label class="field"><span class="label">{t('onboarding.totp_code')}</span>
				<input bind:value={code} inputmode="numeric" maxlength="6" required={invite.totp_required} autocomplete="one-time-code" /></label>
		</fieldset>
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		<button class="primary wide">{t('common.continue')}</button>
	</form>
{:else if step === 2 && session.me}
	<h1>{t('onboarding.voice')}</h1>
	{#if codes.length}
		<div class="card"><p>{t('onboarding.kit_help')}</p><ol class="mono">{#each codes as c (c)}<li>{c}</li>{/each}</ol></div>
	{/if}
	<div class="card row">
		<a class="button primary" href="/people/{session.me.profile_id}/voice">{t('voice.start')}</a>
		<button onclick={() => (step = 3)}>{t('onboarding.later')}</button>
	</div>
{:else if step === 3}
	<h1>{t('onboarding.practice')}</h1>
	<div class="card stack">
		<p>{t('onboarding.practice_help')}</p>
		<button onclick={practice}>{t('onboarding.try')}</button>
		<a class="button primary" href="/">{t('onboarding.done')}</a>
	</div>
{/if}
