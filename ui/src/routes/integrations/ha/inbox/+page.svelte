<script lang="ts">
	// New-device inbox (WEB_UI §5.5, IDENTITY_AND_TRUST §6.2): one radio group per row, one
	// Apply for the page; nobody can use a device until an admin decides. Undo from a toast.
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { errorText, t, tx } from '#lib/i18n.ts';
	import Chip from '#lib/components/Chip.svelte';
	import EmptyState from '#lib/components/EmptyState.svelte';

	type Choice = 'everyone' | 'adults' | 'only' | 'ignore';
	type Risk = 'read' | 'act' | 'personal' | 'critical';
	let items = $state<Schemas['InboxItem'][]>([]);
	let people = $state<Schemas['ProfileView'][]>([]);
	let choice = $state<Record<string, Choice>>({});
	let only = $state<Record<string, string[]>>({});
	let risk = $state<Record<string, Risk>>({});
	let error = $state('');

	async function load() {
		items = (await api.GET('/api/integrations/ha/inbox')).data ?? [];
		people = ((await api.GET('/api/profiles')).data ?? []).filter((p) => p.kind !== 'guest');
		choice = Object.fromEntries(items.map((i) => [i.uuid, i.default_choice]));
		risk = Object.fromEntries(items.map((i) => [i.uuid, i.risk]));
		only = Object.fromEntries(items.map((i) => [i.uuid, []]));
	}
	$effect(() => {
		void load();
	});

	function riskOptions(item: Schemas['InboxItem']): Risk[] {
		const all: Risk[] = ['read', 'act', 'personal', 'critical'];
		return item.lowerable ? all : all.slice(all.indexOf(item.risk));
	}

	async function apply() {
		error = '';
		const decisions = items.map((i) => ({
			uuid: i.uuid,
			choice: choice[i.uuid] ?? i.default_choice,
			profile_ids: only[i.uuid] ?? [],
			risk: risk[i.uuid] ?? i.risk
		}));
		const { data, error: err, response } = await api.POST('/api/integrations/ha/inbox', { body: { decisions } });
		if (!response.ok || !data) {
			error = errorText(toProblem(err, response).code);
			return;
		}
		const uuids = decisions.map((d) => d.uuid);
		await load();
		toast.show(t('inbox.applied', { count: uuids.length }), async () => {
			await api.POST('/api/integrations/ha/inbox/undo', { body: { uuids } });
			await load();
		});
	}
</script>

<h1>{t('inbox.title')}</h1>
{#if items.length === 0}
	<EmptyState text={t('inbox.empty')} />
{:else}
	<p class="help">{t('inbox.help')}</p>
	{#each items as item (item.uuid)}
		<fieldset class="card">
			<legend class="spread"><strong>{item.name}</strong> <span class="muted">{item.domain}</span></legend>
			<label class="row risk">{t('inbox.risk')}
				<select bind:value={risk[item.uuid]} aria-label={t('inbox.risk_for', { name: item.name })}>
					{#each riskOptions(item) as r (r)}<option value={r}>{r}</option>{/each}
				</select>
				<Chip kind="risk" value={risk[item.uuid] ?? item.risk} />
			</label>
			{#if item.lowerable}<p class="help">{t('inbox.indirect')}</p>{/if}
			<div class="row">
				{#each ['everyone', 'adults', 'only', 'ignore'] as const as c (c)}
					<label class="row"><input type="radio" name="c-{item.uuid}" value={c} bind:group={choice[item.uuid]} /> {tx(`inbox.${c}`)}</label>
				{/each}
			</div>
			{#if choice[item.uuid] === 'only'}
				<div class="row">
					{#each people as p (p.id)}
						<label class="row"><input type="checkbox" value={p.id} bind:group={only[item.uuid]} /> {p.name}</label>
					{/each}
				</div>
			{/if}
		</fieldset>
	{/each}
	{#if error}<p class="error-text" role="alert">{error}</p>{/if}
	<button class="primary wide" onclick={apply}>{t('inbox.apply')}</button>
{/if}

<style>
	legend {
		width: 100%;
	}
	.risk select {
		width: auto;
	}
</style>
