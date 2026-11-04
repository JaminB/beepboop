<script lang="ts">
	// System (WEB_UI §5.11): admins see every tab; members see Health only.
	import Tabs from '#lib/components/Tabs.svelte';
	import { session } from '#lib/session.svelte.ts';
	import { t } from '#lib/i18n.ts';

	let { children } = $props();
	const tabs = $derived(
		session.isAdmin
			? [
					{ href: '/system', text: t('system.health') },
					{ href: '/system/llm', text: t('system.llm') },
					{ href: '/system/voice', text: t('system.voice') },
					{ href: '/system/backups', text: t('system.backups') },
					{ href: '/system/updates', text: t('system.updates') },
					{ href: '/system/https', text: t('system.https') },
					{ href: '/system/logs', text: t('system.logs') }
				]
			: [{ href: '/system', text: t('system.health') }]
	);
</script>

<h1>{t('nav.system')}</h1>
<Tabs label={t('nav.system')} items={tabs} />
{@render children()}
