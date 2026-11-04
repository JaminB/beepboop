<script lang="ts">
	import type { Snippet } from 'svelte';
	let { text, children }: { text: string; children?: Snippet } = $props();
</script>

<div class="empty card">
	<p>{text}</p>
	{@render children?.()}
</div>

<style>
	.empty {
		text-align: center;
		color: var(--bb-muted);
	}
</style>
