<script lang="ts">
	// Confirm dialogs only for destructive actions (WEB_UI §1.8); typed confirmation when
	// `typed` is given ("Delete me" requires typing the profile name, §5.10).
	import { Dialog } from 'bits-ui';
	import { t } from '#lib/i18n.ts';

	let {
		open = $bindable(false),
		title,
		body,
		action,
		typed,
		onconfirm
	}: {
		open?: boolean;
		title: string;
		body: string;
		action: string;
		typed?: string;
		onconfirm: () => void | Promise<void>;
	} = $props();
	let entered = $state('');
	const ready = $derived(typed === undefined || entered.trim() === typed);
</script>

<Dialog.Root bind:open>
	<Dialog.Portal>
		<Dialog.Overlay class="bb-overlay" />
		<Dialog.Content class="bb-dialog">
			<Dialog.Title>{title}</Dialog.Title>
			<Dialog.Description>{body}</Dialog.Description>
			{#if typed !== undefined}
				<label class="field">
					<span class="label">{t('confirm.type', { name: typed })}</span>
					<input bind:value={entered} autocomplete="off" />
				</label>
			{/if}
			<div class="row">
				<Dialog.Close class="button">{t('common.cancel')}</Dialog.Close>
				<button class="danger" disabled={!ready} onclick={async () => { await onconfirm(); open = false; }}>{action}</button>
			</div>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>

<style>
	:global(.bb-overlay) {
		position: fixed;
		inset: 0;
		background: rgb(0 0 0 / 0.45);
		z-index: 30;
	}
	:global(.bb-dialog) {
		position: fixed;
		inset-inline: var(--bb-space-4);
		top: 20vh;
		max-width: 28rem;
		margin-inline: auto;
		background: var(--bb-surface);
		color: var(--bb-text);
		border-radius: var(--bb-radius);
		padding: var(--bb-space-6);
		z-index: 31;
		display: grid;
		gap: var(--bb-space-3);
	}
</style>
