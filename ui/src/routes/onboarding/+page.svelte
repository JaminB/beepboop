<script lang="ts">
	// First-run wizard (WEB_UI §5.1): account → house → LLM → home → voice → household →
	// finish (Recovery kit, practice approval). Resumable: it reopens at the stored step.
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { live } from '#lib/live.svelte.ts';
	import { errorText, t, tx } from '#lib/i18n.ts';
	import { groupKey, qrDataUrl } from '#lib/qr.ts';
	import { MIN_PASSWORD, strength } from '#lib/password.ts';
	import SchemaForm from '#lib/components/SchemaForm.svelte';
	import type { JsonSchema } from '#lib/schemaform.ts';

	type Step = 'account' | 'recover' | 'house' | 'llm' | 'home' | 'voice' | 'household' | 'finish';
	const ORDER: Step[] = ['account', 'house', 'llm', 'home', 'voice', 'household', 'finish'];

	let step = $state<Step>('account');
	let error = $state('');
	let fieldErrors = $state<Record<string, string>>({});
	let busy = $state(false);
	let recoveryCodes = $state<string[]>([]);
	let saved = $state(false);
	let practiceId = $state<string | null>(null);

	// account
	let name = $state('');
	let username = $state('');
	let password = $state('');
	let totpSecret = $state('');
	let totpUri = $state('');
	let totpCode = $state('');
	// recover
	let recoveryCode = $state('');
	// house
	let houseName = $state('Home');
	let timezone = $state(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
	let units = $state<'metric' | 'imperial'>('metric');
	// llm
	let llm = $state<Schemas['LlmView'] | null>(null);
	let llmValues = $state<Record<string, unknown>>({});
	let llmSecrets = $state<Record<string, string>>({});
	// home
	let homeMode = $state<'recommended' | 'one_by_one'>('recommended');

	$effect(() => {
		void start();
	});

	async function start() {
		if (page.url.searchParams.get('recover')) {
			step = 'recover';
			return;
		}
		const { data } = await api.GET('/api/onboarding/state');
		if (session.me?.kind === 'admin' && data) {
			const s = data.step as Step;
			step = data.done ? 'finish' : ORDER.includes(s) && s !== 'account' ? s : 'house';
		} else if (!data?.claimed) {
			await newTotp();
		} else {
			await goto('/login');
		}
		if (step === 'llm') await loadLlm();
	}

	async function newTotp() {
		const { data } = await api.GET('/api/auth/totp/new', { params: { query: { username: username || 'admin' } } });
		if (data) {
			totpSecret = data.secret;
			totpUri = data.uri;
		}
	}

	function fail(err: unknown, response: Response) {
		const p = toProblem(err, response);
		error = errorText(p.code);
		fieldErrors = p.fields ?? {};
	}

	async function go(next: Step) {
		error = '';
		fieldErrors = {};
		step = next;
		if (session.me) await api.PUT('/api/onboarding/step', { body: { step: next === 'finish' ? 'finish' : next } });
		if (next === 'llm') await loadLlm();
	}

	async function createAdmin(e: SubmitEvent) {
		e.preventDefault();
		busy = true;
		const { data, error: err, response } = await api.POST('/api/onboarding/admin', {
			body: { name, username, password, totp_secret: totpSecret, totp_code: totpCode }
		});
		busy = false;
		if (!response.ok || !data) return fail(err, response);
		recoveryCodes = data.recovery_codes;
		session.set(data.me);
		live.start();
		await go('house');
	}

	async function recover(e: SubmitEvent) {
		e.preventDefault();
		const { data, error: err, response } = await api.POST('/api/onboarding/recover', {
			body: { username, recovery_code: recoveryCode }
		});
		if (!response.ok || !data) return fail(err, response);
		session.set(data);
		await goto(`/people/${data.profile_id}`);
	}

	async function saveHouse(e: SubmitEvent) {
		e.preventDefault();
		const { error: err, response } = await api.PUT('/api/onboarding/house', {
			body: { name: houseName, locale: 'en-US', timezone, units }
		});
		if (!response.ok) return fail(err, response);
		await go('llm');
	}

	async function loadLlm() {
		const { data } = await api.GET('/api/system/llm');
		if (data) {
			llm = data;
			llmValues = { ...data.values };
		}
	}

	async function saveLlm() {
		const secrets = Object.fromEntries(Object.entries(llmSecrets).filter(([, v]) => v));
		const { error: err, response } = await api.PUT('/api/system/llm', { body: { values: llmValues, secrets } });
		if (!response.ok) return fail(err, response);
		await go('home');
	}

	async function saveHome() {
		const { error: err, response } = await api.POST('/api/onboarding/home', { body: { mode: homeMode } });
		if (!response.ok) return fail(err, response);
		await go('voice');
	}

	async function practice() {
		const { data, error: err, response } = await api.POST('/api/approvals/practice');
		if (!response.ok || !data) return fail(err, response);
		practiceId = data.id;
		await live.poll();
	}

	async function done() {
		await api.PUT('/api/onboarding/step', { body: { step: 'done' } });
		await goto('/');
	}

	const stepNo = $derived(Math.max(1, ORDER.indexOf(step) + 1));
	const pwScore = $derived(strength(password));
</script>

<header class="spread">
	<h1>{t('onboarding.title')}</h1>
	{#if step !== 'finish' && step !== 'recover'}<span class="muted">{t('onboarding.step', { n: stepNo, total: 6 })}</span>{/if}
</header>

{#if step === 'account'}
	<form class="card" onsubmit={createAdmin}>
		<h2>{t('onboarding.account')}</h2>
		<p class="help">{t('onboarding.account_help')}</p>
		<label class="field"><span class="label">{t('people.name')}</span><input bind:value={name} required autocomplete="name" /></label>
		<label class="field"><span class="label">{t('login.username')}</span>
			<input bind:value={username} required autocomplete="username" autocapitalize="none" onchange={newTotp} />
			{#if fieldErrors.username}<span class="error-text">{errorText(fieldErrors.username)}</span>{/if}</label>
		<label class="field" for="admin-password"><span class="label">{t('login.password')}</span></label>
		<div class="field">
			<input id="admin-password" type="password" bind:value={password} required minlength={MIN_PASSWORD} autocomplete="new-password" />
			<meter min="0" max="4" value={pwScore} aria-label={t('password.strength')}></meter>
			<span class="help">{tx(`password.score_${pwScore}`)}</span>
			{#if fieldErrors.password}<span class="error-text">{errorText(fieldErrors.password)}</span>{/if}
		</div>
		<fieldset class="totp">
			<legend>{t('onboarding.totp')}</legend>
			{#if totpUri}<img src={qrDataUrl(totpUri)} alt={t('onboarding.totp_qr')} width="180" height="180" />{/if}
			<p class="help">{t('onboarding.totp_key')} <span class="mono">{groupKey(totpSecret)}</span></p>
			<label class="field"><span class="label">{t('onboarding.totp_code')}</span>
				<input bind:value={totpCode} inputmode="numeric" autocomplete="one-time-code" maxlength="6" required /></label>
		</fieldset>
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		<button class="primary wide" disabled={busy}>{t('common.continue')}</button>
	</form>
{:else if step === 'recover'}
	<form class="card" onsubmit={recover}>
		<h2>{t('onboarding.recover')}</h2>
		<p class="help">{t('onboarding.recover_help')}</p>
		<label class="field"><span class="label">{t('login.username')}</span><input bind:value={username} required /></label>
		<label class="field"><span class="label">{t('login.recovery')}</span><input bind:value={recoveryCode} required /></label>
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		<button class="primary wide">{t('common.continue')}</button>
	</form>
{:else if step === 'house'}
	<form class="card" onsubmit={saveHouse}>
		<h2>{t('onboarding.house')}</h2>
		<label class="field"><span class="label">{t('onboarding.house_name')}</span><input bind:value={houseName} required /></label>
		<label class="field"><span class="label">{t('onboarding.language')}</span><select disabled><option>English (US)</option></select></label>
		<label class="field"><span class="label">{t('onboarding.timezone')}</span><input bind:value={timezone} required /></label>
		<label class="field"><span class="label">{t('onboarding.units')}</span>
			<select bind:value={units}><option value="metric">{t('onboarding.metric')}</option><option value="imperial">{t('onboarding.imperial')}</option></select></label>
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		<button class="primary wide">{t('common.continue')}</button>
	</form>
{:else if step === 'llm'}
	<section class="card">
		<h2>{t('onboarding.llm')}</h2>
		<p class="help">{t('onboarding.llm_help')}</p>
		{#if llm}
			<SchemaForm schema={llm.settings_schema as JsonSchema} bind:values={llmValues} errors={fieldErrors} idPrefix="llm" />
			{#each llm.secrets as s (s.name)}
				<label class="field"><span class="label">{s.title}</span>
					<input type="password" autocomplete="off" placeholder={s.set ? t('secrets.set') : ''} bind:value={llmSecrets[s.name]} /></label>
			{/each}
		{/if}
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		<div class="row"><button class="primary" onclick={saveLlm}>{t('common.save')}</button><button onclick={() => go('home')}>{t('onboarding.skip')}</button></div>
	</section>
{:else if step === 'home'}
	<section class="card">
		<h2>{t('onboarding.home')}</h2>
		<label class="row"><input type="radio" bind:group={homeMode} value="recommended" /> {t('onboarding.home_recommended')}</label>
		<label class="row"><input type="radio" bind:group={homeMode} value="one_by_one" /> {t('onboarding.home_one_by_one')}</label>
		<p class="help">{t('onboarding.home_critical')}</p>
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		<div class="row"><button class="primary" onclick={saveHome}>{t('common.continue')}</button><button onclick={() => go('voice')}>{t('onboarding.skip')}</button></div>
	</section>
{:else if step === 'voice'}
	<section class="card">
		<h2>{t('onboarding.voice')}</h2>
		<p>{t('onboarding.voice_help')}</p>
		<div class="row">
			{#if session.me}<a class="button primary" href="/people/{session.me.profile_id}/voice">{t('voice.start')}</a>{/if}
			<button onclick={() => go('household')}>{t('onboarding.later')}</button>
		</div>
	</section>
{:else if step === 'household'}
	<section class="card">
		<h2>{t('onboarding.household')}</h2>
		<p>{t('onboarding.household_help')}</p>
		<div class="row"><a class="button" href="/people/new">{t('people.add')}</a><button class="primary" onclick={() => go('finish')}>{t('common.continue')}</button></div>
	</section>
{:else}
	<section class="card kit">
		<h2>{t('onboarding.kit')}</h2>
		{#if recoveryCodes.length}
			<p>{t('onboarding.kit_help')}</p>
			<ol class="codes mono">{#each recoveryCodes as c (c)}<li>{c}</li>{/each}</ol>
			<button onclick={() => window.print()}>{t('onboarding.print')}</button>
			<label class="row"><input type="checkbox" bind:checked={saved} /> {t('onboarding.saved')}</label>
		{:else}
			<p class="help">{t('onboarding.kit_gone')}</p>
		{/if}
		<p class="help">{t('onboarding.kit_backup')}</p>
	</section>
	<section class="card">
		<h2>{t('onboarding.practice')}</h2>
		<p>{t('onboarding.practice_help')}</p>
		<button onclick={practice} disabled={practiceId !== null}>{t('onboarding.try')}</button>
	</section>
	<section class="card">
		<p>{t('onboarding.say')}</p>
		<button class="primary wide" disabled={recoveryCodes.length > 0 && !saved} onclick={done}>{t('onboarding.done')}</button>
	</section>
{/if}

<style>
	.totp {
		border: 1px solid var(--bb-border);
		border-radius: var(--bb-radius-sm);
		margin-bottom: var(--bb-space-4);
	}
	.totp img {
		image-rendering: pixelated;
		background: #fff;
		padding: 8px;
	}
	.codes {
		columns: 2;
		font-size: 1.1rem;
	}
	meter {
		width: 100%;
	}
	@media print {
		:global(header.top),
		:global(nav),
		:global(.approval-banner) {
			display: none;
		}
	}
</style>
