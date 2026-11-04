<script lang="ts">
	// Home is a status page, not a control panel (WEB_UI §5.2).
	import { api, type Schemas } from '#lib/api/client.ts';
	import { live } from '#lib/live.svelte.ts';
	import { t, tx } from '#lib/i18n.ts';
	import { clock, speakerChip } from '#lib/format.ts';

	let home = $state<Schemas['HomeView'] | null>(null);
	$effect(() => {
		void api.GET('/api/home').then(({ data }) => (home = data ?? null));
	});
	const hour = new Date().getHours();
	const greeting = hour < 12 ? 'home.morning' : hour < 18 ? 'home.afternoon' : 'home.evening';

	async function endGuest() {
		await api.DELETE('/api/guest-access');
		home = (await api.GET('/api/home')).data ?? home;
	}
</script>

{#if home}
	<h1>{t(greeting, { name: home.name })}</h1>
	<p class="muted">{tx(`device.${live.device}`)}</p>

	{#if home.attention.length}
		<h2>{t('home.attention')}</h2>
		<ul class="plain">
			{#each home.attention as a, i (i)}
				<li class="spread">
					<span>▲ {tx(`attention.${a.kind}`, { count: a.count, detail: a.detail })}</span>
					{#if a.kind === 'guest_access'}
						<button onclick={endGuest}>{t('guest.end_now')}</button>
					{:else}
						<a class="button" href={a.link}>{tx(`attention.${a.kind}_action`)}</a>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}

	<h2>{t('home.today')}</h2>
	{#if home.recent.length}
		<ul class="plain">
			{#each home.recent as turn (turn.id)}
				<li>
					<a href="/activity/{turn.id}">{clock(turn.created_at)} · {speakerChip(turn.profile_name, turn.confidence)}</a>
					<div class="muted" dir="auto">{turn.transcript ?? t('activity.hidden')}</div>
				</li>
			{/each}
		</ul>
		<a href="/activity">{t('home.see_all')}</a>
	{:else}
		<p class="muted">{t('home.nothing_yet')}</p>
	{/if}
{/if}
