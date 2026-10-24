"""
Unified WatchDog Service for monitoring UI Automation events.
Allows single instantiation to handle multiple monitors (Focus, Structure) safely in one STA thread.
"""

from windows_mcp.uia.core import _AutomationClient
from windows_mcp.uia.enums import TreeScope
from threading import Thread, Event
import comtypes.client
import comtypes
import logging
import time

from .event_handlers import (
    StructureChangedEventHandler,
    PropertyChangedEventHandler,
    _record_failure,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Consecutive per-callback COMErrors that trigger a full client rebuild.
# UIA event subscriptions can silently go stale (desktop switch, sleep/resume,
# session reconfig) without the failure ever becoming a catchable exception in
# the pump loop, so we detect it from the callback side instead.
FAIL_THRESHOLD = 5
MAX_BACKOFF_SECONDS = 30.0

# How long a run must survive before its exit is treated as a one-off rather
# than a symptom of a still-broken environment. Below this, the backoff keeps
# growing, so a UIA stack that degrades immediately on every rebuild is retried
# ever more slowly instead of churning COM clients at 1 Hz forever.
HEALTHY_RUN_SECONDS = 60.0

# Focus is polled rather than subscribed to (#332). A UIA focus subscription
# makes native COM call into Python on every focus change; once the UIA stack
# collapses (sleep/resume, session or display reconfiguration) one of those
# inbound calls can hit a torn-down interface and fault inside the pump, which
# kills the whole server with no Python traceback. Polling GetFocusedElement()
# inverts the call direction: every COM call is ours, made on this thread, and
# its failures surface as catchable COMErrors that feed the rebuild path.
FOCUS_POLL_SECONDS = 0.25


class WatchDog:
    def __init__(self):
        # Only used to reuse the cached UIAutomationCore module (interface
        # definitions, not a live COM object) - never share the singleton's
        # IUIAutomation pointer, since it was bound to whatever desktop was
        # active when the process started and won't survive a desktop switch.
        self.uia_client = _AutomationClient.instance()
        self.uia = None
        self.is_running = Event()
        self.thread = None
        self._needs_rebuild = Event()

        # Callbacks
        self._focus_callback = None
        self._structure_callback = None
        self._structure_element = None
        self._property_callback = None
        self._property_element = None
        self._property_ids = None

        # Internal state for tracking active handlers. Focus keeps only the
        # last runtime id - never a COM pointer that could outlive the apartment.
        self._last_focus_id = None
        self._next_focus_poll = 0.0
        self._structure_handler = None
        self._active_structure_element = None
        self._property_handler = None
        self._active_property_element = None
        self._active_property_ids = None

        # Consecutive-failure counters, reset on success or rebuild
        self._focus_fail_count = 0
        self._structure_fail_count = 0
        self._property_fail_count = 0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def start(self):
        """Start the watchdog service thread."""
        if self.is_running.is_set():
            return
        self.is_running.set()
        self.thread = Thread(target=self._run, name="WatchDogThread")
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        """Stop the watchdog service thread."""
        if not self.is_running.is_set():
            return
        self.is_running.clear()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)

    def set_focus_callback(self, callback):
        """Set the callback for focus changes. Pass None to disable."""
        self._focus_callback = callback

    def set_structure_callback(self, callback, element=None):
        """Set the callback for structure changes. Pass None to disable.
        Optionally specify an element to watch (defaults to RootElement)."""
        self._structure_callback = callback
        self._structure_element = element

    def set_property_callback(self, callback, element=None, property_ids=None):
        """Set the callback for property changes. Pass None to disable.
        Optionally specify an element to watch (defaults to RootElement)
        and a list of property IDs to monitor."""
        self._property_callback = callback
        self._property_element = element
        self._property_ids = property_ids

    def request_rebuild(self):
        """Signal that the current UIA client/subscriptions should be torn
        down and recreated. Safe to call from event-handler callbacks."""
        self._needs_rebuild.set()

    def _create_uia(self):
        """Create a fresh IUIAutomation object bound to this thread's current
        COM apartment/desktop context."""
        return comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=self.uia_client.UIAutomationCore.IUIAutomation,
        )

    def _run(self):
        """Main event loop running in a dedicated STA thread.

        Outer loop rebuilds the IUIAutomation client and all event
        subscriptions from scratch whenever they're flagged as degraded
        (repeated COMErrors) or an exception escapes the inner pump loop,
        with exponential backoff so a persistently broken UIA environment
        doesn't spin hot.
        """
        backoff = 1.0
        while self.is_running.is_set():
            started = time.monotonic()
            initialized = False
            try:
                # Inside the try: a CoInitialize failure used to escape and end
                # the thread silently, leaving no watchdog and no retry.
                comtypes.CoInitialize()
                initialized = True
                self.uia = self._create_uia()
                self._needs_rebuild.clear()
                self._focus_fail_count = 0
                self._structure_fail_count = 0
                self._property_fail_count = 0
                self._last_focus_id = None
                self._next_focus_poll = 0.0
                self._event_loop()
            except Exception as e:
                logger.warning(f"WatchDog degraded, rebuilding UIA client: {e}")
            finally:
                self._teardown_handlers()
                if initialized:
                    comtypes.CoUninitialize()

            # Only a run that lasted earns a reset. A rebuild requested seconds
            # after starting means the environment is still broken, and
            # resetting here would retry at 1 Hz indefinitely -- rebuilding the
            # COM client and logging a warning every second, in exactly the
            # degraded state this backoff exists to damp.
            if time.monotonic() - started >= HEALTHY_RUN_SECONDS:
                backoff = 1.0

            if self.is_running.is_set():
                self.is_running.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def _event_loop(self):
        """Pump UIA events against the current self.uia client until stopped
        or a rebuild is requested."""
        while self.is_running.is_set() and not self._needs_rebuild.is_set():
            # --- Focus Monitoring (polled, see FOCUS_POLL_SECONDS) ---
            now = time.monotonic()
            if self._focus_callback and now >= self._next_focus_poll:
                self._next_focus_poll = now + FOCUS_POLL_SECONDS
                self._poll_focus()

            # --- Structure Monitoring ---
            # Check if we need to UNREGISTER because configuration changed or disabled
            config_changed = self._structure_element != self._active_structure_element

            should_be_active = self._structure_callback is not None
            is_active = self._structure_handler is not None

            if is_active and (not should_be_active or config_changed):
                try:
                    target = (
                        self._active_structure_element
                        if self._active_structure_element
                        else self.uia.GetRootElement()
                    )
                    self.uia.RemoveStructureChangedEventHandler(target, self._structure_handler)
                except Exception as e:
                    logger.debug(f"Failed to remove structure handler: {e}")
                self._structure_handler = None
                self._active_structure_element = None
                is_active = False

            if should_be_active and not is_active:
                try:
                    target = (
                        self._structure_element
                        if self._structure_element
                        else self.uia.GetRootElement()
                    )
                    scope = TreeScope.TreeScope_Subtree

                    self._structure_handler = StructureChangedEventHandler(self)
                    self.uia.AddStructureChangedEventHandler(
                        target, scope, None, self._structure_handler
                    )
                    self._active_structure_element = target
                except Exception as e:
                    logger.debug(f"Failed to add structure handler: {e}")

            # --- Property Monitoring ---
            config_changed = (self._property_element != self._active_property_element) or (
                self._property_ids != self._active_property_ids
            )

            should_be_active = self._property_callback is not None
            is_active = self._property_handler is not None

            if is_active and (not should_be_active or config_changed):
                try:
                    target = (
                        self._active_property_element
                        if self._active_property_element
                        else self.uia.GetRootElement()
                    )
                    self.uia.RemovePropertyChangedEventHandler(target, self._property_handler)
                except Exception as e:
                    logger.debug(f"Failed to remove property handler: {e}")
                self._property_handler = None
                self._active_property_element = None
                self._active_property_ids = None
                is_active = False

            if should_be_active and not is_active:
                try:
                    target = (
                        self._property_element
                        if self._property_element
                        else self.uia.GetRootElement()
                    )
                    scope = TreeScope.TreeScope_Subtree

                    # Monitor common properties if none specified
                    # 30005: Name, 30045: Value, 30093: LegacyIAccessibleVal, 30128: ToggleState
                    p_ids = (
                        self._property_ids if self._property_ids else [30005, 30045, 30093, 30128]
                    )

                    self._property_handler = PropertyChangedEventHandler(self)
                    self.uia.AddPropertyChangedEventHandler(
                        target, scope, None, self._property_handler, p_ids
                    )

                    self._active_property_element = target
                    self._active_property_ids = p_ids
                except Exception as e:
                    logger.debug(f"Failed to add property handler: {e}")

            # Pump events for this thread
            comtypes.client.PumpEvents(0.1)

        if self._needs_rebuild.is_set():
            logger.warning("WatchDog event pipeline degraded, rebuilding UIA client")

    def _poll_focus(self):
        """Check the focused element once and notify the focus callback on change.

        Never raises: a COMError counts toward FAIL_THRESHOLD (so a collapsed
        UIA stack, including E_UNEXPECTED, leads to a client rebuild with
        backoff) and any other error is logged and dropped.
        """
        try:
            element = self.uia.GetFocusedElement()
            if not element:
                return
            runtime_id = tuple(element.GetRuntimeId() or ())
            if runtime_id != self._last_focus_id:
                self._last_focus_id = runtime_id
                callback = self._focus_callback
                if callback:
                    callback(element)
            self._focus_fail_count = 0
        except comtypes.COMError as e:
            _record_failure(self, "_focus_fail_count")
            logger.debug("Focus poll COM error: %s", e)
        except Exception as e:
            logger.debug("Focus poll error: %s", e)

    def _teardown_handlers(self):
        """Best-effort removal of any active handlers against self.uia."""
        if not self.uia:
            return

        self._last_focus_id = None

        if self._structure_handler:
            try:
                target = (
                    self._active_structure_element
                    if self._active_structure_element
                    else self.uia.GetRootElement()
                )
                self.uia.RemoveStructureChangedEventHandler(target, self._structure_handler)
            except Exception:
                pass
            self._structure_handler = None
            self._active_structure_element = None

        if self._property_handler:
            try:
                target = (
                    self._active_property_element
                    if self._active_property_element
                    else self.uia.GetRootElement()
                )
                self.uia.RemovePropertyChangedEventHandler(target, self._property_handler)
            except Exception:
                pass
            self._property_handler = None
            self._active_property_element = None
            self._active_property_ids = None

        self.uia = None
