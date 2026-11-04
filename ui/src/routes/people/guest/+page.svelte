<script lang="ts">
	// Guest access (WEB_UI §5.3): scheduled `act` grants for the built-in guest profile,
	// nothing more (IDENTITY_AND_TRUST §9 owns the list).
	import { api, toProblem, type Schemas } from '#lib/api/client.ts';
	import { session } from '#lib/session.svelte.ts';
	import { toast } from '#lib/toast.svelte.ts';
	import { errorText, t } from '#lib/i18n.ts';
	import { absolute } from '#lib/format.ts';

	let view = $state<Schemas['GuestAccessView'] | null>(null);
	let areas = $state<string[]>([]);
	let until = $state('');
	let error = $state('');

	function local(iso: string): string {
		const d = new Date(iso);
		const pad = (n: number) => String(n).padStart(2, '0');
		return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
	}
	async function load() {
		const { data } = await api.GET('/api/guest-access');
		view = data ?? null;
		if (data) {
			areas = [...data.area_ids];
			until = local(data.until ?? data.default_until);
		}
	}
	$effect(() => {
		void load();
	});

	async function start(e: SubmitEvent) {
		e.preventDefault();
		const { error: err, response } = await api.PUT('/api/guest-access', {
			body: {
				area_ids: areas,
				until: until ? new Date(until).toISOString() : null
			}
		});
		if (!response.ok) {
			error = errorText(toProblem(err, response).code);
			return;
		}
		toast.show(t('guest.started'));
		await load();
	}
	async function end() {
		await api.DELETE('/api/guest-access');
		toast.show(t('guest.ended'));
		await load();
	}
</script>

<h1>{t('guest.title')}</h1>
<p class="help">{t('guest.help')}</p>
{#if view}
	{#if view.active}
		<div class="banner-warn spread">
			<span>{t('guest.on_until', { when: absolute(view.until ?? '') })}</span>
			{#if session.isAdmin}<button onclick={end}>{t('guest.end_now')}</button>{/if}
		</div>
	{/if}
	{#if session.isAdmin}
		<form class="card" onsubmit={start}>
			<fieldset>
				<legend>{t('guest.areas')}</legend>
				{#each view.known_areas as a (a)}
					<label class="row"><input type="checkbox" bind:group={areas} value={a} /> {a.replaceAll('_', ' ')}</label>
				{:else}
					<p class="help">{t('guest.no_areas')}</p>
				{/each}
			</fieldset>
			<label class="field"><span class="label">{t('guest.until')}</span><input type="datetime-local" bind:value={until} required /></label>
			{#if error}<p class="error-text" role="alert">{error}</p>{/if}
			<button class="primary wide">{t('guest.turn_on')}</button>
		</form>
	{/if}
{/if}
