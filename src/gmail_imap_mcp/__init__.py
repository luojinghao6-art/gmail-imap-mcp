"""gmail-imap — Gmail for AI agents. App Password only, no OAuth."""

from .core import (  # noqa: F401
    AuthError,
    GmailError,
    MailClient,
    Settings,
    config_file,
    has_credentials,
    load_settings,
    save_settings,
)

__version__ = "0.1.0"
__all__ = [
    "AuthError",
    "GmailError",
    "MailClient",
    "Settings",
    "config_file",
    "has_credentials",
    "load_settings",
    "save_settings",
    "__version__",
]
