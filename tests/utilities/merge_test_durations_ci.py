import json
import re
from pathlib import Path


def merge_durations(files: list, output_suffix: str):
    outname = f"test_durations/.test_durations_{output_suffix}"
    durations_path = Path(outname)
    try:
        previous_durations = json.loads(durations_path.read_text())
    except FileNotFoundError:
        previous_durations = {}
    new_durations = previous_durations.copy()

    for path in files:
        d = Path(path)
        durations = json.loads(d.read_text())
        new_durations.update(
            {
                name: duration
                for (name, duration) in durations.items()
                if previous_durations.get(name) != duration
            }
        )


    durations_path.parent.mkdir(parents=True, exist_ok=True)

    with open(outname, 'w') as f:
        f.writelines(json.dumps(new_durations))


if __name__ == '__main__':
    regex = re.compile(r'.*python-((\d+\.?)+).*')
    files = Path(".").glob("duration-chunk-*/*")
    versions = {}
    for f in files:
        m = regex.match(str(f))
        if m:
            versions[m.group(1)] = [m.group(0)] + versions.get(m.group(1), [])

    for py_vers, file_list in versions.items():
        merge_durations(file_list, py_vers)
