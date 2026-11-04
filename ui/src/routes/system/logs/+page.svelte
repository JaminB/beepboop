<script lang="ts">
	// Logs (WEB_UI §5.11): the last 500 structlog lines (already redacted), with turn links.
	import { api } from '#lib/api/client.ts';
	import { t } from '#lib/i18n.ts';

	let lines = $state<Record<string, unknown>[]>([]);
	$effect(() => {
		void api.GET('/api/system/logs').then(({ data }) => (lines = (data ?? []) as Record<string, unknown>[]));
	});
	function download() {
		const blob = new Blob([JSON.stringify(lines, null, 2)], { type: 'application/json' });
		const a = document.createElement('a');
		a.href = URL.createObjectURL(blob);
		a.download = 'beepboop-logs.json';
		a.click();
	}
</script>

<div class="row"><button onclick={download} disabled={!lines.length}>{t('logs.download')}</button></div>
{#if lines.length}
	<ol class="plain logs mono">
		{#each lines as line, i (i)}
			<li>
				<span class="muted">{String(line.timestamp ?? '')}</span>
				{String(line.level ?? '')}
				{String(line.event ?? '')}
				{#if typeof line.turn_id === 'string'}<a href="/activity/{line.turn_id}">{line.turn_id}</a>{/if}
			</li>
		{/each}
	</ol>
{:else}
	<p class="muted">{t('logs.none')}</p>
{/if}

<style>
	.logs {
		font-size: 0.8rem;
	}
</style>
