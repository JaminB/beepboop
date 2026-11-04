// SchemaForm's engine (WEB_UI §9): the MODULES §6.1 JSON Schema subset → fields, defaults
// and client-side validation. The server validates again with the same schema.

export type JsonSchema = {
	type?: string | string[];
	title?: string;
	description?: string;
	default?: unknown;
	enum?: unknown[];
	format?: string;
	minimum?: number;
	maximum?: number;
	multipleOf?: number;
	maxLength?: number;
	maxItems?: number;
	items?: JsonSchema;
	properties?: Record<string, JsonSchema>;
	required?: string[];
	examples?: unknown[];
	'x-enum-labels'?: Record<string, string>;
	'x-advanced'?: boolean;
	'x-per-profile'?: boolean;
	'x-order'?: number;
	'x-entity'?: { domains?: string[] };
};

export type Widget =
	| 'text'
	| 'url'
	| 'email'
	| 'time'
	| 'date'
	| 'duration'
	| 'select'
	| 'number'
	| 'range'
	| 'toggle'
	| 'tags'
	| 'multiselect'
	| 'entity'
	| 'group';

export interface Field {
	key: string;
	widget: Widget;
	title: string;
	description?: string;
	required: boolean;
	advanced: boolean;
	perProfile: boolean;
	options: { value: string; label: string }[];
	min?: number;
	max?: number;
	step?: number;
	maxLength?: number;
	maxItems?: number;
	placeholder?: string;
	default?: unknown;
	children: Field[];
}

function typeOf(s: JsonSchema): string {
	return Array.isArray(s.type) ? (s.type.find((x) => x !== 'null') ?? 'string') : (s.type ?? 'string');
}

function widgetFor(s: JsonSchema): Widget {
	const type = typeOf(s);
	if (s['x-entity']) return 'entity';
	if (type === 'object') return 'group';
	if (type === 'boolean') return 'toggle';
	if (type === 'array') return s.items?.enum ? 'multiselect' : 'tags';
	if (type === 'integer' || type === 'number')
		return s.minimum !== undefined && s.maximum !== undefined ? 'range' : 'number';
	if (s.enum) return 'select';
	switch (s.format) {
		case 'uri':
			return 'url';
		case 'email':
			return 'email';
		case 'time':
			return 'time';
		case 'date':
			return 'date';
		case 'duration':
			return 'duration';
		default:
			return 'text';
	}
}

export function fields(schema: JsonSchema): Field[] {
	const props = schema.properties ?? {};
	const required = new Set(schema.required ?? []);
	const keys = Object.keys(props);
	const ordered = keys
		.map((key, i) => ({ key, i, order: props[key]?.['x-order'] ?? Number.MAX_SAFE_INTEGER }))
		.sort((a, b) => a.order - b.order || a.i - b.i);
	return ordered.map(({ key }) => {
		const s = props[key] ?? {};
		const widget = widgetFor(s);
		const enumValues = s.enum ?? s.items?.enum ?? [];
		const labels = s['x-enum-labels'] ?? {};
		return {
			key,
			widget,
			title: s.title ?? key,
			description: s.description,
			required: required.has(key),
			advanced: s['x-advanced'] === true,
			perProfile: s['x-per-profile'] === true,
			options: enumValues.map((v) => ({ value: String(v), label: labels[String(v)] ?? String(v) })),
			min: s.minimum,
			max: s.maximum,
			step: s.multipleOf ?? (typeOf(s) === 'integer' ? 1 : undefined),
			maxLength: s.maxLength,
			maxItems: s.maxItems,
			placeholder: s.examples?.[0] === undefined ? undefined : String(s.examples[0]),
			default: s.default,
			children: widget === 'group' ? fields(s) : []
		};
	});
}

export function defaults(schema: JsonSchema, stored: Record<string, unknown> = {}): Record<string, unknown> {
	const out: Record<string, unknown> = {};
	for (const [key, s] of Object.entries(schema.properties ?? {})) {
		if (key in stored) out[key] = stored[key];
		else if (typeOf(s) === 'object') out[key] = defaults(s, {});
		else if (s.default !== undefined) out[key] = s.default;
	}
	return out;
}

const URL_RE = /^https?:\/\/[^\s/$.?#].[^\s]*$/i;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Field errors keyed like the server's `fields` (dotted for one nesting level). */
export function validate(schema: JsonSchema, values: Record<string, unknown>, prefix = ''): Record<string, string> {
	const errors: Record<string, string> = {};
	const props = schema.properties ?? {};
	for (const key of schema.required ?? []) {
		const v = values[key];
		if (v === undefined || v === null || v === '') errors[prefix + key] = 'required';
	}
	for (const [key, s] of Object.entries(props)) {
		const v = values[key];
		const path = prefix + key;
		if (v === undefined || v === null || v === '' || errors[path]) continue;
		const type = typeOf(s);
		if ((type === 'integer' || type === 'number') && typeof v === 'number') {
			if (type === 'integer' && !Number.isInteger(v)) errors[path] = 'not_integer';
			else if (s.minimum !== undefined && v < s.minimum) errors[path] = 'too_small';
			else if (s.maximum !== undefined && v > s.maximum) errors[path] = 'too_large';
		} else if (type === 'integer' || type === 'number') {
			errors[path] = 'not_number';
		} else if (type === 'string' && typeof v === 'string') {
			if (s.enum && !s.enum.includes(v)) errors[path] = 'not_allowed';
			else if (s.maxLength !== undefined && v.length > s.maxLength) errors[path] = 'too_long';
			else if (s.format === 'uri' && !URL_RE.test(v)) errors[path] = 'bad_url';
			else if (s.format === 'email' && !EMAIL_RE.test(v)) errors[path] = 'bad_email';
			else if (s.format === 'time' && !TIME_RE.test(v)) errors[path] = 'bad_time';
			else if (s.format === 'date' && !DATE_RE.test(v)) errors[path] = 'bad_date';
		} else if (type === 'array' && Array.isArray(v)) {
			if (s.maxItems !== undefined && v.length > s.maxItems) errors[path] = 'too_many';
		} else if (type === 'object' && typeof v === 'object') {
			Object.assign(errors, validate(s, v as Record<string, unknown>, `${path}.`));
		}
	}
	return errors;
}
