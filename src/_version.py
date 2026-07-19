"""P1-12：单一版本源。

整个项目（main.py 标题、build.py 构建元信息、打包脚本）从这里读取版本，
避免版本散落在 workflow / spec / main / build / pyproject 多处。

``pyproject.toml`` 的 ``[project] version`` 是面向打包工具的权威来源，
本模块与之保持一致；运行时代码只读本模块。
"""

__version__ = "1.6.0"


def display_version() -> str:
    """面向用户展示的 major.minor 版本号（如 "1.6"）。

    构建产物名、窗口标题使用此短版本，避免把补丁号带入面向用户的展示。
    """
    parts = __version__.split(".")
    return ".".join(parts[:2])
