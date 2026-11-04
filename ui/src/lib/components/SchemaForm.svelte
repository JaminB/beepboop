<script lang="ts">
	// The one form engine (WEB_UI §9): module settings, System › LLM, onboarding. Renders the
	// MODULES §6.1 subset; `x-advanced` goes under one fold, `x-order` sorts. Validation runs
	// here with the same schema and again on the server (server errors arrive as `errors`).
	// `x-entity` fields render as a select over `entityOptions` (granted HA entities the page
	// loads from /api/integrations/{id}/entities); `only` limits the form to those keys
	// ("Your settings" on a household module, MODULES §6.3).
	import { Switch } from 'bits-ui';
	import { fields as toFields, type Field, type JsonSchema } from '#lib/schemaform.ts';
	import { errorText, hasError, t } from '#lib/i18n.ts';

	export type EntityOption = { uuid: string; name: string; entity_id: string; domain: string; area_id: string | null };

	let {
		schema,
		values = $bindable({}),
		errors = {},
		idPrefix = 'f',
		entityOptions = [],
		only
	}: {
		schema: JsonSchema;
		values?: Record<string, unknown>;
		errors?: Record<string, string>;
		idPrefix?: string;
		entityOptions?: EntityOption[];
		only?: string[];
	} = $props();

	const all = $derived(toFields(schema).filter((f) => !only || only.includes(f.key)));
	const basic = $derived(all.filter((f) => !f.advanced));
	const advanced = $derived(all.filter((f) => f.advanced));

	function str(v: unknown): string {
		return v === undefined || v === null ? '' : String(v);
	}
	function setValue(key: string, v: unknown) {
		values = { ...values, [key]: v };
	}
	function num(e: Event): number | undefined {
		const raw = (e.currentTarget as HTMLInputElement).value;
		return raw === '' ? undefined : Number(raw);
	}
	function tags(v: unknown): string[] {
		return Array.isArray(v) ? v.map(String) : [];
	}
	function toggleTag(key: string, value: string, on: boolean) {
		const current = tags(values[key]).filter((x) => x !== value);
		setValue(key, on ? [...current, value] : current);
	}
	/** Stable codes are translated; a server validation sentence is shown as it is. */
	function message(code: string | undefined): string | undefined {
		if (!code) return undefined;
		return hasError(`form_${code}`) ? errorText(`form_${code}`) : code;
	}
	function describedBy(id: string, f: Field, err: string | undefined): string | undefined {
		const ids = [f.description ? `${id}-help` : '', err ? `${id}-err` : ''].filter(Boolean);
		return ids.length ? ids.join(' ') : undefined;
	}
	function entityLabel(o: EntityOption): string {
		return o.area_id ? `${o.name} · ${o.area_id.replace(/_/g, ' ')}` : o.name;
	}
</script>

{#snippet help(id: string, f: Field, err: string | undefined)}
	{#if f.description}<span class="help" id="{id}-help">{f.description}</span>{/if}
	{#if err}<span class="error-text" id="{id}-err" role="alert">{err}</span>{/if}
{/snippet}

{#snippet input(f: Field)}
	{@const id = `${idPrefix}-${f.key}`}
	{@const err = message(errors[f.key])}
	{@const aria = describedBy(id, f, err)}
	{#if f.widget === 'toggle'}
		<div class="field toggle">
			<Switch.Root
				{id}
				checked={values[f.key] === true}
				onCheckedChange={(c) => setValue(f.key, c)}
				class="bb-switch"
				aria-describedby={aria}
				aria-invalid={err ? 'true' : undefined}
			>
				<Switch.Thumb class="bb-thumb" />
			</Switch.Root>
			<label for={id} class="label">{f.title}</label>
			<div class="grid-help">{@render help(id, f, err)}</div>
		</div>
	{:else if f.widget === 'group'}
		<fieldset class="group" aria-describedby={aria}>
			<legend>{f.title}</legend>
			{#each f.children as child (child.key)}
				{@const childId = `${id}-${child.key}`}
				{@const childErr = message(errors[`${f.key}.${child.key}`])}
				<label class="field" for={childId}>
					<span class="label">{child.title}{child.required ? ' *' : ''}</span>
					<input
						id={childId}
						value={str((values[f.key] as Record<string, unknown> | undefined)?.[child.key])}
						aria-invalid={childErr ? 'true' : undefined}
						aria-describedby={describedBy(childId, child, childErr)}
						oninput={(e) =>
							setValue(f.key, {
								...((values[f.key] as Record<string, unknown> | undefined) ?? {}),
								[child.key]: (e.currentTarget as HTMLInputElement).value
							})}
					/>
					{@render help(childId, child, childErr)}
				</label>
			{/each}
			{@render help(id, f, err)}
		</fieldset>
	{:else if f.widget === 'multiselect'}
		<fieldset class="group" aria-describedby={aria}>
			<legend>{f.title}{f.required ? ' *' : ''}</legend>
			{#each f.options as o (o.value)}
				<label class="check">
					<input
						type="checkbox"
						checked={tags(values[f.key]).includes(o.value)}
						onchange={(e) => toggleTag(f.key, o.value, (e.currentTarget as HTMLInputElement).checked)}
					/>
					{o.label}
				</label>
			{/each}
			{@render help(id, f, err)}
		</fieldset>
	{:else}
		<label class="field" for={id}>
			<span class="label">{f.title}{f.required ? ' *' : ''}</span>
			{#if f.widget === 'entity'}
				<select {id} value={str(values[f.key])} aria-invalid={err ? 'true' : undefined} aria-describedby={aria}
					onchange={(e) => setValue(f.key, (e.currentTarget as HTMLSelectElement).value || undefined)}>
					<option value="">{t('form.entity_default')}</option>
					{#each entityOptions as o (o.uuid)}<option value={o.uuid}>{entityLabel(o)}</option>{/each}
				</select>
				{#if !entityOptions.length}<span class="help">{t('form.entity_none')}</span>{/if}
			{:else if f.widget === 'select'}
				<select {id} value={str(values[f.key])} aria-invalid={err ? 'true' : undefined} aria-describedby={aria}
					onchange={(e) => setValue(f.key, (e.currentTarget as HTMLSelectElement).value)}>
					{#if !f.required}<option value="">—</option>{/if}
					{#each f.options as o (o.value)}<option value={o.value}>{o.label}</option>{/each}
				</select>
			{:else if f.widget === 'number' || f.widget === 'range'}
				<div class="row">
					{#if f.widget === 'range'}
						<input type="range" min={f.min} max={f.max} step={f.step} value={str(values[f.key] ?? f.min)}
							oninput={(e) => setValue(f.key, num(e))} aria-label={f.title} />
					{/if}
					<input {id} type="number" class="num" min={f.min} max={f.max} step={f.step} value={str(values[f.key])}
						aria-invalid={err ? 'true' : undefined} aria-describedby={aria}
						oninput={(e) => setValue(f.key, num(e))} />
				</div>
			{:else if f.widget === 'tags'}
				<input {id} value={tags(values[f.key]).join(', ')} placeholder={t('form.tags_hint')}
					aria-invalid={err ? 'true' : undefined} aria-describedby={aria}
					onchange={(e) => setValue(f.key, (e.currentTarget as HTMLInputElement).value.split(',').map((s) => s.trim()).filter(Boolean))} />
			{:else}
				<input
					{id}
					type={f.widget === 'url' ? 'url' : f.widget === 'email' ? 'email' : f.widget === 'time' ? 'time' : f.widget === 'date' ? 'date' : 'text'}
					value={str(values[f.key])}
					maxlength={f.maxLength}
					placeholder={f.placeholder}
					aria-invalid={err ? 'true' : undefined}
					aria-describedby={aria}
					oninput={(e) => setValue(f.key, (e.currentTarget as HTMLInputElement).value)}
				/>
			{/if}
			{@render help(id, f, err)}
		</label>
	{/if}
{/snippet}

<div class="schema-form">
	{#each basic as f (f.key)}{@render input(f)}{/each}
	{#if advanced.length}
		<details>
			<summary>{t('common.advanced')}</summary>
			{#each advanced as f (f.key)}{@render input(f)}{/each}
		</details>
	{/if}
</div>

<style>
	.toggle {
		display: grid;
		grid-template-columns: auto 1fr;
		align-items: center;
		gap: var(--bb-space-2) var(--bb-space-3);
		margin-bottom: var(--bb-space-4);
	}
	.toggle .grid-help {
		grid-column: 2;
	}
	.num {
		max-width: 7rem;
	}
	.group {
		border: 1px solid var(--bb-border);
		border-radius: var(--bb-radius-sm);
		margin-bottom: var(--bb-space-4);
	}
	.check {
		display: flex;
		gap: var(--bb-space-2);
		align-items: center;
		min-height: 24px;
	}
	:global(.bb-switch) {
		width: 3rem;
		height: 1.75rem;
		border-radius: 999px;
		border: 1px solid var(--bb-border);
		background: var(--bb-border);
		padding: 2px;
		min-height: 1.75rem;
		justify-content: flex-start;
	}
	:global(.bb-switch[data-state='checked']) {
		background: var(--bb-primary);
		justify-content: flex-end;
	}
	:global(.bb-thumb) {
		display: block;
		width: 1.4rem;
		height: 1.4rem;
		border-radius: 999px;
		background: var(--bb-surface);
	}
</style>
