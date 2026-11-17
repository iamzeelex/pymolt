"""The user docs may only name commands and options that exist.

README.md, docs/*.md and the plugin's Markdown (its README, skill, agent and command, which agents
follow literally) drifted from the CLI more than once: removed commands, renamed flags, flags that
never existed. This checks, against the live Typer app:

- every `pymolt <command>` in code (inline code and fenced blocks), with the flags and the
  `capture` slot that follow it, up to a `--` (what follows belongs to CMD);
- the option tables under a "### `pymolt <command>`" heading;
- inline code that names a command without the `pymolt` prefix (`setup --target-env`);
- a bare inline flag (`--sample-rate`), which must exist on some command.

Hidden commands and hidden options (removed ones that only print their replacement) count as
missing. A region between `<!-- docs-check:off -->` and `<!-- docs-check:on -->` (e.g. the table of
removed commands) is skipped.
"""
import re
from pathlib import Path

import pytest
import typer.main

from pymolt.interfaces.cli.commands import app
from pymolt.interfaces.cli.verify_cmd import _CAPTURE_WHEN

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
# Design notes (docs/design/) describe history and plans, not the current CLI, so they are
# not checked.
DOCS = [
    ROOT / "README.md",
    *sorted((ROOT / "docs").glob("*.md")),
    PLUGIN / "README.md",
    *sorted(PLUGIN.glob("skills/*/SKILL.md")),
    *sorted(PLUGIN.glob("agents/*.md")),
    *sorted(PLUGIN.glob("commands/*.md")),
]
ANY = "*"   # a bare flag: it must exist on some command

_OFF = re.compile(r"<!-- docs-check:off -->.*?<!-- docs-check:on -->", re.S)
_FENCE = re.compile(r"^(```+|~~~+)[^\n]*\n(.*?)^\1[ \t]*$", re.S | re.M)
_INLINE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^#{2,4} `pymolt ([a-z][a-z-]*)")
_INVOCATION = re.compile(r"(?<![\w./\[-])pymolt ([a-z][a-z-]*)((?: +[^\s`|]+)*)")
_FLAG = re.compile(r"(?<![\w-])(--?[a-zA-Z][a-zA-Z-]*)")
_COMMENT = re.compile(r"(^|\s)#\s.*$")
_PYTHON = re.compile(r"^\s*(from|import)\s|^\s*>>>")


def _cli() -> dict[str, set[str]]:
    group = typer.main.get_command(app)
    commands = {}
    for name, cmd in group.commands.items():
        if cmd.hidden:
            continue
        opts = {"--help"}
        for param in cmd.params:
            if getattr(param, "hidden", False):
                continue
            opts.update(o for o in (*param.opts, *param.secondary_opts) if o.startswith("-"))
        commands[name] = opts
    return commands


def _lines(snippet: str) -> list[str]:
    """The snippet's lines, with `\\` continuations joined and shell comments dropped."""
    joined = re.sub(r"\s*\\\n\s*", " ", snippet)
    return [_COMMENT.sub("", line) for line in joined.splitlines() if not _PYTHON.match(line)]


def _references(text: str, commands: set[str] = frozenset()) -> list[tuple[str, str | None, str]]:
    """(command, flag or None, where) for every reference in code and option tables.

    A flag is `--name`/`-x`, or `slot:<value>` for the slot after `pymolt capture`. With
    `commands`, inline code that starts with one of those names counts as an invocation.
    """
    text = _OFF.sub("", text)
    refs: list[tuple[str, str | None, str]] = []
    code = [m.group(2) for m in _FENCE.finditer(text)]
    prose = _FENCE.sub("", text)
    inline = [m.group(1) for m in _INLINE.finditer(prose)]
    for snippet in code + inline:
        for line in _lines(snippet):
            for m in _INVOCATION.finditer(line):
                command, rest = m.group(1), m.group(2).split()
                refs.append((command, None, line.strip()))
                if command == "capture" and rest and not rest[0].startswith("-"):
                    refs.append((command, f"slot:{rest[0]}", line.strip()))
                for token in rest:
                    if token == "--":            # what follows belongs to CMD (pytest, …)
                        break
                    for flag in _FLAG.findall(token):
                        refs.append((command, flag, line.strip()))
    for snippet in inline:
        words = snippet.split()
        if not words or "pymolt" in words:
            continue
        if words[0].startswith("--") and len(words) == 1:
            refs.extend((ANY, flag, snippet) for flag in _FLAG.findall(words[0]))
        elif words[0] in commands and len(words) > 1 and words[1].startswith("-"):
            for token in words[1:]:
                if token == "--":
                    break
                refs.extend((words[0], flag, snippet) for flag in _FLAG.findall(token))
    # Option tables under a "### `pymolt <command>`" heading.
    current = None
    for line in prose.splitlines():
        heading = _HEADING.match(line)
        if heading:
            current = heading.group(1)
            continue
        if line.startswith("#"):
            current = None
        elif current and line.startswith("|"):
            first_cell = line.split("|")[1] if line.count("|") > 1 else ""
            for flag in _FLAG.findall(first_cell):
                refs.append((current, flag, line.strip()))
    return refs


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: str(d.relative_to(ROOT)))
def test_docs_name_only_existing_commands_and_options(doc):
    cli = _cli()
    every_flag = set().union(*cli.values())
    slots = set(_CAPTURE_WHEN)
    problems = []
    for command, flag, where in _references(doc.read_text(encoding="utf-8"), set(cli)):
        if command == ANY:
            if flag not in every_flag:
                problems.append(f"no command has {flag}: {where}")
        elif command not in cli:
            problems.append(f"no command `pymolt {command}`: {where}")
        elif flag is None:
            continue
        elif flag.startswith("slot:"):
            if flag[5:] not in slots:
                problems.append(f"`pymolt capture` has no slot {flag[5:]!r}: {where}")
        elif flag not in cli[command]:
            problems.append(f"`pymolt {command}` has no {flag}: {where}")
    assert not problems, "\n".join(dict.fromkeys(problems))


def test_the_check_sees_references():
    refs = _references(
        "```bash\npymolt capture baseline --mode tests -- pytest --maxfail 1\n"
        "pymolt setup . \\\n    --bogus-flag x   # install the pymolt package\n"
        "from pymolt import views\n```\n"
        "~~~\npymolt plan . -o plan.json\n~~~\n"
        "Run `pymolt plan --online`, then `pymolt capture post_migration`.\n"
        "Set `--sample-rate`, or `setup --target-env DIR`.\n"
        "### `pymolt apply [dir]`\n| `--plan` | file |\n"
        "<!-- docs-check:off -->`pymolt doctor`<!-- docs-check:on -->\n",
        commands={"setup", "plan"},
    )
    assert ("capture", "--mode", "pymolt capture baseline --mode tests -- pytest --maxfail 1") \
        in refs
    assert not any(flag == "--maxfail" for _c, flag, _w in refs)   # after `--`: CMD's flags
    assert ("setup", "--bogus-flag", "pymolt setup . --bogus-flag x") in refs   # continuation
    assert not any(cmd in {"import", "package"} for cmd, _f, _w in refs)   # Python, comments
    assert ("plan", "-o", "pymolt plan . -o plan.json") in refs             # ~~~ fence, short
    assert ("plan", "--online", "pymolt plan --online") in refs
    assert ("capture", "slot:post_migration", "pymolt capture post_migration") in refs
    assert (ANY, "--sample-rate", "--sample-rate") in refs
    assert ("setup", "--target-env", "setup --target-env DIR") in refs
    assert ("apply", "--plan", "| `--plan` | file |") in refs
    assert not any(cmd == "doctor" for cmd, _f, _w in refs)


def test_hidden_options_count_as_missing():
    assert "--current-python" not in _cli()["assess"]
