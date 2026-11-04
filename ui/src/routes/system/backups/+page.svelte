<script lang="ts">
	// Backups (WEB_UI §5.11, DATA_AND_PRIVACY §11): the standing amber state while backups
	// are only on this disk.
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { errorText, t } from '#lib/i18n.ts';
	import { absolute, bytes } from '#lib/format.ts';

	let view = $state<Schemas['BackupsView'] | null>(null);
	let busy = $state(false);
	let error = $state('');
	$effect(() => {
		void api.GET('/api/system/backups').then(({ data }) => (view = data ?? null));
	});
	async function backUpNow() {
		busy = true;
		const { data, error: err, response } = await api.POST('/api/system/backups');
		busy = false;
		if (!response.ok || !data) {
			error = errorText(toProblem(err, response).code);
			return;
		}
		view = data;
		toast.show(t('backups.done'));
	}
</script>

{#if view}
	{#if view.only_local}<p class="banner-warn">{t('backups.only_local')}</p>{/if}
	<div class="card">
		<p>{t('backups.destinations', { list: view.destinations.join(', '), schedule: view.schedule })}</p>
		<ul class="plain">
			{#each view.backups as b (b.name)}
				<li class="spread"><span>{absolute(b.at)}</span><span class="muted">{bytes(b.bytes)}</span></li>
			{:else}
				<li class="muted">{t('backups.none')}</li>
			{/each}
		</ul>
	</div>
	{#if error}<p class="error-text" role="alert">{error}</p>{/if}
	<button class="primary wide" disabled={busy} onclick={backUpNow}>{t('backups.now')}</button>
{/if}
