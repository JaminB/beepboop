// One toast at a time, bottom-centre, 6 s, with Undo where applicable (WEB_UI §4, §1.8).
export interface Toast {
	id: number;
	text: string;
	undo?: () => void | Promise<void>;
}

export const TOAST_MS = 6000;

class ToastState {
	current = $state<Toast | null>(null);
	#timer: ReturnType<typeof setTimeout> | undefined;
	#next = 0;

	show(text: string, undo?: () => void | Promise<void>): void {
		clearTimeout(this.#timer);
		this.current = { id: ++this.#next, text, undo };
		this.#timer = setTimeout(() => (this.current = null), TOAST_MS);
	}

	dismiss(): void {
		clearTimeout(this.#timer);
		this.current = null;
	}
}

export const toast = new ToastState();
