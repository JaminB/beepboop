// Dates, numbers and relative times via Intl.* (WEB_UI §10). The API never formats text.
import { t } from './i18n';

export type Risk = 'read' | 'act' | 'personal' | 'critical';
export const RISKS: readonly Risk[] = ['read', 'act', 'personal', 'critical'];

export function relativeTime(when: Date | number | string, now: Date = new Date()): string {
	const then = new Date(when).getTime();
	const seconds = Math.round((then - now.getTime()) / 1000);
	const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
	const abs = Math.abs(seconds);
	if (abs < 60) return rtf.format(seconds, 'second');
	if (abs < 3600) return rtf.format(Math.round(seconds / 60), 'minute');
	if (abs < 86400) return rtf.format(Math.round(seconds / 3600), 'hour');
	return rtf.format(Math.round(seconds / 86400), 'day');
}

export function clock(when: Date | number | string): string {
	return new Intl.DateTimeFormat('en', { hour: '2-digit', minute: '2-digit' }).format(new Date(when));
}

export function absolute(when: Date | number | string): string {
	return new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'short' }).format(
		new Date(when)
	);
}

/** Speaker chip, one rule everywhere (WEB_UI §11.1): name + percentage, never hidden. */
export function speakerChip(name: string, confidence: number | null | undefined): string {
	if (confidence === null || confidence === undefined) return t('chip.typed', { name });
	return t('chip.speaker', { name, pct: Math.round(confidence * 100) });
}

export function secondsLeft(exp: string | number | Date, now: Date = new Date()): number {
	return Math.max(0, Math.ceil((new Date(exp).getTime() - now.getTime()) / 1000));
}

export function bytes(n: number | null | undefined): string {
	if (n === null || n === undefined) return '—';
	const units = ['B', 'kB', 'MB', 'GB', 'TB'];
	let v = n;
	let u = 0;
	while (v >= 1000 && u < units.length - 1) {
		v /= 1000;
		u++;
	}
	return `${new Intl.NumberFormat('en', { maximumFractionDigits: v < 10 ? 1 : 0 }).format(v)} ${units[u]}`;
}

/** Waterfall bars: each stage's share of the largest, for HTML/CSS bars (no chart library). */
export function waterfall(stages: Record<string, unknown>): { stage: string; ms: number; pct: number }[] {
	const rows = Object.entries(stages)
		.filter((e): e is [string, number] => typeof e[1] === 'number')
		.map(([stage, ms]) => ({ stage, ms }));
	const max = Math.max(1, ...rows.map((r) => r.ms));
	return rows.map((r) => ({ ...r, ms: Math.round(r.ms), pct: Math.round((r.ms / max) * 100) }));
}
