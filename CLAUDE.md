# Rules for this repo

## Record everything in the scorer's image

Any data the selector ships that comes from running sqlglot or its tests must be recorded inside the scorer's pinned image (`python:3.13-slim@sha256:e48f59cd...`, the `FROM` line of `Dockerfile`) with `requirements.lock` installed. Never record it on a laptop. That covers coverage maps, key maps, tokenizer recordings, prover profiles and baselines. The answers were labeled in that image. Anything that differs elsewhere (timezone, locale, paths, package builds, memory layout) can make a recording miss what really happens there, and one miss costs 300 s.

- `tools/build_map_docker.sh` records the coverage map; `tools/record_in_image.sh` records the key map and the tokenizer recordings.
- The image's native arm64 variant is fine for speed (sqlglot is pure Python). When you use it, re-record a sample of tests under `linux/amd64` and compare. `tools/record_in_image.sh` does both.
- Benches that check a stage against planted bugs must also run in the image.
- Before pushing, run `make sample` (it builds and runs the image under the scorer's limits), not only the selector on your machine.
