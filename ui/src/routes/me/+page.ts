import { redirect } from '@sveltejs/kit';
import { session } from '#lib/session.svelte.ts';

// `/me` is a shortcut to `/people/<your id>` (WEB_UI §3).
export async function load() {
	const me = session.me ?? (await session.load());
	redirect(307, me ? `/people/${me.profile_id}` : '/login?next=/me');
}
