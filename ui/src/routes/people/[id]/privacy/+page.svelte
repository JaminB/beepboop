<script lang="ts">
	// Privacy ledger (WEB_UI §5.10): what I keep, what left this house, export, delete.
	import { goto } from '$app/navigation';
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { errorText, t, tx } from '#lib/i18n.ts';
	import ConfirmDialog from '#lib/components/ConfirmDialog.svelte';

	let { data } = $props();
	const p = $derived(data.person);
	let ledger = $state<Schemas['Ledger'] | null>(null);
	let confirmMe = $state(false);
	let confirmTranscripts = $state(false);
	let error = $state('');
	const canAct = $derived(session.isAdmin || session.me?.profile_id === p.id);

	async function load() {
		const { data: l } = await api.GET('/api/privacy/{profile_id}', { params: { path: { profile_id: p.id } } });
		ledger = l ?? null;
	}
	$effect(() => {
		void load();
	});

	async function exportZip() {
		const { data: ready, error: err, response } = await api.POST('/api/privacy/{profile_id}/export', {
			params: { path: { profile_id: p.id } }
		});
		if (!response.ok || !ready) {
			error = errorText(toProblem(err, response).code);
			return;
		}
		const a = document.createElement('a');
		a.href = `/api/privacy/${p.id}/export/${ready.export_id}`;
		a.download = ready.filename;
		a.click();
	}
	async function deleteTranscripts() {
		await api.DELETE('/api/privacy/{profile_id}/transcripts', { params: { path: { profile_id: p.id } } });
		toast.show(t('privacy.transcripts_deleted'));
		await load();
	}
	async function deleteMe() {
		const { error: err, response } = await api.DELETE('/api/profiles/{profile_id}', {
			params: { path: { profile_id: p.id }, query: { confirm: p.name } }
		});
		if (!response.ok) {
			error = errorText(toProblem(err, response).code);
			return;
		}
		if (session.me?.profile_id === p.id) location.assign('/login');
		else await goto('/people');
	}
</script>

{#if ledger}
	<h2>{t('privacy.kept')}</h2>
	<ul class="plain">
		{#each ledger.kept as k (k.key)}
			<li class="spread">
				<span>{tx(`privacy.kept_${k.key}`)}</span>
				<span class="muted">{k.key === 'audio' ? t('privacy.no_audio') : t('privacy.count', { count: k.count, detail: k.detail })}</span>
			</li>
		{/each}
	</ul>
	{#if canAct}<button onclick={() => (confirmTranscripts = true)}>{t('privacy.delete_transcripts')}</button>{/if}

	<h2>{t('privacy.left')}</h2>
	<ul class="plain">
		{#each ledger.left as l, i (i)}
			<li>
				<div class="spread"><strong>{l.destination}</strong><span class="muted">{t('privacy.times', { count: l.count })}</span></div>
				<details><summary>{t('privacy.why')}</summary><p class="help">{l.purpose}{l.data ? ` · ${l.data}` : ''}</p></details>
			</li>
		{:else}
			<li class="muted">{t('privacy.nothing_left')}</li>
		{/each}
	</ul>

	{#if error}<p class="error-text" role="alert">{error}</p>{/if}
	{#if canAct}
		<div class="stack actions">
			<button class="primary" onclick={exportZip}>{t('privacy.export')}</button>
			{#if p.kind !== 'guest'}<button class="danger" onclick={() => (confirmMe = true)}>{t('privacy.delete_me', { name: p.name })}</button>{/if}
		</div>
		<ConfirmDialog bind:open={confirmTranscripts} title={t('privacy.delete_transcripts')} body={t('privacy.delete_transcripts_body')} action={t('privacy.delete_transcripts')} onconfirm={deleteTranscripts} />
		<ConfirmDialog bind:open={confirmMe} title={t('privacy.delete_me', { name: p.name })} body={t('privacy.delete_me_body')} action={t('privacy.delete_me', { name: p.name })} typed={p.name} onconfirm={deleteMe} />
	{/if}
{/if}

<style>
	.actions {
		margin-top: var(--bb-space-6);
	}
</style>
