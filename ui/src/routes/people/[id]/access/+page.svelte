<script lang="ts">
	// Access tab (WEB_UI §5.5): every toggle writes or deletes one grants row. Group grants
	// show as inherited rows with a lock. Read-only for non-admins.
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { errorText, t } from '#lib/i18n.ts';
	import Chip from '#lib/components/Chip.svelte';

	let { data } = $props();
	const p = $derived(data.person);
	let access = $state<Schemas['AccessView'] | null>(null);
	let yaml = $state('');
	let error = $state('');
	let resourceType = $state<'tool_glob' | 'ha_entity' | 'ha_area'>('tool_glob');
	let resourceId = $state('');
	let risk = $state<'read' | 'act' | 'personal' | 'critical'>('act');

	async function load() {
		const { data: a } = await api.GET('/api/profiles/{profile_id}/access', { params: { path: { profile_id: p.id } } });
		access = a ?? null;
	}
	$effect(() => {
		void load();
	});

	async function add(e: SubmitEvent) {
		e.preventDefault();
		const { data: g, error: err, response } = await api.POST('/api/grants', {
			body: { subject_type: 'profile', subject_id: p.id, resource_type: resourceType, resource_id: resourceId, max_risk: risk }
		});
		if (!response.ok || !g) {
			error = errorText(toProblem(err, response).code);
			return;
		}
		resourceId = '';
		await load();
		toast.show(t('access.added'), async () => {
			await api.DELETE('/api/grants/{grant_id}', { params: { path: { grant_id: g.id } } });
			await load();
		});
	}
	async function remove(g: Schemas['GrantView']) {
		await api.DELETE('/api/grants/{grant_id}', { params: { path: { grant_id: g.id } } });
		await load();
		toast.show(t('access.removed'), async () => {
			await api.POST('/api/grants', {
				body: {
					subject_type: g.subject_type as 'profile' | 'group',
					subject_id: g.subject_id,
					resource_type: g.resource_type as 'tool_glob',
					resource_id: g.resource_id,
					max_risk: g.max_risk as 'act'
				}
			});
			await load();
		});
	}
	async function exportYaml() {
		const res = await fetch(`/api/grants/export/${p.id}`, { credentials: 'same-origin' });
		yaml = await res.text();
	}
</script>

{#if access}
	<p>{t('access.ceiling', { risk: access.risk_ceiling })}</p>
	<ul class="plain">
		{#each access.grants as g (g.id)}
			<li class="spread">
				<span>{g.inherited ? '🔒 ' : ''}{g.label} <span class="muted small">{g.resource_type}{g.schedule ? ` · ${t('access.scheduled')}` : ''}{g.inherited ? ` · ${t('access.via_group', { group: g.subject_id })}` : ''}</span></span>
				<span class="row">
					<Chip kind="risk" value={g.max_risk} />
					{#if access.editable && !g.inherited}<button onclick={() => remove(g)} aria-label={t('access.remove', { what: g.label })}>✕</button>{/if}
				</span>
			</li>
		{:else}
			<li class="muted">{t('access.none')}</li>
		{/each}
	</ul>

	<h2>{t('access.modules')}</h2>
	{#each access.modules as m (m.module_id)}
		<div class="card">
			<strong>{m.module_id}</strong>
			<ul class="plain">
				{#each m.tools as tool (tool.name)}
					<li class="spread"><span class="mono">{tool.name}</span><span class="row"><Chip kind="risk" value={tool.risk} />{tool.allowed ? '✓' : t('access.not_granted')}</span></li>
				{/each}
			</ul>
		</div>
	{/each}

	{#if access.editable}
		<details>
			<summary>{t('access.add')}</summary>
			<form class="card" onsubmit={add}>
				<label class="field"><span class="label">{t('access.resource_type')}</span>
					<select bind:value={resourceType}><option value="tool_glob">tool_glob</option><option value="ha_area">ha_area</option><option value="ha_entity">ha_entity</option></select></label>
				<label class="field"><span class="label">{t('access.resource')}</span><input bind:value={resourceId} required placeholder="timer.*" /></label>
				<label class="field"><span class="label">{t('access.max_risk')}</span>
					<select bind:value={risk}>{#each ['read', 'act', 'personal', 'critical'] as const as r (r)}<option value={r}>{r}</option>{/each}</select></label>
				{#if error}<p class="error-text" role="alert">{error}</p>{/if}
				<button class="primary">{t('access.add')}</button>
			</form>
		</details>
	{/if}
	<details ontoggle={exportYaml}>
		<summary>{t('access.yaml')}</summary>
		<pre class="mono">{yaml}</pre>
	</details>
{/if}

<style>
	.small {
		font-size: 0.85rem;
	}
	pre {
		white-space: pre-wrap;
		font-size: 0.85rem;
	}
</style>
