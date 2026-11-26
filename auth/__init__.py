"""Passwordless registered-user authentication module."""
from auth.passwordless_service import (
    PasswordlessAuthService,
    MockEmailDeliveryProvider,
    EmailDeliveryProvider,
    AuthenticationError,
    RateLimitExceededError,
)

__all__ = [
    "PasswordlessAuthService",
    "MockEmailDeliveryProvider",
    "EmailDeliveryProvider",
    "AuthenticationError",
    "RateLimitExceededError",
]
