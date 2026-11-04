<script lang="ts">
	import { page } from '$app/state';
	let { items, label }: { items: { href: string; text: string }[]; label: string } = $props();
</script>

<nav class="tabs" aria-label={label}>
	{#each items as item (item.href)}
		<a href={item.href} aria-current={page.url.pathname === item.href ? 'page' : undefined}>{item.text}</a>
	{/each}
</nav>

<style>
	.tabs {
		display: flex;
		gap: var(--bb-space-1);
		overflow-x: auto;
		margin-bottom: var(--bb-space-4);
		border-bottom: 1px solid var(--bb-border);
	}
	a {
		padding: var(--bb-space-2) var(--bb-space-3);
		min-height: 2.75rem;
		display: inline-flex;
		align-items: center;
		color: var(--bb-muted);
		text-decoration: none;
		white-space: nowrap;
	}
	a[aria-current='page'] {
		color: var(--bb-text);
		border-bottom: 3px solid var(--bb-primary);
		font-weight: 600;
	}
</style>
