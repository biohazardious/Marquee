"""Categorise, acquire and update a MAME romset.

Copyright (C) 2026 Baybars HAZAR (Biohazardious) and contributors.

Marquee is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. It is distributed
without any warranty; see the LICENSE file at the root of the source tree, or
<https://www.gnu.org/licenses/>.

SPDX-License-Identifier: GPL-3.0-or-later
"""

__version__ = "0.4.0"

from .config import Config
from .errors import (ConfigError, MarqueeError, SourceNotFoundError,
                     VersionMismatchError)
from .reporting import CollectingReporter, Reporter, TerminalReporter

__all__ = ["__version__", "Config", "MarqueeError", "ConfigError", "SourceNotFoundError",
           "VersionMismatchError", "Reporter", "TerminalReporter", "CollectingReporter"]
