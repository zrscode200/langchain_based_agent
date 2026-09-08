"""Optional enterprise integration; importing it does not load the TUI."""
# Reserve OG's host-owned settings before upstream can load workspace dotenv.
import lc_factory as _factory

__version__ = "0.2.0"
