<script lang="ts">
	// The person page: tabs Profile · Voice · Access · Privacy (WEB_UI §3, §5.3-§5.10).
	import Tabs from '#lib/components/Tabs.svelte';
	import { t, tx } from '#lib/i18n.ts';

	let { data, children } = $props();
	const base = $derived(`/people/${data.person.id}`);
</script>

<h1>{data.person.name} <span class="muted kind">{tx(`kind.${data.person.kind}`)}</span></h1>
<Tabs
	label={data.person.name}
	items={[
		{ href: base, text: t('person.profile') },
		{ href: `${base}/voice`, text: t('person.voice') },
		{ href: `${base}/access`, text: t('person.access') },
		{ href: `${base}/privacy`, text: t('person.privacy') }
	]}
/>
{@render children()}

<style>
	.kind {
		font-size: 1rem;
		font-weight: normal;
	}
</style>
