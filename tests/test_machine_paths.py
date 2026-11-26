"""``machine_paths``: the project's folder and the home folder out of pymolt's prose, the
project's own text left as written."""
from __future__ import annotations

import os

import pytest
from pydantic import BaseModel, Field

from pymolt.machine_paths import PathScrubber, scrub_prose

HOME = "/home/user"
ROOT = "/home/user/work/app"


@pytest.fixture
def scrub():
    return PathScrubber(ROOT, home=HOME)


@pytest.mark.parametrize("text, expected", [
    (f"{ROOT}/.venv/bin/python", ".venv/bin/python"),
    (f"baseline capture interpreter ({ROOT})", "baseline capture interpreter (.)"),
    (f"setup names a target environment: {HOME}/.envs/3.12",
     "setup names a target environment: ~/.envs/3.12"),
    (HOME, "~"),
    (f"{ROOT}/a and {HOME}/b", "a and ~/b"),
    # Not the project or the home folder: a longer name, or the same letters inside a path.
    ("/home/username/x", "/home/username/x"),
    (f"{ROOT}-old/x", "~/work/app-old/x"),
    (f"/srv{HOME}/x", f"/srv{HOME}/x"),
    ("/usr/bin/python3", "/usr/bin/python3"),
    ("", ""),
])
def test_text(scrub, text, expected):
    assert scrub.text(text) == expected


def test_a_value_keeps_its_shape(scrub):
    value = {"command": [f"{ROOT}/.venv/bin/python", "-m", "pytest"],
             "sides": {"old": f"probe:{HOME}/py/bin/python"}, "n": 3, "none": None}
    assert scrub(value) == {"command": [".venv/bin/python", "-m", "pytest"],
                            "sides": {"old": "probe:~/py/bin/python"}, "n": 3, "none": None}


def test_every_spelling_of_a_folder(tmp_path):
    real = tmp_path / "real"
    (real / "app").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    scrub = PathScrubber(link / "app", home=tmp_path / "nohome")
    assert scrub.text(f"{link}/app/x {real}/app/y") == "x y"
    assert scrub.text(os.path.realpath(real / "app")) == "."


def test_no_home_folder(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr("pymolt.machine_paths._home", lambda: None)
    assert PathScrubber(None).text(f"{HOME}/x") == f"{HOME}/x"
    assert PathScrubber(ROOT).text(f"{ROOT}/x {HOME}/y") == f"x {HOME}/y"


class _Site(BaseModel):
    file: str
    text: str


class _Step(BaseModel):
    title: str
    evidence: list[str] = Field(default_factory=list)
    command: list[str] | None = None
    where: list[_Site] = Field(default_factory=list)
    new_source: str = ""
    extra: dict = Field(default_factory=dict)


def test_scrub_prose_rewrites_prose_only(scrub):
    literal = f'DATA = "{HOME}/data.txt"'
    step = _Step(title=f"title {HOME}", evidence=[f"from {HOME}/.envs/3.12"],
                 command=[f"{ROOT}/.venv/bin/python"], where=[_Site(file="m.py", text=literal)],
                 new_source=literal + "\n",
                 extra={"notes": [f"probe {HOME}/x"], "lines": [f"pkg @ file://{HOME}/pkg"]})
    scrub_prose([step], scrub)
    assert step.evidence == ["from ~/.envs/3.12"]
    assert step.command == [".venv/bin/python"]
    assert step.extra == {"notes": ["probe ~/x"], "lines": [f"pkg @ file://{HOME}/pkg"]}
    # The project's text and fields that are not prose: as written.
    assert step.where[0].text == literal and step.new_source == literal + "\n"
    assert step.title == f"title {HOME}"
