// The one SSE stream per session (`GET /api/events`, WEB_UI §8) and what it feeds: the
// approvals banner, the inbox count, the device status dot and enrolment progress. While
// the page is visible the approvals list is also polled every 10 s in case the stream is
// reconnecting (§5.7); nothing polls in the background.
import { api } from './api/client';
import { mergeApproval, pendingList, type LiveApproval } from './approvals';

export type DeviceState =
	| 'idle'
	| 'listening'
	| 'thinking'
	| 'speaking'
	| 'muted'
	| 'pin'
	| 'error'
	| 'offline';

export interface EnrollEvent {
	session_id: string;
	step: string;
	accepted: boolean;
	progress: number;
	total: number;
	done: boolean;
	clip_quality: { snr: number; clipping: number; speech_ratio: number; duration_ms: number };
}

const POLL_MS = 10_000;

class LiveState {
	approvals = $state(new Map<string, LiveApproval>());
	inbox = $state<number | null>(null);
	device = $state<DeviceState>('offline');
	connected = $state(false);
	enroll = $state<EnrollEvent | null>(null);
	now = $state(new Date());
	#source: EventSource | null = null;
	#poll: ReturnType<typeof setInterval> | undefined;
	#tick: ReturnType<typeof setInterval> | undefined;

	get pending(): LiveApproval[] {
		return pendingList(this.approvals, this.now);
	}

	start(): void {
		if (this.#source || typeof EventSource === 'undefined') return;
		const src = new EventSource('/api/events');
		this.#source = src;
		src.onopen = () => (this.connected = true);
		src.onerror = () => (this.connected = false);
		src.addEventListener('approval', (e) => this.#onApproval(JSON.parse((e as MessageEvent).data)));
		src.addEventListener('inbox', (e) => (this.inbox = JSON.parse((e as MessageEvent).data).count));
		src.addEventListener('device', (e) => (this.device = JSON.parse((e as MessageEvent).data).state));
		src.addEventListener('enroll', (e) => (this.enroll = JSON.parse((e as MessageEvent).data)));
		this.#tick = setInterval(() => (this.now = new Date()), 1000);
		this.#poll = setInterval(() => {
			if (document.visibilityState === 'visible') void this.poll();
		}, POLL_MS);
		void this.poll();
	}

	stop(): void {
		this.#source?.close();
		this.#source = null;
		clearInterval(this.#poll);
		clearInterval(this.#tick);
		this.connected = false;
		this.approvals = new Map();
	}

	async poll(): Promise<void> {
		const { data, response } = await api.GET('/api/approvals');
		if (!response.ok || !data) return;
		// Merge only: a decision arrives on the stream; the banner also drops anything past exp.
		let next = this.approvals;
		for (const a of data) {
			next = mergeApproval(next, { id: a.id, summary: a.summary, tool: a.tool, exp: a.exp, state: a.state });
		}
		this.approvals = next;
	}

	#onApproval(data: LiveApproval): void {
		this.approvals = mergeApproval(this.approvals, data);
	}

	get(id: string): LiveApproval | undefined {
		return this.approvals.get(id);
	}

	settle(id: string, state: LiveApproval['state']): void {
		const a = this.approvals.get(id);
		if (a) this.approvals = mergeApproval(this.approvals, { ...a, state });
	}
}

export const live = new LiveState();
