<script lang="ts">
	// Updates are notify-only in v0.1 (WEB_UI §5.11, OPERATIONS §9).
	import { api, type Schemas } from '#lib/api/client.ts';
	import { t } from '#lib/i18n.ts';

	let view = $state<Schemas['UpdatesView'] | null>(null);
	$effect(() => {
		void api.GET('/api/system/updates').then(({ data }) => (view = data ?? null));
	});
</script>

{#if view}
	<div class="card stack">
		<p>{t('updates.current', { version: view.current })}</p>
		<p>{view.available ? t('updates.available', { version: view.available }) : t('updates.none')}</p>
		{#if view.changelog_url}<a href={view.changelog_url} rel="noreferrer">{t('updates.changelog')}</a>{/if}
		<p class="help">{t('updates.how')}</p>
		<pre class="mono">{view.command}</pre>
	</div>
{/if}
