class UserError(Exception):
    """An actionable message safe to show to a Telegram user."""


class ProcessError(Exception):
    def __init__(self, stderr: str):
        self.stderr = stderr
        super().__init__("External media process failed")
