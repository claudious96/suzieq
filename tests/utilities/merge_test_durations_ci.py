import json
import argparse
from pathlib import Path


def merge_durations(filename: str, prefix: str):
    split_paths = Path(".").glob(f"{prefix}-*/{filename}")
    durations_path = Path(filename)
    try:
        previous_durations = json.loads(durations_path.read_text())
    except FileNotFoundError:
        previous_durations = {}
    new_durations = previous_durations.copy()

    for path in split_paths:
        durations = json.loads(path.read_text())
        new_durations.update(
            {
                name: duration
                for (name, duration) in durations.items()
                if previous_durations.get(name) != duration
            }
        )

    durations_path.parent.mkdir(parents=True, exist_ok=True)
    durations_path.write_text(json.dumps(new_durations))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-d", "--durations-path", type=str, help="Path of duration files")
    parser.add_argument(
        "-p", "--prefix", type=str, help="File prefix")
    args = parser.parse_args()
    merge_durations(args.durations_path, args.prefix)
