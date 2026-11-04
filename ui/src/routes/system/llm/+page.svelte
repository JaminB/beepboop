<script lang="ts">
	// LLM (WEB_UI §5.11): the provider's settings form; credentials are write-only.
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { errorText, t } from '#lib/i18n.ts';
	import { defaults, validate, type JsonSchema } from '#lib/schemaform.ts';
	import SchemaForm from '#lib/components/SchemaForm.svelte';

	let view = $state<Schemas['LlmView'] | null>(null);
	let values = $state<Record<string, unknown>>({});
	let secrets = $state<Record<string, string>>({});
	let errors = $state<Record<string, string>>({});
	let error = $state('');

	async function load() {
		const { data } = await api.GET('/api/system/llm');
		view = data ?? null;
		if (data) values = defaults(data.settings_schema as JsonSchema, data.values);
	}
	$effect(() => {
		void load();
	});
	async function save(e: SubmitEvent) {
		e.preventDefault();
		if (!view) return;
		errors = validate(view.settings_schema as JsonSchema, values);
		if (Object.keys(errors).length) return;
		const { error: err, response } = await api.PUT('/api/system/llm', {
			body: { values, secrets: Object.fromEntries(Object.entries(secrets).filter(([, v]) => v)) }
		});
		if (!response.ok) {
			const p = toProblem(err, response);
			errors = p.fields ?? {};
			error = errorText(p.code);
			return;
		}
		secrets = {};
		toast.show(t('common.saved'));
		await load();
	}
</script>

{#if view}
	<p class="muted">
		{view.provider_active ? t('llm.active', { provider: view.provider_active }) : t('llm.none_active')}
	</p>
	<form class="card" onsubmit={save}>
		<SchemaForm schema={view.settings_schema as JsonSchema} bind:values {errors} idPrefix="llm" />
		{#each view.secrets as s (s.name)}
			<label class="field">
				<span class="label">{s.title}</span>
				<input type="password" autocomplete="off" bind:value={secrets[s.name]} placeholder={s.set ? t('secrets.set') : ''} />
			</label>
		{/each}
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		<button class="primary wide">{t('common.save')}</button>
	</form>
{/if}
