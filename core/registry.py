"""平台插件注册表 - 自动扫描 platforms/ 目录加载插件"""
import importlib
import pkgutil
from typing import Dict, Type
from .base_platform import BasePlatform

_registry: Dict[str, Type[BasePlatform]] = {}
_DISABLED_PLATFORMS = {"trae", "qwen"}


def is_platform_enabled(name: str) -> bool:
    return str(name or "").strip().lower() not in _DISABLED_PLATFORMS


def register(cls: Type[BasePlatform]):
    """装饰器：注册平台插件"""
    if not is_platform_enabled(cls.name):
        return cls
    _registry[cls.name] = cls
    return cls


def load_all():
    """自动扫描并加载 platforms/ 下所有插件"""
    import platforms
    for finder, name, _ in pkgutil.iter_modules(platforms.__path__, platforms.__name__ + "."):
        platform_name = name.rsplit(".", 1)[-1].lower()
        if not is_platform_enabled(platform_name):
            continue
        try:
            importlib.import_module(f"{name}.plugin")
        except ModuleNotFoundError:
            pass


def get(name: str) -> Type[BasePlatform]:
    if not is_platform_enabled(name):
        raise KeyError(f"平台 '{name}' 已下线")
    if name not in _registry:
        raise KeyError(f"平台 '{name}' 未注册，已注册: {list(_registry.keys())}")
    return _registry[name]


def list_platforms() -> list:
    return [
        {"name": cls.name, "display_name": cls.display_name, "version": cls.version}
        for cls in _registry.values()
        if is_platform_enabled(cls.name)
    ]
