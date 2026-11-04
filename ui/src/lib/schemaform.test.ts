import { describe, expect, it } from 'vitest';
import { defaults, fields, validate, type JsonSchema } from './schemaform';

const schema: JsonSchema = {
	type: 'object',
	properties: {
		url: { type: 'string', format: 'uri', title: 'CalDAV URL' },
		calendar: { type: 'string', title: 'Calendar', default: '' },
		lookahead_days: { type: 'integer', minimum: 1, maximum: 30, default: 7, 'x-advanced': true },
		units: { type: 'string', enum: ['c', 'f'], 'x-enum-labels': { c: 'Celsius' }, 'x-order': 0 },
		enabled: { type: 'boolean' },
		names: { type: 'array', items: { type: 'string' }, maxItems: 2 },
		wake: { type: 'string', format: 'time' }
	},
	required: ['url']
};

describe('SchemaForm engine', () => {
	it('maps the MODULES §6.1 subset to widgets, honouring x-order and x-advanced', () => {
		const f = fields(schema);
		expect(f.map((x) => x.key)[0]).toBe('units');
		const by = Object.fromEntries(f.map((x) => [x.key, x]));
		expect(by.url?.widget).toBe('url');
		expect(by.url?.required).toBe(true);
		expect(by.lookahead_days?.widget).toBe('range');
		expect(by.lookahead_days?.advanced).toBe(true);
		expect(by.units?.options).toEqual([
			{ value: 'c', label: 'Celsius' },
			{ value: 'f', label: 'f' }
		]);
		expect(by.enabled?.widget).toBe('toggle');
		expect(by.names?.widget).toBe('tags');
		expect(by.wake?.widget).toBe('time');
	});

	it('fills defaults under stored values', () => {
		expect(defaults(schema, { calendar: 'Work' })).toEqual({ calendar: 'Work', lookahead_days: 7 });
	});

	it('validates like the server', () => {
		expect(validate(schema, {})).toEqual({ url: 'required' });
		expect(
			validate(schema, { url: 'nope', lookahead_days: 99, units: 'k', names: ['a', 'b', 'c'], wake: '25:00' })
		).toEqual({
			url: 'bad_url',
			lookahead_days: 'too_large',
			units: 'not_allowed',
			names: 'too_many',
			wake: 'bad_time'
		});
		expect(validate(schema, { url: 'https://cloud.example.com/dav', lookahead_days: 3 })).toEqual({});
	});
});
