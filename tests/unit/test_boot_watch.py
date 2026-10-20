from tests.boot.watch import classify


def test_failure_wins_over_success():
    v = classify({"screen (OCR)": "Recycle Bin\nSTOP: 0x0000007B (0xF78D2524, ...)"}, "windows")
    assert v.state == "failure" and "STOP" in v.reason


def test_success_patterns_per_family():
    assert classify({"serial console": "Ubuntu 24.04 LTS ubuntu ttyS0\n\nubuntu login: "},
                    "linux").state == "success"
    assert classify({"text screen": "FreeCom version 0.83\nC:\\>"}, "dos").state == "success"
    assert classify({"screen (OCR)": "Press Ctrl + Alt + Delete to log on"}, "windows").state == "success"
    assert classify({"screen (OCR)": "My Computer\nRecycle Bin\nStart"}, "win9x").state == "success"
    assert classify({"screen (OCR)": "Starting Windows..."}, "windows").state == "waiting"


def test_custom_success_text():
    assert classify({"screen (OCR)": "ReactOS Setup"}, "windows", [r"ReactOS\s+Setup"]).state == "success"


def test_boot_failures():
    for text in ("Kernel panic - not syncing: VFS", "grub rescue> ", "NTLDR is missing",
                 "Your PC ran into a problem and needs to restart", "Windows protection error."):
        assert classify({"x": text}, "other").state == "failure", text
