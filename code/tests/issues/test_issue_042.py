import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_delivery_index_covers_prd_and_prototype_boards() -> None:
    evidence = (REPO_ROOT / "doc/ACCEPTANCE-EVIDENCE.md").read_text(encoding="utf-8")
    for prefix, count in (("FR", 28), ("AC", 27), ("NFR", 14)):
        for number in range(1, count + 1):
            assert f"{prefix}-{number:02}" in evidence
    for number in range(1, 26):
        assert re.search(rf"\|\s*{number:02}\s*\|", evidence), (
            f"缺少原型画板 {number:02}"
        )


def test_delivery_docs_and_demo_links_exist() -> None:
    documents = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "doc/README.md",
        REPO_ROOT / "doc/DELIVERY-GUIDE.md",
        REPO_ROOT / "doc/development/LOCAL-ENVIRONMENT.md",
    ]
    for document in documents:
        assert document.is_file(), f"文档不存在：{document}"

    script = REPO_ROOT / "code/scripts/demo_issue_042.ps1"
    assert script.is_file()
    content = script.read_text(encoding="utf-8")
    for scenario in (
        "TextToOrder",
        "OcrAndQuota",
        "ExcelPartial",
        "DualModeTraceEvaluation",
        "All",
    ):
        assert scenario in content
    for verifier in (
        "verify_issue_037",
        "verify_issue_028",
        "verify_issue_029",
        "verify_issue_032",
        "verify_issue_039",
    ):
        assert (script.parent / f"{verifier}.ps1").is_file()


def test_delivery_docs_disclose_observation_limit_and_link_targets() -> None:
    evidence = (REPO_ROOT / "doc/ACCEPTANCE-EVIDENCE.md").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "doc/DELIVERY-GUIDE.md").read_text(encoding="utf-8")
    assert "豁免、未完成" in evidence
    assert "八小时" in guide

    for document in (
        REPO_ROOT / "README.md",
        REPO_ROOT / "doc/README.md",
        *[
            REPO_ROOT / "doc" / name
            for name in ("ACCEPTANCE-EVIDENCE.md", "DELIVERY-GUIDE.md")
        ],
    ):
        for target in re.findall(
            r"\[[^\]]+\]\(([^)]+)\)", document.read_text(encoding="utf-8")
        ):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path = (document.parent / target.split("#", 1)[0]).resolve()
            assert path.exists(), (
                f"{document.relative_to(REPO_ROOT)} 中的链接目标不存在：{target}"
            )
