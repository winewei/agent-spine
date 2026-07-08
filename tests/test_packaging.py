"""打包/依赖声明回归测试（review round 3 fix，finding F1）。

背景：`src/npc/spec_pipeline.py` 在运行时 `import jsonschema` 并在
`spec_review_run` 的门 3（LLM 语义评审结果 schema 校验）里调用它，但
`pyproject.toml` 曾经只把 `jsonschema` 声明在 `[dependency-groups].dev`
里，`[project].dependencies` 是空数组。任何按 `pip install npc` /
`uv tool install .` 走**纯运行时**依赖装的用户，第一次触发
`npc spec review run` 时会在门 3 抛 `ModuleNotFoundError`，而不是title
里承诺的「validate/gate/LLM 三道门」正常报错。

本文件用两条互补的静态检查防止同一类问题（运行时 import 的第三方包
未进 `[project].dependencies`）在本 change 范围内的任何文件再次出现：

1. 针对 F1 具体指出的 `jsonschema`，直接断言它已在 `[project].dependencies`
   声明（而不仅仅是 dev group）。
2. 泛化扫描：对 `src/npc/` 下所有模块做 AST 级别的 import 抽取，找出
   所有非标准库、非本包（`npc`）的第三方 top-level import，逐个与
   `[project].dependencies` 声明的包名比对，缺失即失败——这条覆盖的是
   「以后新增运行时依赖又忘记声明」这一整类回归，而不只是 jsonschema
   这一个具体符号。
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
SRC_NPC_DIR = REPO_ROOT / "src" / "npc"

# import 名 -> PyPI 包名不一致的已知例外（当前 change 范围内暂无，留空即可）。
_IMPORT_NAME_TO_DIST_NAME: dict[str, str] = {}


def _load_declared_runtime_deps() -> set[str]:
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    raw_deps = data.get("project", {}).get("dependencies", [])
    names: set[str] = set()
    for dep in raw_deps:
        # "jsonschema>=4.0" / "pyyaml==6.0" / "foo" 等 PEP 508 简单形态取包名部分。
        name = dep
        for sep in ("[", ">=", "<=", "==", "!=", "~=", ">", "<", " "):
            name = name.split(sep, 1)[0]
        names.add(name.strip().lower())
    return names


def _iter_top_level_third_party_imports() -> set[str]:
    stdlib_names = set(sys.stdlib_module_names) | {"__future__"}
    local_names = {"npc"}
    found: set[str] = set()

    for py_file in SRC_NPC_DIR.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root not in stdlib_names and root not in local_names:
                        found.add(root)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # 相对 import，必是包内模块
                if node.module is None:
                    continue
                root = node.module.split(".", 1)[0]
                if root not in stdlib_names and root not in local_names:
                    found.add(root)
    return found


def test_jsonschema_declared_as_runtime_dependency_not_only_dev():
    """F1 的具体回归：spec_pipeline.py 运行时 import 的 jsonschema 必须在
    `[project].dependencies` 里，不能只活在 `[dependency-groups].dev` 里。
    """
    declared_runtime = _load_declared_runtime_deps()
    assert "jsonschema" in declared_runtime, (
        "jsonschema 是 src/npc/spec_pipeline.py 的运行时依赖（门 3 schema 校验），"
        "必须声明在 [project].dependencies，而不仅仅是 [dependency-groups].dev；"
        f"当前 [project].dependencies 解析出的包名集合={declared_runtime!r}"
    )


def test_all_third_party_runtime_imports_are_declared_as_dependencies():
    """泛化回归：src/npc/ 下任何模块 import 的第三方包都必须能在
    `[project].dependencies` 里找到对应声明，防止本类问题（F1）在其他文件
    换个包名重演。
    """
    declared_runtime = _load_declared_runtime_deps()
    used_third_party = _iter_top_level_third_party_imports()

    missing = {
        pkg
        for pkg in used_third_party
        if _IMPORT_NAME_TO_DIST_NAME.get(pkg, pkg).lower() not in declared_runtime
    }
    assert not missing, (
        "以下第三方包被 src/npc/ 运行时代码 import，但未声明在 "
        f"[project].dependencies：{sorted(missing)}；"
        f"已声明的运行时依赖={declared_runtime!r}"
    )
