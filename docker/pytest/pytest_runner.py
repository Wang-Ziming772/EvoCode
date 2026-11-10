"""Run pytest and send a bounded JUnit summary to the host executor."""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


_REPORT = Path("/tmp/evocode-junit.xml")
_MAX_REPORT_BYTES = 4 * 1024 * 1024


def _report_summary():
    try:
        with _REPORT.open("rb") as file:
            data = file.read(_MAX_REPORT_BYTES + 1)
        if len(data) > _MAX_REPORT_BYTES:
            return {"error": "pytest 结构化报告超过大小限制"}
        root = ET.fromstring(data)
        if root.tag != "testsuites":
            raise ValueError
        suites = root.findall("testsuite")
        if not suites:
            raise ValueError
        totals = {"tests": 0, "skipped": 0, "failures": 0, "errors": 0}
        for suite in suites:
            counts = {name: int(suite.attrib[name]) for name in totals}
            if any(value < 0 for value in counts.values()):
                raise ValueError
            cases = suite.findall("testcase")
            if (counts["tests"] != len(cases)
                    or counts["skipped"] != sum(case.find("skipped") is not None for case in cases)
                    or counts["failures"] != sum(case.find("failure") is not None for case in cases)
                    or counts["errors"] != sum(case.find("error") is not None for case in cases)):
                raise ValueError
            for name, value in counts.items():
                totals[name] += value
        return totals
    except FileNotFoundError:
        return {"error": "pytest 未生成结构化报告"}
    except (OSError, ET.ParseError, KeyError, ValueError, OverflowError):
        return {"error": "pytest 结构化报告无效"}


def main():
    token = sys.argv[1]
    command = [
        "/usr/local/bin/python", "-m", "pytest", "-q", "-o", "cache_dir=/tmp/pytest-cache",
        "--basetemp=/tmp/pytest-temp", f"--junitxml={_REPORT}",
    ]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, check=False)
    summary = _report_summary()
    summary["exit_code"] = result.returncode
    marker = f"\n__EVOCODE_PYTEST_REPORT_{token}__:"
    sys.stderr.write(marker + json.dumps(summary, separators=(",", ":"), ensure_ascii=True) + "\n")
    sys.stderr.flush()
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    sys.exit(main())
