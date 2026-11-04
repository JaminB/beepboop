<script lang="ts">
	import { toast } from '#lib/toast.svelte.ts';
	import { t } from '#lib/i18n.ts';

	async function undo() {
		const fn = toast.current?.undo;
		toast.dismiss();
		await fn?.();
	}
</script>

{#if toast.current}
	<div class="toast" role="status">
		<span>{toast.current.text}</span>
		{#if toast.current.undo}<button onclick={undo}>{t('common.undo')}</button>{/if}
	</div>
{/if}

<style>
	.toast {
		position: fixed;
		inset-inline: 0;
		bottom: 5rem;
		margin-inline: auto;
		width: fit-content;
		max-width: calc(100% - 2rem);
		display: flex;
		align-items: center;
		gap: var(--bb-space-3);
		background: var(--bb-text);
		color: var(--bb-bg);
		padding: var(--bb-space-2) var(--bb-space-4);
		border-radius: var(--bb-radius);
		z-index: 20;
	}
	.toast button {
		background: transparent;
		color: inherit;
		border-color: currentColor;
	}
</style>
