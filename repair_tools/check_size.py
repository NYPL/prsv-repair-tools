import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from repair_tools.utils.format_utils import print_standard_summary

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--directory",
        "-d",
        type=Path, 
        nargs='+',
        required=True,
        help="One or more base directories to scan"
    )
    parser.add_argument(
        "--limit", 
        type=float, 
        required=True,
        help="The size threshold in Gigabytes (GB)"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count(),
        help="Number of threads to use (default: CPU count)"
    )
    return parser.parse_args()

def get_bag_size(path: Path) -> int:
    total = 0
    stack = [path]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        # recursion issues to solve later
                        if entry.is_symlink():
                            continue
                            
                        if entry.is_file():
                            total += entry.stat().st_size
                        elif entry.is_dir():
                            stack.append(entry.path)
                    except (PermissionError, FileNotFoundError):
                        continue
        except (PermissionError, FileNotFoundError):
            continue
            
    return total

def check_structure(base_path: Path, size_limit_gb: float, max_workers: int):
    if not base_path.exists() or not base_path.is_dir():
        print(f"Skipping invalid directory: {base_path}")
        return {}

    print(f"Scanning bags in '{base_path}'...")

    bags_to_scan = []
    
    try:
        for group_dir in base_path.iterdir():
            if group_dir.is_dir() and not group_dir.is_symlink():
                try:
                    for bag_dir in group_dir.iterdir():
                        if bag_dir.is_dir() and not bag_dir.is_symlink():
                            bags_to_scan.append(bag_dir)
                except PermissionError:
                    print(f"Permission denied: {group_dir}")
    except PermissionError:
        print(f"Permission denied: {base_path}")
        return {}

    bad_dirs = {}
    
    print(f"Found {len(bags_to_scan)} targets. Calculating sizes...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {
            executor.submit(get_bag_size, p): p 
            for p in bags_to_scan
        }

        for future in as_completed(future_to_path):
            bag_path = future_to_path[future]
            try:
                size_bytes = future.result()
                size_gb = size_bytes / (1024**3)

                if size_gb > size_limit_gb:
                    bad_dirs[str(bag_path)] = size_gb
                    
            except Exception as exc:
                print(f"Error scanning {bag_path.name}: {exc}")

    return bad_dirs

def main():
    args = parse_args()
    bad_dirs = {}

    for dir_path in args.directory:
        result_dict = check_structure(dir_path, args.limit, args.workers)
        bad_dirs.update(result_dict)

    if bad_dirs:
        stats = {dirname: f"{total_gb:.2f} GB" for dirname, total_gb in bad_dirs.items()}
        print_standard_summary("Base dirs containing large bags", stats)
        sys.exit(1)
    else:
        print_standard_summary("Limit Check", {"Status": "No bags exceeded the limit."})

if __name__ == "__main__":
    main()