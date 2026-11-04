<script lang="ts">
	// Timing waterfall from stages_ms_json; HTML/CSS bars, no chart library (WEB_UI §2).
	import { waterfall } from '#lib/format.ts';
	let { stages }: { stages: Record<string, unknown> } = $props();
	const rows = $derived(waterfall(stages));
</script>

<table class="waterfall">
	<tbody>
		{#each rows as row (row.stage)}
			<tr>
				<th scope="row">{row.stage.replaceAll('_', ' ')}</th>
				<td class="bar"><meter min="0" max="100" value={row.pct}>{row.pct}%</meter></td>
				<td class="ms">{row.ms} ms</td>
			</tr>
		{/each}
	</tbody>
</table>

<style>
	.waterfall {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.9rem;
	}
	th {
		text-align: start;
		font-weight: normal;
		color: var(--bb-muted);
		padding-inline-end: var(--bb-space-2);
		white-space: nowrap;
	}
	.bar meter {
		width: 100%;
	}
	.ms {
		text-align: end;
		white-space: nowrap;
		font-family: var(--bb-mono);
	}
</style>
