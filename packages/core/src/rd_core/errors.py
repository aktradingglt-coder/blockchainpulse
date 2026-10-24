"""Base error hierarchy. Every error raised by our code derives from ``ResearchDeskError``."""


class ResearchDeskError(Exception):
    """Root of all application errors."""


class ConfigError(ResearchDeskError):
    """Configuration is missing, malformed or inconsistent."""


class SecretLeakError(ResearchDeskError):
    """A value that looks like a secret was about to be written somewhere it must not go."""
