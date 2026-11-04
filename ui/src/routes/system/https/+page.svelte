<script lang="ts">
	// HTTPS (WEB_UI §5.11, §7): Tier 0 local CA; Tier 1 is v0.2.
	import { api, type Schemas } from '#lib/api/client.ts';
	import { t } from '#lib/i18n.ts';

	let view = $state<Schemas['HttpsView'] | null>(null);
	$effect(() => {
		void api.GET('/api/system/https').then(({ data }) => (view = data ?? null));
	});
</script>

{#if view}
	<div class="card stack">
		<p>{t('https.tier', { tier: view.tier, url: view.canonical_url })}</p>
		<p>{t('https.fingerprint')} <span class="mono">{view.fingerprint ?? t('ca.none')}</span></p>
		<a class="button" href="/ca">{t('https.install_help')}</a>
	</div>
	<h2>{t('https.gates')}</h2>
	<ul class="plain">
		{#each view.admin_gates as g (g.port)}
			<li class="spread"><span>{g.name}</span><span class="mono">:{g.port}</span></li>
		{/each}
	</ul>
	<p class="help">{t('https.tier1')}</p>
{/if}
