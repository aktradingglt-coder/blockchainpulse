"""``python -m rd_worker.main [dramatiq options]`` — worker launcher.

Configures JSON logging (with redaction) in the Dramatiq supervisor process too, then hands over to Dramatiq's CLI
with ``--skip-logging`` so neither parent nor children fall back to Dramatiq's plain-text formatter (D-017).
"""

from __future__ import annotations

import sys

from dramatiq.cli import main as dramatiq_main
from dramatiq.cli import make_argument_parser

from rd_core.bootstrap import bootstrap

SERVICE = "rd-worker"
ACTORS_MODULE = "rd_worker.actors"


def build_args(extra: list[str]) -> list[str]:
    return [ACTORS_MODULE, "--skip-logging", *extra]


def main() -> None:
    bootstrap(SERVICE)
    args = make_argument_parser().parse_args(build_args(sys.argv[1:]))
    sys.exit(dramatiq_main(args))


if __name__ == "__main__":
    main()
