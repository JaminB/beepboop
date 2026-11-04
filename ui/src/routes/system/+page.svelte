<script lang="ts">
	// Health (WEB_UI §5.11, OPERATIONS §11). Pi-only numbers come from host.json; until the
	// host writes them they read "not measured", never a made-up value.
	import { api, type Schemas } from '#lib/api/client.ts';
	import { t } from '#lib/i18n.ts';
	import { bytes, absolute } from '#lib/format.ts';

	let h = $state<Schemas['SystemHealth'] | null>(null);
	$effect(() => {
		void api.GET('/api/system/health').then(({ data }) => (h = data ?? null));
	});
	function ms(v: number | null | undefined): string {
		return v === null || v === undefined ? '—' : `${(v / 1000).toFixed(2)} s`;
	}
</script>

{#if h}
	<dl class="card grid">
		<dt>{t('health.version')}</dt>
		<dd>{h.version} · {t('health.schema', { v: h.schema_version })}</dd>
		<dt>{t('health.host')}</dt>
		<dd>{h.host ? JSON.stringify(h.host) : t('health.not_measured')}</dd>
		<dt>{t('health.disk')}</dt>
		<dd>{bytes(h.disk_free_bytes)} {t('health.free_of')} {bytes(h.disk_total_bytes)} · WAL {bytes(h.wal_bytes)}</dd>
		<dt>{t('health.mic')}</dt>
		<dd>{h.audio === 'ok' ? t('health.ok') : t('health.audio_offline')}</dd>
		<dt>HA</dt>
		<dd>{h.ha}</dd>
		<dt>LLM</dt>
		<dd>{h.llm_provider ?? t('health.no_llm')}</dd>
		<dt>{t('health.modules')}</dt>
		<dd>{h.modules.map((m) => m.module_id).join(', ') || '—'} · {t('health.tools', { count: h.tools })}</dd>
		<dt>{t('health.backup')}</dt>
		<dd>{h.last_backup_at ? absolute(h.last_backup_at) : '—'}</dd>
	</dl>
	<h2>{t('health.latency')}</h2>
	{#if h.latency.length}
		<table class="lat">
			<thead><tr><th>{t('health.stage')}</th><th>p50</th><th>p95</th><th>n</th></tr></thead>
			<tbody>
				{#each h.latency as row (row.stage)}
					<tr><td>{row.stage.replaceAll('_', ' ')}</td><td>{ms(row.p50_ms)}</td><td>{ms(row.p95_ms)}</td><td>{row.n}</td></tr>
				{/each}
			</tbody>
		</table>
	{:else}
		<p class="muted">{t('health.no_turns')}</p>
	{/if}
{/if}

<style>
	.grid {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: var(--bb-space-2) var(--bb-space-4);
	}
	dt {
		color: var(--bb-muted);
	}
	dd {
		margin: 0;
		word-break: break-word;
	}
	.lat {
		width: 100%;
		border-collapse: collapse;
	}
	.lat th,
	.lat td {
		text-align: start;
		padding: var(--bb-space-1) var(--bb-space-2);
		border-bottom: 1px solid var(--bb-border);
	}
</style>
