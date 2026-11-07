"""Entry script for the PyInstaller build (transferstage.spec).

PyInstaller runs its entry point as a top-level script, where the package's
relative imports in ``transferstage/__main__.py`` would fail; this file imports
the package absolutely instead.
"""

import sys

from transferstage.app import main

if __name__ == "__main__":
    sys.exit(main())
