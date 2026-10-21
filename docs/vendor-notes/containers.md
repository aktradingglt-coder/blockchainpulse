# Container base images

Pulled 2026-10-21 via `mirror.gcr.io` (Google's Docker Hub mirror; Docker Hub anonymous pulls were rate-limited in the build sandbox). Digests are multi-arch index digests and are identical on Docker Hub.

| Image | Tag | Digest | Licence note |
| --- | --- | --- | --- |
| timescale/timescaledb | 2.22.1-pg16 | sha256:de6e2326b4a3f5f6774e2629b60791d3ccf3f58a58e551414e6730e17e43f17e | Timescale License (TSL) for community features: allowed for internal use and in products, **not** for offering a hosted DBaaS |
| valkey/valkey | 8.1.4-alpine | sha256:e706d1213aaba6896c162bb6a3a9e1894e1a435f28f8f856d14fab2e10aa098b | BSD-3-Clause, Redis-protocol compatible (D-004) |
| python | 3.12.13-slim-bookworm | sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 | PSF (patch version matches `.python-version` and CI's uv-managed 3.12.13) |
| docker/dockerfile (BuildKit frontend) | 1.7 | sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e | Apache-2.0 |
| node | 22.22.0-bookworm-slim | sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94 | MIT |
