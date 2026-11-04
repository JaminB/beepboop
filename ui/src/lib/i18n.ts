// UI strings live in messages/<locale>.json and are referenced by key (WEB_UI §10). A key
// that does not exist is a type error, so a missing message fails `pnpm check`.
// Supported syntax: `{name}` interpolation and ICU `{n, plural, one {...} other {...}}`.
import en from '../../messages/en.json';

export type MessageKey = keyof typeof en;
export type Params = Record<string, string | number>;

const catalogs: Record<string, Record<string, string>> = { 'en-US': en, en };
let locale = 'en-US';

export function setLocale(next: string): void {
	locale = next in catalogs ? next : 'en-US';
}

export function format(template: string, params: Params = {}, lang = locale): string {
	let out = '';
	let i = 0;
	while (i < template.length) {
		const open = template.indexOf('{', i);
		if (open < 0) {
			out += template.slice(i);
			break;
		}
		out += template.slice(i, open);
		let depth = 0;
		let close = open;
		for (; close < template.length; close++) {
			if (template[close] === '{') depth++;
			else if (template[close] === '}' && --depth === 0) break;
		}
		out += expand(template.slice(open + 1, close), params, lang);
		i = close + 1;
	}
	return out;
}

function expand(body: string, params: Params, lang: string): string {
	const parts = body.split(',');
	const name = (parts[0] ?? '').trim();
	if (parts.length < 3 || (parts[1] ?? '').trim() !== 'plural') {
		const v = params[name];
		return v === undefined ? `{${name}}` : String(v);
	}
	const n = Number(params[name] ?? 0);
	const rest = parts.slice(2).join(',');
	const options = new Map<string, string>();
	const re = /\s*(=\d+|zero|one|two|few|many|other)\s*\{/g;
	let m: RegExpExecArray | null;
	while ((m = re.exec(rest))) {
		let depth = 1;
		let j = re.lastIndex;
		for (; j < rest.length && depth > 0; j++) {
			if (rest[j] === '{') depth++;
			else if (rest[j] === '}') depth--;
		}
		options.set(m[1] ?? 'other', rest.slice(re.lastIndex, j - 1));
		re.lastIndex = j;
	}
	const chosen =
		options.get(`=${n}`) ??
		options.get(new Intl.PluralRules(lang).select(n)) ??
		options.get('other') ??
		'';
	return format(chosen.replace(/#/g, String(n)), params, lang);
}

export function t(key: MessageKey, params?: Params): string {
	const template = catalogs[locale]?.[key] ?? en[key];
	return format(template, params);
}

/** Translate a stable problem `code` (WEB_UI §8); unknown codes get the generic line. */
export function errorText(code: string | undefined, params?: Params): string {
	const key = `error.${code ?? 'unknown'}`;
	if (key in en) return t(key as MessageKey, params);
	return t('error.unknown');
}

export function hasError(code: string): boolean {
	return `error.${code}` in en;
}

/** A computed key (`kind.${k}`); falls back to the key itself if the catalog lacks it. */
export function tx(key: string, params?: Params): string {
	return key in en ? t(key as MessageKey, params) : key;
}
