# "Hey beepboop" microWakeWord training pipeline (VOICE_PIPELINE §6.4: one target per step).
# Run from this directory on Linux / WSL2 / Colab after `uv sync`. README.md is the runbook.

UV ?= uv
RUN := $(UV) run train-wakeword
PROFILE ?= shipped
DATA ?= data
ARGS ?=

# Pinned sources (also pinned in pyproject.toml / README §2).
PSG_REPO := https://github.com/rhasspy/piper-sample-generator
PSG_COMMIT := 2971426a55072f7d22fec416ca7800df8bd23207

.PHONY: deps check synth negatives augment train export eval all clean

deps:            ## create the venv and clone the sample generator at its pinned commit
	$(UV) sync
	@if [ ! -d $(DATA)/third_party/piper-sample-generator/.git ]; then \
		git clone --no-checkout $(PSG_REPO) $(DATA)/third_party/piper-sample-generator; \
	fi
	git -C $(DATA)/third_party/piper-sample-generator checkout --quiet $(PSG_COMMIT)

check:           ## validate the config and DATA_LICENSES.md (no training deps needed)
	$(RUN) --profile $(PROFILE) --data-dir $(DATA) check

synth:           ## 01: synthetic positives + near-misses
	$(RUN) --profile $(PROFILE) --data-dir $(DATA) positives $(ARGS)

negatives:       ## 02: download and convert the negative datasets
	$(RUN) --profile $(PROFILE) --data-dir $(DATA) negatives $(ARGS)

augment:         ## 03: augment and build feature mmaps
	$(RUN) --profile $(PROFILE) --data-dir $(DATA) augment $(ARGS)

train:           ## 04: run the trainer (GPU recommended)
	$(RUN) --profile $(PROFILE) --data-dir $(DATA) train $(ARGS)

export:          ## 05: TFLite + manifest + provenance into $(DATA)/out
	$(RUN) --profile $(PROFILE) --data-dir $(DATA) export $(ARGS)

eval:            ## 06: false accepts/hour + false rejects -> $(DATA)/results
	$(RUN) --profile $(PROFILE) --data-dir $(DATA) evaluate $(ARGS)

all: check synth negatives augment train export eval

clean:           ## remove generated features and training output (keeps downloads)
	rm -rf $(DATA)/features $(DATA)/trained_models
