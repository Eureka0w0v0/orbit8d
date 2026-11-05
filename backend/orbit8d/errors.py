"""带错误码的异常：任务失败时错误码写进记录、接口按错误码返回，界面按错误码翻译成当前语言（SPEC §7）。"""


class CodedError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
