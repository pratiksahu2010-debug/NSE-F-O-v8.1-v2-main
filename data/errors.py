class ProviderError(Exception):
    """Counts toward failover (network, 5xx, auth, rate limit)."""


class AuthError(ProviderError):
    pass


class RateLimited(ProviderError):
    pass


class DataUnavailable(Exception):
    """Valid response but no data (new listing, holiday...). Does NOT count toward failover."""


class NotSupported(Exception):
    pass
