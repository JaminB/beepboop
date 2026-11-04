import { describe, expect, it } from 'vitest';
import { bytes, secondsLeft, speakerChip, waterfall } from './format';
import { strength } from './password';
import { groupKey } from './qr';

describe('formatting', () => {
	it('speaker chips always show the percentage', () => {
		expect(speakerChip('Sam', 0.876)).toBe('Sam · 88 %');
		expect(speakerChip('Alex', null)).toBe('Alex · typed');
	});
	it('waterfall bars are relative to the slowest stage and skip non-numbers', () => {
		expect(waterfall({ endpoint: 250, stt_tail: 100, note: 'x' })).toEqual([
			{ stage: 'endpoint', ms: 250, pct: 100 },
			{ stage: 'stt_tail', ms: 100, pct: 40 }
		]);
	});
	it('counts down and never goes negative', () => {
		const now = new Date('2026-11-04T12:00:00Z');
		expect(secondsLeft('2026-11-04T12:00:46.200Z', now)).toBe(47);
		expect(secondsLeft('2026-11-04T11:59:00Z', now)).toBe(0);
	});
	it('bytes and keys', () => {
		expect(bytes(41_000_000)).toBe('41 MB');
		expect(bytes(null)).toBe('—');
		expect(groupKey('ABCDEFGHIJ')).toBe('ABCD-EFGH-IJ');
	});
	it('password strength matches the 12-character rule', () => {
		expect(strength('short')).toBe(0);
		expect(strength('elevenchars')).toBe(1);
		expect(strength('twelve chars')).toBeGreaterThanOrEqual(2);
		expect(strength('correct horse battery staple')).toBe(4);
	});
});
