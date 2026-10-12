"""Podgrove: one isolated Docker engine per working directory."""
from importlib import metadata

__version__ = "0.4.3"


def package_version() -> str:
    """Report the installed distribution, falling back only for an unpackaged checkout."""
    try:
        return metadata.version("podgrove")
    except metadata.PackageNotFoundError:
        return __version__
