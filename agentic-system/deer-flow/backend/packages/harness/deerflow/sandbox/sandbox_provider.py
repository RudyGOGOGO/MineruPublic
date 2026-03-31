from abc import ABC, abstractmethod

from deerflow.config import get_app_config
from deerflow.reflection import resolve_class
from deerflow.sandbox.sandbox import Sandbox


class SandboxProvider(ABC):
    """Abstract base class for sandbox providers"""

    @abstractmethod
    def acquire(self, thread_id: str | None = None) -> str:
        """Acquire a sandbox environment and return its ID.

        Returns:
            The ID of the acquired sandbox environment.
        """
        pass

    @abstractmethod
    def get(self, sandbox_id: str) -> Sandbox | None:
        """Get a sandbox environment by ID.

        Args:
            sandbox_id: The ID of the sandbox environment to retain.
        """
        pass

    @abstractmethod
    def release(self, sandbox_id: str) -> None:
        """Release a sandbox environment.

        Args:
            sandbox_id: The ID of the sandbox environment to destroy.
        """
        pass


_default_sandbox_provider: SandboxProvider | None = None
_named_sandbox_providers: dict[str, SandboxProvider] = {}


def get_sandbox_provider(name: str | None = None, **kwargs) -> SandboxProvider:
    """Get a sandbox provider by name, or the default provider.

    When ``name`` is given and a matching entry exists in ``config.sandboxes``,
    the corresponding named provider is returned (creating it on first access).
    Otherwise the default provider derived from ``config.sandbox`` is returned.

    Returns a cached singleton instance per name. Use ``reset_sandbox_provider()``
    to clear the cache, or ``shutdown_sandbox_provider()`` to properly shutdown
    and clear all providers.

    Returns:
        A sandbox provider instance.
    """
    global _default_sandbox_provider

    config = get_app_config()

    # Named sandbox lookup
    if name and name in config.sandboxes:
        if name not in _named_sandbox_providers:
            sandbox_config = config.sandboxes[name]
            cls = resolve_class(sandbox_config.use, SandboxProvider)
            _named_sandbox_providers[name] = cls(**kwargs)
        return _named_sandbox_providers[name]

    # Default sandbox
    if _default_sandbox_provider is None:
        cls = resolve_class(config.sandbox.use, SandboxProvider)
        _default_sandbox_provider = cls(**kwargs)
    return _default_sandbox_provider


def reset_sandbox_provider() -> None:
    """Reset the sandbox provider singleton.

    This clears the cached instance without calling shutdown.
    The next call to `get_sandbox_provider()` will create a new instance.
    Useful for testing or when switching configurations.

    Note: If the provider has active sandboxes, they will be orphaned.
    Use `shutdown_sandbox_provider()` for proper cleanup.
    """
    global _default_sandbox_provider, _named_sandbox_providers
    _default_sandbox_provider = None
    _named_sandbox_providers = {}


def shutdown_sandbox_provider() -> None:
    """Shutdown and reset all sandbox providers.

    This properly shuts down every provider (releasing all sandboxes)
    before clearing the singletons. Call this when the application
    is shutting down or when you need to completely reset the sandbox system.
    """
    global _default_sandbox_provider, _named_sandbox_providers

    # Shutdown named providers
    for provider in _named_sandbox_providers.values():
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    _named_sandbox_providers = {}

    # Shutdown default provider
    if _default_sandbox_provider is not None:
        if hasattr(_default_sandbox_provider, "shutdown"):
            _default_sandbox_provider.shutdown()
        _default_sandbox_provider = None


def set_sandbox_provider(provider: SandboxProvider) -> None:
    """Set a custom sandbox provider instance.

    This allows injecting a custom or mock provider for testing purposes.

    Args:
        provider: The SandboxProvider instance to use.
    """
    global _default_sandbox_provider
    _default_sandbox_provider = provider
