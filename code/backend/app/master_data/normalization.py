"""标准物料与业务别名共用的检索归一化规则。"""

import re


def normalize_material_name(value: str) -> str:
    """忽略空格和常见标点，不推断或移除规格数字。"""
    return re.sub(r"[\s\-_/·（）()，,。]+", "", value).casefold()
