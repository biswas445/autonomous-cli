"""UI subpackage: terminal interface over the existing runtime.

The UI is a client of the runtime, never a second orchestrator (spec §67/§68):
all state comes from the workspace/store, all controls go through the
existing control channel, and all events come from the runtime's event log.
"""

from .facade import RuntimeFacade

__all__ = ["RuntimeFacade"]
