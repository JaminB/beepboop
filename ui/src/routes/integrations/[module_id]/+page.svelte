<script lang="ts">
	// One integration (WEB_UI §5.6): SchemaForm over manifest.settings, write-only secrets,
	// *Test and save* (saves, then a real health check), "Your settings" for x-per-profile
	// fields on a household module (MODULES §6.3), the Setup guide (SETUP.md), what it can
	// do, where data goes. The heading is the manifest title; the id lives in Details.
	import { page } from '$app/state';
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { errorText, t } from '#lib/i18n.ts';
	import { defaults, validate, type JsonSchema } from '#lib/schemaform.ts';
	import SchemaForm, { type EntityOption } from '#lib/components/SchemaForm.svelte';
	import Chip from '#lib/components/Chip.svelte';
	import ConfirmDialog from '#lib/components/ConfirmDialog.svelte';

	const moduleId = page.params.module_id ?? '';
	let detail = $state<Schemas['IntegrationDetail'] | null>(null);
	let missing = $state(false);
	let values = $state<Record<string, unknown>>({});
	let mine = $state<Record<string, unknown>>({});
	let secrets = $state<Record<string, string>>({});
	let errors = $state<Record<string, string>>({});
	let mineErrors = $state<Record<string, string>>({});
	let entities = $state<EntityOption[]>([]);
	let health = $state('');
	let busy = $state(false);
	let clearing = $state<{ name: string; title: string } | null>(null);
	let clearOpen = $state(false);

	async function load() {
		const { data, response } = await api.GET('/api/integrations/{module_id}', { params: { path: { module_id: moduleId } } });
		if (!response.ok || !data) {
			missing = true;
			return;
		}
		detail = data;
		const schema = data.settings_schema as JsonSchema;
		const conn = data.scope === 'household' ? data.household : data.mine;
		values = defaults(schema, conn?.settings ?? {});
		const perProfile = data.per_profile_keys ?? [];
		if (data.scope === 'household' && perProfile.length) {
			mine = Object.fromEntries(
				perProfile.map((k) => [k, data.mine?.settings?.[k]]).filter(([, v]) => v !== undefined)
			);
		}
		const domains = Object.values(schema.properties ?? {})
			.flatMap((p) => p['x-entity']?.domains ?? [])
			.join(',');
		if (Object.values(schema.properties ?? {}).some((p) => p['x-entity'])) {
			const r = await api.GET('/api/integrations/{module_id}/entities', {
				params: { path: { module_id: moduleId }, query: domains ? { domains } : {} }
			});
			entities = r.data ?? [];
		}
	}
	$effect(() => {
		void load();
	});

	const conn = $derived(detail ? (detail.scope === 'household' ? detail.household : detail.mine) : null);
	const editable = $derived(detail !== null && (detail.scope === 'profile' || session.isAdmin));
	const yourKeys = $derived(detail && detail.scope === 'household' ? (detail.per_profile_keys ?? []) : []);

	async function save(e: SubmitEvent) {
		e.preventDefault();
		if (!detail) return;
		errors = validate(detail.settings_schema as JsonSchema, values);
		if (Object.keys(errors).length) return;
		busy = true;
		const filled = Object.fromEntries(Object.entries(secrets).filter(([, v]) => v));
		const { data, error, response } = await api.PUT('/api/integrations/{module_id}/settings', {
			params: { path: { module_id: moduleId } },
			body: { values, secrets: filled }
		});
		busy = false;
		if (!response.ok || !data) {
			const p = toProblem(error, response);
			errors = p.fields ?? {};
			health = errorText(p.code);
			return;
		}
		secrets = {};
		health = data.last_error ?? t('integrations.health_ok');
		toast.show(t('common.saved'));
		await load();
	}
	async function saveMine(e: SubmitEvent) {
		e.preventDefault();
		if (!detail || !session.me) return;
		const sub = { type: 'object', properties: Object.fromEntries(Object.entries((detail.settings_schema as JsonSchema).properties ?? {}).filter(([k]) => yourKeys.includes(k))) };
		mineErrors = validate(sub, mine);
		if (Object.keys(mineErrors).length) return;
		const { error, response } = await api.PUT('/api/integrations/{module_id}/settings', {
			params: { path: { module_id: moduleId } },
			body: { values: mine, secrets: {}, profile_id: session.me.profile_id }
		});
		if (!response.ok) {
			const p = toProblem(error, response);
			mineErrors = p.fields ?? {};
			toast.show(errorText(p.code));
			return;
		}
		toast.show(t('common.saved'));
		await load();
	}
	async function clearSecret() {
		if (!clearing) return;
		const { error, response } = await api.DELETE('/api/integrations/{module_id}/secrets/{name}', { params: { path: { module_id: moduleId, name: clearing.name } } });
		if (!response.ok) {
			toast.show(errorText(toProblem(error, response).code));
			return;
		}
		toast.show(t('secrets.cleared', { title: clearing.title }));
		clearOpen = false;
		clearing = null;
		await load();
	}
</script>

{#if missing}
	<div class="card"><p>{t('integrations.not_found')}</p><a class="button" href="/integrations">{t('common.back')}</a></div>
{:else if detail}
	<div class="spread"><h1>{detail.title}</h1><span class="muted">v{detail.version}</span></div>
	<p class="muted">{detail.summary}</p>
	{#if detail.module_id === 'ha'}<p><a href="/integrations/ha/inbox">{t('inbox.title')}</a></p>{/if}
	{#if editable && conn}
		<form class="card" onsubmit={save}>
			<div class="spread"><h2>{detail.scope === 'profile' ? t('integrations.your_connection') : t('integrations.settings')}</h2><Chip value={conn.status} /></div>
			<SchemaForm schema={detail.settings_schema as JsonSchema} bind:values {errors} idPrefix={moduleId} entityOptions={entities} />
			{#each conn.secrets as s (s.name)}
				{@const title = s.title ?? s.name}
				<div class="field">
					<label class="field" for="{moduleId}-secret-{s.name}">
						<span class="label">{title}</span>
						<input id="{moduleId}-secret-{s.name}" type="password" autocomplete="off" bind:value={secrets[s.name]} placeholder={s.set ? t('secrets.set') : ''} />
						{#if s.description}<span class="help">{s.description}</span>{/if}
					</label>
					{#if s.set}
						<button type="button" class="link" aria-label={t('secrets.clear_named', { title })} onclick={() => { clearing = { name: s.name, title }; clearOpen = true; }}>{t('secrets.clear_named', { title })}</button>
					{/if}
				</div>
			{/each}
			<button class="primary wide" disabled={busy}>{t('integrations.test_and_save')}</button>
			{#if health}<p class="help" role="status">{t('integrations.health', { detail: health })}</p>{/if}
		</form>
	{:else if !editable}
		<p class="muted">{t('integrations.admin_only')}</p>
	{/if}
	{#if yourKeys.length && session.me && session.me.kind !== 'child'}
		<form class="card" onsubmit={saveMine}>
			<h2>{t('integrations.your_settings')}</h2>
			<p class="help">{t('integrations.your_settings_help')}</p>
			<SchemaForm schema={detail.settings_schema as JsonSchema} bind:values={mine} errors={mineErrors} idPrefix="{moduleId}-mine" only={yourKeys} />
			<button class="primary wide">{t('common.save')}</button>
		</form>
	{/if}
	{#if detail.setup_md}
		<details>
			<summary>{t('integrations.setup_guide')}</summary>
			<pre class="md">{detail.setup_md}</pre>
		</details>
	{/if}
	<details>
		<summary>{t('integrations.can_do', { count: detail.tools.length })}</summary>
		<ul class="plain">{#each detail.tools as tool (tool.name)}<li class="spread"><span class="mono">{tool.name}</span><Chip kind="risk" value={tool.risk} /></li>{/each}</ul>
	</details>
	{#if detail.egress.length}
		<details>
			<summary>{t('integrations.egress')}</summary>
			<ul class="plain">{#each detail.egress as e, i (i)}<li><strong>{e.destination}</strong> · {e.purpose}</li>{/each}</ul>
		</details>
	{/if}
	<details>
		<summary>{t('common.details')}</summary>
		<p class="mono small">{t('integrations.module_id', { id: detail.module_id })} · {detail.scope} · {detail.category}</p>
	</details>
	{#if clearing}
		<ConfirmDialog
			bind:open={clearOpen}
			title={t('secrets.clear_named', { title: clearing.title })}
			body={t('secrets.clear_body', { title: clearing.title })}
			action={t('secrets.clear_named', { title: clearing.title })}
			onconfirm={clearSecret}
		/>
	{/if}
{/if}

<style>
	.md {
		white-space: pre-wrap;
		font: inherit;
		font-size: 0.9rem;
	}
	.small {
		font-size: 0.85rem;
	}
</style>
