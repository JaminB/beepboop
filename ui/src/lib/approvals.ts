// Pure approval bookkeeping for the banner (WEB_UI §5.7), unit-tested in vitest.
export type ApprovalState = 'pending' | 'approved' | 'denied' | 'expired';

export interface LiveApproval {
	id: string;
	summary: string;
	tool: string;
	exp: string;
	state: ApprovalState;
	nonce?: string;
	factor?: string;
}

/** Merge one SSE `approval` event (or a polled row) into the map; nonces are kept once seen. */
export function mergeApproval(
	map: ReadonlyMap<string, LiveApproval>,
	incoming: LiveApproval
): Map<string, LiveApproval> {
	const next = new Map(map);
	const prev = next.get(incoming.id);
	next.set(incoming.id, { ...prev, ...incoming, nonce: incoming.nonce ?? prev?.nonce });
	return next;
}

/** What the banner shows: live, undecided, unexpired; soonest expiry first. */
export function pendingList(map: ReadonlyMap<string, LiveApproval>, now: Date): LiveApproval[] {
	return [...map.values()]
		.filter((a) => a.state === 'pending' && new Date(a.exp).getTime() > now.getTime())
		.sort((a, b) => new Date(a.exp).getTime() - new Date(b.exp).getTime());
}

/** Summary → button label: "Unlock the front door?" / "Unlock front door" (§11.2). */
export function effectLabel(summary: string): string {
	const s = summary.trim().replace(/[?.!]+$/, '');
	return s ? s[0]!.toUpperCase() + s.slice(1) : s;
}
