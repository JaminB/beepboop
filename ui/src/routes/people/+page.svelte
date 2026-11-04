<script lang="ts">
	// People (WEB_UI §5.3): everyone in the house, their voice status, and open invites.
	import { api, type Schemas } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { t, tx } from '#lib/i18n.ts';
	import { relativeTime } from '#lib/format.ts';

	let people = $state<Schemas['ProfileView'][]>([]);
	let invites = $state<Schemas['InviteItem'][]>([]);

	async function load() {
		people = (await api.GET('/api/profiles')).data ?? [];
		if (session.isAdmin) invites = (await api.GET('/api/invites')).data ?? [];
	}
	$effect(() => {
		void load();
	});

	async function revoke(id: string) {
		await api.DELETE('/api/invites/{invite_id}', { params: { path: { invite_id: id } } });
		toast.show(t('people.invite_revoked'));
		await load();
	}
	function voiceLine(p: Schemas['ProfileView']): string {
		if (p.kind === 'guest') return t('people.guest_line');
		return p.voice.enrolled ? t('people.voice_on') : t('people.voice_off');
	}
</script>

<div class="spread">
	<h1>{t('nav.people')}</h1>
	{#if session.isAdmin}<a class="button" href="/people/new">+ {t('people.add')}</a>{/if}
</div>
<ul class="plain">
	{#each people as p (p.id)}
		<li>
			<a class="spread" href={p.kind === 'guest' ? '/people/guest' : `/people/${p.id}`}>
				<span>{p.name}</span>
				<span class="muted">{tx(`kind.${p.kind}`)} · {voiceLine(p)}</span>
			</a>
		</li>
	{/each}
</ul>

{#if invites.length}
	<h2>{t('people.invites')}</h2>
	<ul class="plain">
		{#each invites as inv (inv.id)}
			<li class="spread">
				<span>{inv.name} · {tx(`kind.${inv.kind}`)} · {t('people.expires', { when: relativeTime(inv.expires_at) })}</span>
				<button onclick={() => revoke(inv.id)}>{t('people.revoke')}</button>
			</li>
		{/each}
	</ul>
	<p class="help">{t('people.invite_link_once')}</p>
{/if}

<style>
	li a {
		color: inherit;
		text-decoration: none;
	}
</style>
