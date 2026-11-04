import { describe, expect, it } from 'vitest';
import en from '../../messages/en.json';
import { errorText, format, t, tx } from './i18n';

describe('messages', () => {
	it('interpolates and pluralises with ICU syntax', () => {
		expect(format('Hi {name}.', { name: 'Sam' })).toBe('Hi Sam.');
		expect(format('{n, plural, one {# device} other {# devices}}', { n: 1 })).toBe('1 device');
		expect(format('{n, plural, one {# device} other {# devices}}', { n: 3 })).toBe('3 devices');
		expect(format('{n, plural, =0 {none} other {#}}', { n: 0 })).toBe('none');
		expect(format('keep {missing}')).toBe('keep {missing}');
	});

	it('translates keys and stable problem codes', () => {
		expect(t('inbox.found', { count: 2 })).toBe('2 new devices found');
		expect(errorText('last_admin')).toMatch(/last admin/);
		expect(errorText('something_new')).toBe(t('error.unknown'));
		expect(tx('kind.child')).toBe('child');
		expect(tx('no.such.key')).toBe('no.such.key');
	});

	it('follows the tone rules: no exclamation marks, no "simply/just/easily"', () => {
		for (const [key, text] of Object.entries(en)) {
			expect(text, key).not.toMatch(/!/);
			expect(text, key).not.toMatch(/\b(simply|just|easily)\b/i);
		}
	});
});
