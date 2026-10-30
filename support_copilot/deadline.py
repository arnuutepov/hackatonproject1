"""Bounded, daemonized provider isolation with a wall-clock response budget."""
from copy import deepcopy
import threading

from support_copilot.draft_provider import DraftFailure, DraftProvider

# No unbounded queue or ever-growing threads when providers fail to return.
_SLOTS = threading.BoundedSemaphore(4)


def generate_with_deadline(provider: DraftProvider, context: dict, timeout: float) -> str:
    isolated = deepcopy(context)
    slots = _SLOTS
    if not slots.acquire(blocking=False):
        raise DraftFailure('draft_provider_busy')
    ready = threading.Event()
    box = {}
    def worker():
        try:
            box['value'] = provider.generate(isolated)
        except Exception as exc:
            box['error'] = exc
        finally:
            slots.release()
            ready.set()
    try:
        threading.Thread(target=worker, name='copilot-draft', daemon=True).start()
    except Exception:
        slots.release()
        raise DraftFailure('draft_provider_unavailable') from None
    if not ready.wait(timeout):
        raise DraftFailure('draft_provider_timeout')
    if 'error' in box:
        if isinstance(box['error'], TimeoutError):
            raise DraftFailure('draft_provider_timeout') from None
        raise box['error']
    return box['value']
