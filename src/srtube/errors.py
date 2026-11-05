class UserError(Exception):
    """An actionable message safe to show to a Telegram user."""


class InvalidArtwork(UserError):
    def __init__(self) -> None:
        super().__init__("Пришли исправное изображение JPEG, PNG или WebP, не больше 8 мегапикселей.")


class InvalidMP3(UserError):
    def __init__(self) -> None:
        super().__init__("Не удалось прочитать MP3. Пришли исправный файл в формате MP3.")


class MP3TooLong(UserError):
    def __init__(self, limit: int) -> None:
        super().__init__(f"MP3 слишком длинный. Лимит — {limit // 60} мин.")


class ProcessError(Exception):
    def __init__(self, stderr: str):
        self.stderr = stderr
        super().__init__("External media process failed")
