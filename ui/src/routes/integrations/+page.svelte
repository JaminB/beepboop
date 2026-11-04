<script lang="ts">
	// Integrations (WEB_UI §5.6): cards straight from manifest.yaml; no per-module UI code.
	// Cards show the manifest title ("Calendar"); the id is in each module's Details.
	import { api, type Schemas } from '#lib/api/client.ts';
	import { live } from '#lib/live.svelte.ts';
	import { session } from '#lib/session.svelte.ts';
	import { t } from '#lib/i18n.ts';
	import Chip from '#lib/components/Chip.svelte';
	import EmptyState from '#lib/components/EmptyState.svelte';

	let cards = $state<Schemas['IntegrationCard'][]>([]);
	let inbox = $state<number | null>(null);
	$effect(() => {
		void api.GET('/api/integrations').then(({ data }) => (cards = data ?? []));
		if (session.isAdmin) void api.GET('/api/integrations/ha/inbox').then(({ data }) => (inbox = data?.length ?? null));
	});
	const inboxCount = $derived(live.inbox ?? inbox);
</script>

<h1>{t('nav.integrations')}</h1>
{#if session.isAdmin && inboxCount}
	<a class="card spread" href="/integrations/ha/inbox">
		<span>{t('inbox.found', { count: inboxCount })}</span><span class="button">{t('inbox.review')}</span>
	</a>
{/if}
{#each cards as c (c.module_id)}
	<a class="card integration" href="/integrations/{c.module_id}">
		<div class="spread"><strong>{c.title}</strong><span class="muted small">{t(c.scope === 'household' ? 'integrations.household' : 'integrations.per_person')}</span></div>
		<div class="row"><Chip value={c.status} /><span class="muted small">{c.summary}</span></div>
	</a>
{:else}
	<EmptyState text={t('integrations.empty')}>
		<p class="help">{t('integrations.empty_next')}</p>
		<a class="button" href="/system">{t('system.health')}</a>
	</EmptyState>
{/each}

<style>
	a.card {
		display: block;
		color: inherit;
		text-decoration: none;
	}
	.small {
		font-size: 0.85rem;
	}
</style>
