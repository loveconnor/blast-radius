import json, os
failed = set(); seen = set()
def pytest_runtest_logreport(report):
    seen.add(report.nodeid)
    if report.failed:
        failed.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        failed.add("COLLECT::" + report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["FAIL_OUT"], "w") as f:
        json.dump({"failed": sorted(failed), "seen": len(seen), "exit": int(exitstatus)}, f)
