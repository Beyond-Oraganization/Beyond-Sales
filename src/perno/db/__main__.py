"""`python3 -m perno.db --check` (diagnostyka bazy) — kod w perno.cli.dbcheck."""

import sys

from perno.cli.dbcheck import main

sys.exit(main())
