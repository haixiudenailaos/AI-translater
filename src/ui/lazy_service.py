"""Thread-safe lazy construction for non-visual UI services."""

from __future__ import annotations

from importlib import import_module
from threading import RLock
from typing import Any


class LazyService:
    """Delay importing and constructing a service until it is first used.

    Tkinter startup should only build widgets needed for the first frame. Heavy
    document parsers and network clients are loaded when an action requires
    them, while ``call_if_initialized`` lets shutdown remain side-effect free.
    """

    def __init__(
        self,
        module_name: str,
        class_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._module_name = module_name
        self._class_name = class_name
        self._args = args
        self._kwargs = kwargs
        self._instance: Any = None
        self._lock = RLock()

    @property
    def is_initialized(self) -> bool:
        return self._instance is not None

    def get(self) -> Any:
        instance = self._instance
        if instance is not None:
            return instance

        with self._lock:
            if self._instance is None:
                module = import_module(self._module_name)
                service_class = getattr(module, self._class_name)
                self._instance = service_class(*self._args, **self._kwargs)
            return self._instance

    def call_if_initialized(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        instance = self._instance
        if instance is None:
            return None
        return getattr(instance, method_name)(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.get(), name)
