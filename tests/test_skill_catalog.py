"""标准 Skill 目录与 Agent 可见集合的定向验证。"""
from hashlib import sha256
from pathlib import Path
import pytest
from core.skills.catalog import SkillCatalog, SkillConfigurationError, parse_skill
from core.agent_platform.ci_diagnosis import definition as ci_definition
from core.feature_impact.agent import definition as feature_definition
from core.agent_platform.contracts import AgentDefinition
from core.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def write_skill(root, name="example", front=None):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + (front or f"name: {name}\ndescription: 示例") + "\n---\n# 方法\n\n保留 **正文**。\n", encoding="utf-8", newline="")
    return path


def test_standard_fields_body_and_readonly(tmp_path):
    path = write_skill(tmp_path, front='name: example\ndescription: 示例\nlicense: MIT\ncompatibility: LocalAgent\nmetadata:\n  version: "1.0"\nallowed-tools: Read')
    skill = parse_skill(path)
    assert (skill.name, skill.description, skill.license, skill.compatibility) == ("example", "示例", "MIT", "LocalAgent")
    assert skill.metadata == {"version": "1.0"}
    assert skill.allowed_tools == "Read"
    assert skill.body == "# 方法\n\n保留 **正文**。\n"
    assert skill.skill_md_sha256 == sha256(path.read_bytes()).hexdigest()
    with pytest.raises(TypeError):
        skill.metadata["version"] = "2"
    catalog = SkillCatalog.discover([tmp_path])
    with pytest.raises(TypeError):
        catalog.skills["other"] = skill


def test_invalid_packages_are_rejected(tmp_path):
    for front in ("name: Bad\ndescription: 示例", "name: example", "name: example\ndescription: [", "name: example\ndescription: 示例\nmetadata: {version: 1}"):
        path = write_skill(tmp_path, front=front)
        with pytest.raises(SkillConfigurationError, match="SKILL.md"):
            parse_skill(path)


def test_discovery_duplicates_and_incomplete_directory(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_skill(a)
    write_skill(b)
    (a / "incomplete").mkdir()
    assert [s.name for s in SkillCatalog.discover([a]).list()] == ["example"]
    with pytest.raises(SkillConfigurationError, match="重复 Skill example"):
        SkillCatalog.discover([a, b])
    with pytest.raises(SkillConfigurationError, match="root"):
        SkillCatalog.discover([tmp_path / "missing"])


def test_business_packages_and_agent_visibility():
    catalog = SkillCatalog.discover([ROOT / "skills"])
    assert len(catalog.list()) == 3
    for skill in catalog.list():
        assert skill.package_path == ROOT / "skills" / skill.name
        assert skill.description and skill.body
        assert list((skill.package_path / "references").glob("*.md"))
    assert {s.name for s in catalog.for_agent(ci_definition())} == {"ci-failure-investigation", "ci-environment-check"}
    assert {s.name for s in catalog.for_agent(feature_definition())} == {"feature-impact-analysis"}
    other = AgentDefinition(agent_id="other", agent_version="1.0.0", display_name="其他", role="其他", instructions="其他")
    assert catalog.for_agent(other) == ()
    from dataclasses import replace
    with pytest.raises(SkillConfigurationError, match="未发现 Skill"):
        catalog.for_agent(replace(other, business_options={"skills": ["missing"]}))
    assert SkillCatalog.discover([]).for_agent(ci_definition()) == ()


def test_skill_roots_settings(monkeypatch, tmp_path):
    import os
    monkeypatch.setenv("LOCAL_AGENT_SKILL_ROOTS", os.pathsep.join(["skills", str(tmp_path)]))
    settings = Settings.load()
    assert settings.skill_roots == (str((Path(settings.project_root) / "skills").resolve()), str(tmp_path.resolve()))
    monkeypatch.setenv("LOCAL_AGENT_SKILL_ROOTS", "")
    assert Settings.load().skill_roots == ()
    monkeypatch.delenv("LOCAL_AGENT_SKILL_ROOTS")
    assert Settings.load().skill_roots == (str((Path(settings.project_root) / "skills").resolve()),)
