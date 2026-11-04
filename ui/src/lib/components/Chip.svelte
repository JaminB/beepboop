<script lang="ts">
	// Risk chips use the same four words everywhere; the word is always printed, never
	// colour alone (WEB_UI §11.1). Status and speaker chips share the shape.
	let { kind = 'status', value, label }: { kind?: 'risk' | 'status' | 'speaker'; value: string; label?: string } =
		$props();
</script>

<span class="chip {kind} {kind}-{value}">{label ?? value}</span>

<style>
	.chip {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		font-size: 0.8rem;
		padding: 0.1rem 0.5rem;
		border-radius: 999px;
		border: 1px solid currentColor;
		white-space: nowrap;
	}
	.risk-read {
		color: var(--bb-risk-read);
	}
	.risk-act {
		color: var(--bb-risk-act);
	}
	.risk-personal {
		color: var(--bb-risk-personal);
	}
	.risk-critical {
		color: var(--bb-risk-critical);
	}
	.status-ok,
	.status-approved {
		color: var(--bb-ok);
	}
	.status-reauth,
	.status-error,
	.status-denied,
	.status-expired {
		color: var(--bb-danger);
	}
	.status,
	.speaker {
		color: var(--bb-muted);
	}
</style>
