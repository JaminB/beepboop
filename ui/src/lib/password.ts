// The strength meter beside the password field (WEB_UI §5.1). Advisory only: the server
// enforces the real rule (12 characters minimum, common-password list, §6.1).
export const MIN_PASSWORD = 12;

export function strength(pw: string): 0 | 1 | 2 | 3 | 4 {
	if (pw.length < MIN_PASSWORD) return pw.length >= 8 ? 1 : 0;
	const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((r) => r.test(pw)).length;
	const words = pw.trim().split(/\s+/).length;
	let score = 2;
	if (pw.length >= 16 || words >= 3) score++;
	if (classes >= 3 || pw.length >= 20) score++;
	return Math.min(score, 4) as 2 | 3 | 4;
}
