<script lang="ts">
	import { session } from '#lib/session.svelte.ts';
	import { t } from '#lib/i18n.ts';
</script>

<h1>{t('nav.more')}</h1>
<ul class="plain">
	{#if session.me?.kind !== 'child'}<li><a href="/integrations">{t('nav.integrations')}</a></li>{/if}
	{#if session.me?.kind === 'admin' || session.me?.kind === 'member'}<li><a href="/system">{t('nav.system')}</a></li>{/if}
	<li><a href="/me">{t('nav.me')}</a></li>
</ul>
