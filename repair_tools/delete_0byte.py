import argparse
import logging
from pathlib import Path


logging.basicConfig(level=logging.INFO, format='%(message)s')

class ExtendUnique(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest, None)

        if items is None:
            items = set(values)
        elif isinstance(items, set):
            items = items.union(values)

        setattr(namespace, self.dest, items)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
            "--directory",
            type=list_of_paths,
            dest="directories", 
            action=ExtendUnique,
            help="path to a directory of packages",
            required=True
        )
    return parser.parse_args()
    
def list_of_paths(p: str) -> list[Path]:
    path = extant_dir(p)
    child_dirs = []
    for child in path.iterdir():
        if child.is_dir():
            child_dirs.append(child)

    if not child_dirs:
        raise argparse.ArgumentTypeError(f"{path} does not contain child directories")

    return child_dirs

def extant_dir(p: str) -> Path:
    path = Path(p)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"{path} is not a directory")

    return path

def manage_zero_byte_files(directories: set[Path]):
    zero_byte_files = []

    logging.info("Scanning for 0-byte files.")
    for folder in directories:
        for item in folder.rglob('*'):
            if item.is_file() and item.stat().st_size == 0:
                zero_byte_files.append(item)

    if not zero_byte_files:
        logging.info("No 0-byte files found.")
        return

    logging.info(f"\nFound {len(zero_byte_files)} file(s) with 0 bytes:")
    for f in zero_byte_files:
        logging.info(f" - {f}")

    confirm = input("Do you want to delete these files? (y/n): ").strip().lower()

    if confirm == 'y':
        count = 0
        for f in zero_byte_files:
            try:
                f.unlink()
                logging.info(f"Deleted: {f.name}")
                count += 1
            except OSError as e:
                logging.error(f"Error deleting {f.name}: {e}")
        logging.info(f"{count} files deleted.")
    else:
        logging.info("No files were deleted.")

def main():
    args = parse_args()
    
    if args.directories:
        manage_zero_byte_files(args.directories)
    else:
        logging.info("No valid directories provided.")

if __name__ == "__main__":
    main()