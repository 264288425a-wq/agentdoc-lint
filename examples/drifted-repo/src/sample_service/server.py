import os


class TokenValidator:
    def verify_session(self, token: str) -> bool:
        return bool(token and os.getenv("SERVICE_DB_URL"))


def run() -> int:
    return 0
