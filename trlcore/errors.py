"""例外クラス。"""



class ConfigError(ValueError):
    pass


class HttpError(Exception):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def transient(self):
        """再試行で回復し得る失敗(ネットワーク断・タイムアウト・429・5xx)。"""
        return self.status is None or self.status == 429 or self.status >= 500


class PromptError(ValueError):
    pass


class LLMError(Exception):
    pass


class ResponseShapeError(LLMError):
    """API 応答の形状異常 (候補が空・安全フィルタ等)。論文の内容に起因しないため、
    内容エラーではなくインフラ側の一時的失敗として扱う。"""
    pass
