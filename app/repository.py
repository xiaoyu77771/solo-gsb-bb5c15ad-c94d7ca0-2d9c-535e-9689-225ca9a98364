"""仓库快照的规范化与解析。

使用 packaging（PEP 440 / PEP 503）完成名称规范化、版本与约束解析。
非法快照在这里抛出 SnapshotError，由 API 层映射为 HTTP 422。
"""

from __future__ import annotations

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .markers import MarkerError, compile_marker, marker_active


def canonical(name: str) -> str:
    """PEP 503 规范化包名（小写，点/下划线/连续连字符折叠为单个 '-'）。"""
    return canonicalize_name(name)


class SnapshotError(ValueError):
    """快照或请求中存在非法（无法按 PEP 440/503/508 解析的）数据。"""


class Dependency:
    __slots__ = ("name", "raw_spec", "spec", "raw_marker", "marker")

    def __init__(
        self,
        name: str,
        raw_spec: str,
        spec: SpecifierSet,
        raw_marker: str,
        marker: Marker | None,
    ):
        self.name = canonical(name)
        self.raw_spec = raw_spec.strip() or "*"
        self.spec = spec
        # 空 marker 归一化为 ""（输出/相等比较用）。
        self.raw_marker = raw_marker.strip()
        self.marker = marker

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"Dependency({self.name!r}, {self.raw_spec!r}, {self.raw_marker!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Dependency):
            return NotImplemented
        return (
            self.name == other.name
            and self.spec == other.spec
            and self.raw_marker == other.raw_marker
        )

    def __hash__(self) -> int:
        return hash((self.name, str(self.spec), self.raw_marker))


class Candidate:
    __slots__ = ("version", "deps")

    def __init__(self, version: Version, deps: list[Dependency]):
        self.version = version
        self.deps = deps


class Repository:
    """按规范化包名索引的不可变快照；候选版本从高到低排列。"""

    def __init__(self, packages: dict[str, list[Candidate]]):
        self._packages = packages

    def candidates(self, name: str) -> list[Candidate]:
        return self._packages.get(canonical(name), [])

    def known(self, name: str) -> bool:
        return canonical(name) in self._packages


def _parse_specifier(raw: str, *, owner: str) -> SpecifierSet:
    text = (raw or "").strip()
    try:
        return SpecifierSet(text)
    except (InvalidSpecifier, InvalidRequirement) as exc:
        raise SnapshotError(
            f"{owner} 的版本约束 {raw!r} 不是合法的 PEP 440 约束: {exc}"
        ) from exc


def build_repository(raw: dict[str, list[dict]], environment: dict[str, str]) -> Repository:
    """把 API 输入构造成 Repository。

    入参为纯字典（API 层对 Pydantic 模型 model_dump 后传入），形如::

        {"pkg": [{"version": "1.0",
                  "dependencies": [
                      {"name": "dep", "specifier": ">=1.0",
                       "marker": "sys_platform == 'linux'"}]}]}]}

    * marker 在构造时按 PEP 508 解析并做变量白名单校验（非法/越权 →
      SnapshotError，错误定位到 包/版本/依赖）；
    * marker 仅在给定 environment（请求中的 target_environment）上求值，
      未激活的依赖在进入求解器之前被剔除，因此不会进入全局约束、回溯、
      冲突来源链与 edges；
    * 候选列表顺序无关，内部按 PEP 440 版本降序排列，保证解析确定性。
    """
    packages: dict[str, list[Candidate]] = {}

    for raw_name, raw_candidates in raw.items():
        cname = canonical(raw_name)
        bucket: dict[Version, Candidate] = {}

        for item in raw_candidates:
            version_text = item["version"].strip()
            try:
                version = Version(version_text)
            except InvalidVersion as exc:
                raise SnapshotError(
                    f"包 {cname!r} 的版本 {item['version']!r} 不是合法的 PEP 440 版本"
                ) from exc

            location = f"{cname}=={version}"
            deps: list[Dependency] = []
            for dep_index, dep in enumerate(item.get("dependencies", []), start=1):
                dep_location = (
                    f"{location} 的第 {dep_index} 条依赖 {dep['name']!r}"
                )
                spec = _parse_specifier(
                    dep.get("specifier", ""), owner=dep_location
                )
                raw_marker = dep.get("marker", "") or ""
                try:
                    marker = compile_marker(raw_marker)
                except MarkerError as exc:
                    raise SnapshotError(f"{dep_location} 的 marker 非法：{exc}") from exc
                deps.append(
                    Dependency(
                        dep["name"],
                        dep.get("specifier", "").strip(),
                        spec,
                        raw_marker,
                        marker,
                    )
                )

            # 去重比较使用完整声明（含 marker 文本）：同一版本若声明不一致
            # （含 marker 差异）即快照冲突。
            if version in bucket:
                other = bucket[version]
                if other.deps != deps:
                    raise SnapshotError(
                        f"包 {cname!r} 的版本 {version} 出现多次且依赖声明不一致"
                    )
                # 完全重复则忽略
                continue
            bucket[version] = Candidate(version, deps)

        def resolved(version_key: Version) -> Candidate:
            """生成按目标环境过滤后的候选：未激活依赖不进入求解。"""
            candidate = bucket[version_key]
            active_deps = [
                dep for dep in candidate.deps if marker_active(dep.marker, environment)
            ]
            if len(active_deps) == len(candidate.deps):
                return candidate
            return Candidate(candidate.version, active_deps)

        # PEP 440 降序：候选总是从高到低尝试。
        packages[cname] = [resolved(v) for v in sorted(bucket, reverse=True)]

    return Repository(packages)


def parse_root_specifier(raw: str) -> SpecifierSet:
    return _parse_specifier(raw, owner="根请求")
