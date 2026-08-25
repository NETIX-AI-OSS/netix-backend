"""Packaged prospector profiles for the NETIX fleet, resolved as ``inherits: [netix]``.

Importable and non-empty on purpose: prospector loads the YAML with ``pkgutil.get_data``, which needs a
loader with ``get_data`` — a namespace package (no ``__init__.py``) returns ``None`` and the inherit fails.
"""
