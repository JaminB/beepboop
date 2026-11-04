<script lang="ts">
	// Tier 0 CA helper: works without a session and over the untrusted connection (WEB_UI §7).
	import { api } from '#lib/api/client.ts';
	import { t, tx } from '#lib/i18n.ts';

	let fp = $state<string | null>(null);
	let download = $state<string | null>(null);
	$effect(() => {
		void api.GET('/api/ca').then(({ data }) => {
			fp = data?.fingerprint ?? null;
			download = data?.download ?? null;
		});
	});
	const ua = typeof navigator === 'undefined' ? '' : navigator.userAgent;
	const os = /iPhone|iPad/.test(ua) ? 'ios' : /Android/.test(ua) ? 'android' : /Mac OS/.test(ua) ? 'macos' : /Windows/.test(ua) ? 'windows' : 'linux';
</script>

<h1>{t('ca.title')}</h1>
<div class="card stack">
	<p>{t('ca.compare')}</p>
	<p class="mono fp">{fp ?? t('ca.none')}</p>
	{#if download}<a class="button primary" href={download}>{t('ca.download')}</a>{/if}
	<p class="help">{tx(`ca.help_${os}`)}</p>
</div>

<style>
	.fp {
		font-size: 1.4rem;
		letter-spacing: 0.05em;
	}
</style>
