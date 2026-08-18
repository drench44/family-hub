"""family-hub — a public family wall-dashboard (FastAPI + SQLite + vanilla JS)."""
from .version import read_version

# The running hub's version, read once from the repo-root VERSION file. The
# release script (scripts/release.py) is the only writer; everything that shows
# a version (the API, the in-app badge, the asset cache-bust) derives from it.
__version__ = read_version()
