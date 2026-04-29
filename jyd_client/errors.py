class JydClientError(RuntimeError):
    pass


class DependencyError(JydClientError):
    pass


class AuthenticationError(JydClientError):
    pass


class BoardUnavailableError(JydClientError):
    pass


class RemoteCommandError(JydClientError):
    pass


def require_module(module_name: str, install_hint: str):
    try:
        return __import__(module_name)
    except ModuleNotFoundError as exc:
        raise DependencyError(
            f"Missing Python dependency '{module_name}'. Install with: {install_hint}"
        ) from exc
