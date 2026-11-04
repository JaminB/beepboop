<script lang="ts">
	// Above the content on every page while something is pending for the signed-in profile
	// (WEB_UI §4, §5.7). Announced through the shell's polite live region.
	import { live } from '#lib/live.svelte.ts';
	import { t } from '#lib/i18n.ts';
	import { secondsLeft } from '#lib/format.ts';

	const first = $derived(live.pending[0]);
</script>

{#if first}
	<aside class="approval-banner" aria-label={t('banner.label')} data-testid="approval-banner">
		<div>
			<strong>▲ {first.summary}?</strong>
			<div class="small">{t('banner.left', { s: secondsLeft(first.exp, live.now) })}</div>
		</div>
		<a class="button primary" href="/approvals/{first.id}">{t('banner.open')}</a>
	</aside>
{/if}

<style>
	.approval-banner {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--bb-space-3);
		background: var(--bb-warn-bg);
		color: var(--bb-warn-text);
		padding: var(--bb-space-3) var(--bb-space-4);
		border-bottom: 1px solid var(--bb-border);
	}
	.small {
		font-size: 0.9rem;
	}
</style>
