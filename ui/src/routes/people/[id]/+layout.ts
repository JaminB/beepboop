import { error } from '@sveltejs/kit';
import { api } from '#lib/api/client.ts';

export async function load({ params }) {
	const { data } = await api.GET('/api/profiles/{profile_id}', { params: { path: { profile_id: params.id } } });
	if (!data) error(404, 'not found');
	return { person: data };
}
