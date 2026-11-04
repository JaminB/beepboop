<script lang="ts">
	// Add person: preset, name, then an invite link + QR for Admin/Member (WEB_UI §5.3).
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { errorText, t, tx } from '#lib/i18n.ts';
	import { qrDataUrl } from '#lib/qr.ts';

	let preset = $state<'admin' | 'member' | 'child'>('member');
	let name = $state('');
	let quietHours = $state(false);
	let created = $state<Schemas['ProfileCreated'] | null>(null);
	let error = $state('');

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		const { data, error: err, response } = await api.POST('/api/profiles', {
			body: { name, preset, quiet_hours: quietHours, invite: true }
		});
		if (!response.ok || !data) {
			error = errorText(toProblem(err, response).code);
			return;
		}
		created = data;
	}
	async function copy(url: string) {
		await navigator.clipboard.writeText(url);
	}
</script>

<h1>{t('people.add')}</h1>
{#if !created}
	<form class="card" onsubmit={submit}>
		<fieldset class="stack">
			<legend>{t('people.preset')}</legend>
			{#each ['admin', 'member', 'child'] as const as p (p)}
				<label class="row"><input type="radio" bind:group={preset} value={p} /> <span><strong>{tx(`kind.${p}`)}</strong> · <span class="muted">{tx(`preset.${p}`)}</span></span></label>
			{/each}
			<p class="help">{t('preset.guest_hint')} <a href="/people/guest">{t('guest.title')}</a></p>
		</fieldset>
		<label class="field"><span class="label">{t('people.name')}</span><input bind:value={name} required maxlength="64" /></label>
		{#if preset === 'child'}
			<label class="row"><input type="checkbox" bind:checked={quietHours} /> {t('preset.quiet_hours')}</label>
		{/if}
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		<button class="primary wide">{t('people.add')}</button>
	</form>
{:else}
	<div class="card stack">
		<p>{t('people.added', { name: created.profile.name })}</p>
		{#if created.invite}
			<p>{t('people.invite_ready')}</p>
			<img src={qrDataUrl(created.invite.url)} alt={t('people.invite_qr')} width="200" height="200" />
			<p class="mono small">{created.invite.url}</p>
			<button onclick={() => copy(created?.invite?.url ?? '')}>{t('people.copy_link')}</button>
			<p class="help">{t('people.invite_link_once')}</p>
		{/if}
		<a class="button primary" href="/people/{created.profile.id}">{t('people.open', { name: created.profile.name })}</a>
	</div>
{/if}

<style>
	img {
		image-rendering: pixelated;
		background: #fff;
		padding: 8px;
	}
	.small {
		word-break: break-all;
		font-size: 0.85rem;
	}
</style>
