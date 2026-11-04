import { describe, expect, it } from 'vitest';
import { effectLabel, mergeApproval, pendingList, type LiveApproval } from './approvals';

const now = new Date('2026-11-04T12:00:00Z');
const a = (id: string, secs: number, extra: Partial<LiveApproval> = {}): LiveApproval => ({
	id,
	summary: 'unlock the front door',
	tool: 'home.set',
	exp: new Date(now.getTime() + secs * 1000).toISOString(),
	state: 'pending',
	...extra
});

describe('approval banner bookkeeping', () => {
	it('keeps the nonce from the SSE event when a polled row (no nonce) arrives', () => {
		let m = mergeApproval(new Map(), a('1', 40, { nonce: 'n1' }));
		m = mergeApproval(m, a('1', 40));
		expect(m.get('1')?.nonce).toBe('n1');
	});

	it('shows only live, undecided approvals, soonest expiry first', () => {
		let m = new Map<string, LiveApproval>();
		m = mergeApproval(m, a('late', 50));
		m = mergeApproval(m, a('soon', 10));
		m = mergeApproval(m, a('gone', -1));
		m = mergeApproval(m, a('done', 30, { state: 'approved' }));
		expect(pendingList(m, now).map((x) => x.id)).toEqual(['soon', 'late']);
	});

	it('names the effect on the button', () => {
		expect(effectLabel('unlock the front door?')).toBe('Unlock the front door');
	});
});
