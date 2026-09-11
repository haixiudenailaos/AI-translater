"""P1-12：单一版本源。

整个项目（main.py 标题、build.py 构建元信息、打包脚本）从这里读取版本，
避免版本散落在 workflow / spec / main / build / pyproject 多处。

``pyproject.toml`` 的 ``[project] version`` 是面向打包工具的权威来源，
本模块与之保持一致；运行时代码只读本模块。
"""

__version__ = "1.6.2"


def display_version() -> str:
    """返回面向用户展示的完整语义版本号。"""
    return __version__
