#!/usr/bin/env python3
"""M0 step 6: the synthetic-only bench for CI (x86, TTS-generated clips, no gate claims).

1. Fetches the pinned models (cached between runs).
2. Generates ``tools/bench/corpus/synthetic/`` with the local Piper voice
   (:mod:`beepboop_bench.synthetic`), flagged ``synthetic: true``.
3. Runs every stage benchmark plus a short concurrent e2e through
   ``beepboop bench all`` and writes
   ``tools/bench/results/<date>-<host>-synthetic.json`` labelled
   "synthetic / x86 / not a gate result".

It never passes ``--gates``: a synthetic corpus can never produce a gate
result, and the CLI refuses anyway. Exit code is 0 when every section ran
(skipped/pending sections are fine; an ``error`` status is not).

    uv run python tools/bench/synthetic/run.py [--small] [--duration 30] [--out results.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from beepboop_bench.models import fetch_all, load_manifest, manifest_path, models_root

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
DEFAULT_CORPUS = BENCH / "corpus" / "synthetic"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--small", action="store_true", help="fewer clips (CI smoke)")
    parser.add_argument("--duration", type=float, default=30.0, help="seconds of concurrent e2e")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="result file (default results/<date>-<host>-synthetic.json)",
    )
    parser.add_argument(
        "--regenerate", action="store_true", help="rebuild the corpus even if present"
    )
    args = parser.parse_args(argv)

    root = models_root()
    manifest = load_manifest(manifest_path(root))
    fetch_all(manifest, root)

    from beepboop_bench import phonemize
    from beepboop_bench.stages import resolve_models
    from beepboop_bench.synthetic import SyntheticOptions, generate

    if not phonemize.available():
        sys.stderr.write(phonemize.MISSING_REASON + "\n")
        return 2
    models = resolve_models(manifest, root)
    corpus: Path = args.corpus
    if args.regenerate or not (corpus / "manifest.yaml").is_file():
        generate(models, corpus, SyntheticOptions(small=args.small))

    from beepboop_bench import run as runner

    opts = runner.Options(
        corpus=corpus,
        models=root,
        out=args.out,
        concurrency=args.concurrency,
        realtime=True,
        duration_s=args.duration,
        label="tools/bench/synthetic/run.py (CI smoke)",
    )
    subs: list[runner.Sub] = ["speaker", "stt", "tts", "wake", "endpoint", "llm", "ram", "thermal"]
    result = runner.run(subs, opts)
    path = runner.write_result(runner.result_path(opts, result, result.host), result)
    runner.render(result)
    sys.stderr.write(f"wrote {path}\n")
    if result.measurement != "not a gate result":
        sys.stderr.write("BUG: a synthetic run must never be a gate result\n")
        return 3
    errors = {
        name: section.reason
        for name, section in result.sections.model_dump().items()
        if section is not None and section.get("status") == "error"
    }
    if errors:
        sys.stderr.write(f"sections with errors: {json.dumps(errors, indent=2)}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
