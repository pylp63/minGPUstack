"""Kubernetes resource-quantity parsing helpers.

二开说明: 企业版计费/计量管线 (metered_usage / resource_events /
collectors / archivers) 已在本版本中整体移除. 本文件仅保留通用的
k8s 数量解析工具 — 调度器 (vgpu selector) 与模板渲染依赖它们.
"""

from typing import Optional

# ---------------------------------------------------------------------------
# Kubernetes quantity parser
# ---------------------------------------------------------------------------

_BINARY_SUFFIX = {
    "Ki": 1.0 / 1024,  # 1 Ki = 1024 bytes = 1/1024 MiB
    "Mi": 1.0,
    "Gi": 1024.0,
    "Ti": 1024.0 * 1024,
    "Pi": 1024.0 * 1024 * 1024,
    "Ei": 1024.0 * 1024 * 1024 * 1024,
}
_DECIMAL_SUFFIX = {
    "": 1.0 / (1024 * 1024),  # raw bytes → MiB
    "k": 1000.0 / (1024 * 1024),
    "K": 1000.0 / (1024 * 1024),
    "M": 1_000_000.0 / (1024 * 1024),
    "G": 1_000_000_000.0 / (1024 * 1024),
    "T": 1_000_000_000_000.0 / (1024 * 1024),
}


def parse_quantity_to_mib(value: Optional[str | int | float]) -> int:
    """Parse a k8s resource quantity (memory / storage) to integer MiB.

    Accepts strings like ``"100Gi"``, ``"2048Mi"``, ``"512Ki"``, bare numbers
    (interpreted as bytes), or numeric types. Returns 0 for ``None`` / empty /
    unparseable inputs — callers treat 0 as "skip this resource".
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value / (1024 * 1024)))
    s = str(value).strip()
    if not s:
        return 0
    # Binary suffixes (Ki/Mi/Gi/...) take priority over decimal because the
    # binary form unambiguously ends in 'i'.
    for suffix, multiplier in _BINARY_SUFFIX.items():
        if s.endswith(suffix):
            numeric = s[: -len(suffix)]
            try:
                return max(0, int(float(numeric) * multiplier))
            except ValueError:
                return 0
    # Decimal suffixes — handle longest first so "M" doesn't shadow "Mi".
    for suffix in sorted(_DECIMAL_SUFFIX, key=len, reverse=True):
        if suffix and s.endswith(suffix):
            numeric = s[: -len(suffix)]
            try:
                return max(0, int(float(numeric) * _DECIMAL_SUFFIX[suffix]))
            except ValueError:
                return 0
    # Bare number → bytes.
    try:
        return max(0, int(float(s) / (1024 * 1024)))
    except ValueError:
        return 0


def parse_quantity_to_millicores(value: Optional[str | int | float]) -> int:
    """Parse a k8s CPU quantity to integer millicores.

    Accepts ``"2"`` (= 2000m), ``"500m"`` (= 500m), or numeric types (whole
    cores). Returns 0 for unparseable inputs.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value * 1000))
    s = str(value).strip()
    if not s:
        return 0
    if s.endswith("m"):
        try:
            return max(0, int(float(s[:-1])))
        except ValueError:
            return 0
    try:
        return max(0, int(float(s) * 1000))
    except ValueError:
        return 0
