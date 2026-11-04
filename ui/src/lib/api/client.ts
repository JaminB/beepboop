// The one API client: generated types (`pnpm gen:api`), never hand-written fetch types.
import createClient from 'openapi-fetch';
import type { components, paths } from './schema';

export type Schemas = components['schemas'];

export const api = createClient<paths>({ baseUrl: '', credentials: 'same-origin' });

/** RFC 9457 problem body (WEB_UI §8); `code` is stable and translated by the UI. */
export interface Problem {
	status: number;
	title: string;
	code?: string;
	detail?: string;
	fields?: Record<string, string>;
	retry_after?: number;
	[key: string]: unknown;
}

export class ApiProblem extends Error {
	constructor(readonly problem: Problem) {
		super(problem.code ?? problem.title);
	}
	get code(): string {
		return this.problem.code ?? `http_${this.problem.status}`;
	}
	get status(): number {
		return this.problem.status;
	}
}

export function toProblem(error: unknown, response: Response): Problem {
	if (error && typeof error === 'object' && 'status' in error) return error as Problem;
	return { status: response.status, title: response.statusText || 'error' };
}

type Result<T> = { data?: T; error?: unknown; response: Response };

/** Unwrap an openapi-fetch result or throw an `ApiProblem`. */
export async function ok<T>(pending: Promise<Result<T>>): Promise<T> {
	const { data, error, response } = await pending;
	if (!response.ok) throw new ApiProblem(toProblem(error, response));
	return data as T;
}
