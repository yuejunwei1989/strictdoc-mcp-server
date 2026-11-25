"""StrictDoc 项目配置：开启需求↔测试追溯。

注意：strictdoc export 不会自动加载本文件，必须显式传
    strictdoc export . --config project_config.py ...
"""

from pathlib import Path

from strictdoc.core.project_config import ProjectConfig, ProjectFeature

PROJECT_ROOT = Path(__file__).resolve().parent


def create_config() -> ProjectConfig:
    return ProjectConfig(
        project_title="WindowMgmt JUnit Traceability Demo",
        project_features=[
            # 注意：必须是 ProjectFeature 枚举；传裸字符串会被静默忽略
            ProjectFeature.REQUIREMENT_TO_SOURCE_TRACEABILITY,
        ],
        # reports/ 里放 JUnit XML，必须纳入文档扫描路径
        include_doc_paths=["/docs/", "/reports/"],
        include_source_paths=["/tests/"],
        # 源码根必须是绝对路径：默认值基于项目根推导，子目录工程易错位
        source_root_path=str(PROJECT_ROOT),
    )
