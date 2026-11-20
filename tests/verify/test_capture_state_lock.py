"""Two captures at once (CI jobs sharing a checkout, an agent and a person): each one's slot
update of ``.pymolt/contract_state.json`` (load, archive the displaced capture, promote,
save) runs under the state lock, so neither loses the other's slot."""
from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path

from pymolt.verify import service

ROUNDS = 300  # enough that, without the lock, updates are lost in practice


def _capture_many(root: str, when: str, go: str, out: str) -> None:
    """In its own process: ``ROUNDS`` captures of ``when`` through ``capture_named_trace``
    (only the traced command is faked), checking before each that the previous one's slot
    is still on disk. Writes the rounds where it was not (lost updates) to ``out``."""
    from pymolt.verify.models import CaptureMode
    from pymolt.verify.service import TraceCaptureResult

    def recorded_once(target, command, out_path, backend, **kwargs):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({"q": "json.loads", "t": "return",
                                        "in": {"bound": {}}, "result": None}) + "\n")
        return TraceCaptureResult(out_path=str(out_path), events=1, processes=1,
                                  where="local")

    service.capture_trace = recorded_once
    Path(out + ".ready").touch()            # both start together: the parent waits for both
    while not Path(go).exists():
        time.sleep(0.001)
    lost = []
    for i in range(ROUNDS):
        if i:
            slot = getattr(service.load_contract_state(root), when)
            if slot is None or slot.command != ["run", when, str(i - 1)]:
                lost.append(i)
        service.capture_named_trace(root, when, CaptureMode.LIVE_COMMAND,
                                    command=["run", when, str(i)], cwd=root)
    Path(out).write_text(json.dumps(lost), encoding="utf-8")


def test_two_processes_capturing_different_slots_keep_both(tmp_path):
    (tmp_path / "app.py").write_text("import json\n", encoding="utf-8")
    go = tmp_path / "go"
    context = multiprocessing.get_context("spawn")
    children = {
        when: context.Process(target=_capture_many,
                              args=(str(tmp_path), when, str(go), str(tmp_path / when)))
        for when in ("baseline", "post_migration")
    }
    for child in children.values():
        child.start()
    deadline = time.monotonic() + 60
    while not all((tmp_path / f"{when}.ready").exists() for when in children):
        assert time.monotonic() < deadline, "the capturing processes did not start"
        time.sleep(0.01)
    go.touch()
    for child in children.values():
        child.join(120)

    assert [child.exitcode for child in children.values()] == [0, 0]
    assert {when: json.loads((tmp_path / when).read_text()) for when in children} == {
        "baseline": [], "post_migration": []}
    state = service.load_contract_state(tmp_path)
    assert state.baseline.command == ["run", "baseline", str(ROUNDS - 1)]
    assert state.post_migration.command == ["run", "post_migration", str(ROUNDS - 1)]
