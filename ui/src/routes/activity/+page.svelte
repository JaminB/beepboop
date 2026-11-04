<script lang="ts">
	// Activity timeline (WEB_UI §5.8): who, outcome badge, transcript where this viewer may see it.
	import { api, type Schemas } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { t } from '#lib/i18n.ts';
	import { clock, speakerChip } from '#lib/format.ts';
	import Chip from '#lib/components/Chip.svelte';
	import Tabs from '#lib/components/Tabs.svelte';
	import EmptyState from '#lib/components/EmptyState.svelte';

	type Outcome = Schemas['TurnView']['outcome'];
	let items = $state<Schemas['TurnView'][]>([]);
	let cursor = $state<string | null>(null);
	let who = $state('me');
	let outcome = $state<Outcome | ''>('');
	let q = $state('');
	let loading = $state(false);

	async function load(reset: boolean) {
		loading = true;
		const { data } = await api.GET('/api/turns', {
			params: {
				query: {
					who,
					outcome: outcome || undefined,
					q: q || undefined,
					cursor: reset ? undefined : (cursor ?? undefined)
				}
			}
		});
		loading = false;
		if (!data) return;
		items = reset ? data.items : [...items, ...data.items];
		cursor = data.next_cursor ?? null;
	}
	$effect(() => {
		void [who, outcome];
		void load(true);
	});
	const OUTCOMES: Outcome[] = ['fast path', 'tool', 'approved', 'denied', 'tainted', 'error', 'offline'];
</script>

<h1>{t('nav.activity')}</h1>
<Tabs
	label={t('nav.activity')}
	items={[
		{ href: '/activity', text: t('activity.timeline') },
		{ href: '/activity/try', text: t('activity.try') }
	]}
/>
<div class="row filters">
	{#if session.isAdmin}
		<select bind:value={who} aria-label={t('activity.who')}>
			<option value="me">{t('activity.me')}</option>
			<option value="everyone">{t('activity.everyone')}</option>
		</select>
	{/if}
	<select bind:value={outcome} aria-label={t('activity.outcome')}>
		<option value="">{t('activity.any_outcome')}</option>
		{#each OUTCOMES as o (o)}<option value={o}>{o}</option>{/each}
	</select>
	<input
		type="search"
		bind:value={q}
		placeholder={t('activity.search')}
		onchange={() => load(true)}
		aria-label={t('activity.search')}
	/>
</div>

{#if items.length === 0 && !loading}
	<EmptyState text={t('activity.empty')}><a class="button" href="/activity/try">{t('activity.try')}</a></EmptyState>
{:else}
	<ul class="plain">
		{#each items as turn (turn.id)}
			<li>
				<a href="/activity/{turn.id}" class="spread">
					<span>{clock(turn.created_at)} {speakerChip(turn.profile_name, turn.confidence)}</span>
					<span class="row">
						{#if turn.typed}<Chip value="typed" label={t('activity.typed')} />{/if}
						<Chip value={turn.outcome} />
					</span>
				</a>
				<div class="muted" dir="auto">{turn.transcript ?? t('activity.hidden')}</div>
			</li>
		{/each}
	</ul>
	{#if cursor}<button class="wide" onclick={() => load(false)} disabled={loading}>{t('activity.more')}</button>{/if}
{/if}

<style>
	.filters > * {
		flex: 1 1 8rem;
		width: auto;
	}
	li a {
		color: inherit;
		text-decoration: none;
	}
</style>
