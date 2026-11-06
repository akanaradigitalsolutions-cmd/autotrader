import asyncio


def externally_cancelled() -> bool:
    """True when the current task has been cancelled from outside.

    Distinguishes a real cancellation (shutdown) from a CancelledError
    raised *inside* an awaited library call. The MetaApi SDK cancels its
    own in-flight request futures when its socket reconnects, and that
    surfaces as CancelledError in our code - it must be treated as a
    failed broker call, not as our task being told to stop. CancelledError
    is a BaseException, so plain `except Exception` handlers never see it:
    without this check it silently kills the awaiting task.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
