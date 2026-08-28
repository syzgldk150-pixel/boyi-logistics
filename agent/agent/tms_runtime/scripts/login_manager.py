class TMSAuth:
    """Compatibility wrapper that returns a requests session from the shared broker."""

    def __init__(
        self,
        config_path=None,
        *,
        username_env: str = "TMS_CAOZUOUSERNAME",
        password_env: str = "TMS_CAOZUOPASSWORD",
        phone_env: str = "",
        profile: str = "default",
    ):
        self.config_path = config_path
        self.username_env = username_env
        self.password_env = password_env
        self.phone_env = phone_env
        self.profile = profile
        self.config: dict[str, object] = {}

    def login_and_get_session(self, max_attempts: int = 6):
        """Return the saved session without a separate online preflight.

        Business callers must let their real target request decide whether the
        session is still accepted.  The shared broker installs response guards
        that turn an actual login redirect/page into ``AUTH_REQUIRED``.  Full
        multi-system validation is reserved for the background health matrix;
        running it here made every read depend on unrelated TMS subsystems.
        """

        _ = max_attempts
        from agent.tms_runtime.session_broker import get_session_broker

        return get_session_broker(self.profile).build_requests_session_unchecked()
