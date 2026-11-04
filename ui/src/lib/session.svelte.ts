// The signed-in profile (`GET /api/me`). The shell is static; the session is a
// `SameSite=Strict` cookie the same-origin fetch carries (WEB_UI §6.2).
import { api } from './api/client';
import type { Schemas } from './api/client';
import { setLocale } from './i18n';

export type Me = Schemas['Me'];

class SessionState {
	me = $state<Me | null>(null);
	loaded = $state(false);

	get isAdmin(): boolean {
		return this.me?.kind === 'admin';
	}

	async load(): Promise<Me | null> {
		const { data, response } = await api.GET('/api/me');
		this.me = response.ok && data ? data : null;
		this.loaded = true;
		if (this.me) setLocale(this.me.locale);
		return this.me;
	}

	set(me: Me | null): void {
		this.me = me;
		this.loaded = true;
	}
}

export const session = new SessionState();
